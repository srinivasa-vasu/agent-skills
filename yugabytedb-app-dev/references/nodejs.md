# Node.js — YugabyteDB Application Reference

This reference uses the **YugabyteDB smart driver** (`@yugabytedb/pg`), a drop-in fork of
node-postgres (`pg`) that adds cluster-aware load balancing and topology routing. The centerpiece
is a production-grade **multi-endpoint pool manager** that probes endpoints, fails over between them,
and drives the smart driver correctly.

---

## Dependencies

```bash
npm install @yugabytedb/pg     # YugabyteDB smart driver (fork of node-postgres)
```

`@yugabytedb/pg` is API-compatible with `pg`: it exports the same `Pool` and `Client`, plus the
extra `loadBalance` / `topologyKeys` options. The latest published version is `8.7.3-yb-10`.

For TypeScript, the bundled types ship with the package; if you need the upstream community types
as a fallback, `npm install --save-dev @types/pg`.

> **Use the smart driver, not plain `pg`.** The smart driver discovers every node in the cluster and
> spreads connections across them. With plain `pg` you connect to a single host and get no
> load balancing or topology-aware routing.

---

## Smart Driver Essentials

Set `loadBalance` on the `Pool` to turn on cluster-aware balancing:

- `loadBalance: 'any'` — balance across all live nodes (most common).
- `loadBalance: 'only-primary'` / `'only-rr'` — restrict to primary or read-replica nodes.
- `topologyKeys: 'cloud.region.zone'` — pin connections to specific placement zones.

The driver only needs **one reachable contact point** to bootstrap. Once connected, it queries
`yb_servers()` to learn the full topology and opens subsequent connections directly to all nodes.

### Three gotchas this pattern solves

1. **Smart-driver state is global and static.** The driver caches cluster topology
   (`controlClient`, `connectionMap`, `hostServerInfo*`, `topologyKeyMap`, failed-host tracking) on
   **static fields of the `Client` class** — shared across *every* `Pool` instance in the process.
   If you retire a pool after a failure without clearing this cache, the next pool inherits stale
   topology and may keep trying dead nodes. The manager calls `resetSmartDriverState()` before
   activating a new pool to force fresh cluster discovery against the new contact point.

2. **`pg-pool` silently ignores `min`.** The pool never eagerly opens `min` connections — it creates
   clients lazily, on demand, up to `max`. To actually spread connections across the cluster at
   startup (so the smart driver opens sockets to every node), you must **prewarm** manually by
   acquiring `N` clients and releasing them back.

3. **`loadBalance` overrides `connectionTimeoutMillis` during discovery.** While the smart driver is
   doing topology discovery it can hang past your configured timeout on a dead host. So endpoint
   **probing uses a plain pool with `loadBalance: false`**, which respects `connectionTimeoutMillis`
   and gives a fast, reliable up/down signal before committing a real load-balanced pool to that host.

---

## Multi-Endpoint Pool Manager

A single class that: probes endpoints, activates a load-balanced pool against the first live one,
fails over to the next endpoint on connection errors, retries transient transaction errors with
exponential backoff, and exposes a `pg`-style `query()` / `connect()` / `withTransaction()` API.

```javascript
'use strict';

const { Pool, Client } = require('@yugabytedb/pg');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const DEFAULT_POOL_OPTIONS = {
  port: 5433,
  database: 'yugabyte',
  user: 'yugabyte',
  password: 'yugabyte',
  loadBalance: 'any',         // smart driver — only used on confirmed-live hosts
  min: 2,
  max: 10,
  idleTimeoutMillis: 120_000,
  connectionTimeoutMillis: 5_000,
  // statement_timeout: 15_000,
  application_name: 'yb-node-app',
};

// Probe pool options — NO loadBalance, so connectionTimeoutMillis is respected
const PROBE_OPTIONS = {
  min: 0,
  max: 1,
  connectionTimeoutMillis: 5_000,
};

const RETRY_SQL_STATES = new Set(['40001', '40P01', '57P01', '08006']);
const RETRY_XX000_MSGS = ['schema version mismatch', 'duplicate request'];
const RETRY_CONN_MSGS = ['connection is closed', 'connection reset by peer'];

const MAX_TX_RETRIES = 5;
const INITIAL_BACKOFF = 200;
const MAX_BACKOFF = 5_000;
const BACKOFF_MULT = 3;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isConnectionError(err) {
  if (!err) return false;
  const code = (err.code || '').toLowerCase();
  const msg = (err.message || '').toLowerCase();
  if (['econnrefused', 'enotfound', 'etimedout', 'econnreset'].includes(code)) return true;
  if (['08006', '08001', '08004'].includes(code)) return true;
  if (msg.includes('connection terminated due to connection timeout')) return true;
  // Auth/config errors (pg_hba, bad password) fall through → fatal, don't rotate
  return false;
}

function isRetryableQueryError(err) {
  if (!err) return false;
  let cause = err;
  while (cause) {
    const code = cause.code || '';
    if (RETRY_SQL_STATES.has(code)) return true;
    if (code === 'XX000') {
      const msg = (cause.message || '').toLowerCase();
      if (RETRY_XX000_MSGS.some(m => msg.includes(m))) return true;
    }
    const msg = (cause.message || '').toLowerCase();
    if (RETRY_CONN_MSGS.some(m => msg.includes(m))) return true;
    cause = cause.cause || cause.original;
  }
  return false;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * The smart driver keeps its cluster-topology cache (controlClient,
 * hostServerInfoPrimary/RR, connectionMap, topologyKeyMap) on STATIC fields
 * of the Client class — shared across every Pool instance in the process.
 *
 * Resetting these statics whenever we retire the active pool forces the
 * next pool to re-run cluster discovery from scratch against the new host,
 * which is what actually restores load balancing across all nodes.
 */
function resetSmartDriverState() {
  Client.controlClient = undefined;
  Client.controlClientHost = '';
  Client.connectionMap = new Map();
  Client.failedHosts = new Map();
  Client.failedHostsTime = new Map();
  Client.hostServerInfoPrimary = new Map();
  Client.hostServerInfoRR = new Map();
  Client.topologyKeyMap = new Map();
}

// ---------------------------------------------------------------------------
// EndpointPoolManager
// ---------------------------------------------------------------------------

class EndpointPoolManager {
  constructor(endpoints, poolOptions = {}) {
    if (!endpoints || endpoints.length <= 1) {
      throw new Error('EndpointPoolManager: at least two endpoints are required');
    }
    this._endpoints = [...endpoints];
    this._poolOpts = { ...DEFAULT_POOL_OPTIONS, ...poolOptions };
    this._pool = null;
    this._activeIdx = -1;
    this._lock = Promise.resolve();
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  async init() {
    for (let i = 0; i < this._endpoints.length; i++) {
      const host = this._endpoints[i];
      console.log(`[yb-pool] Trying endpoint [${i}]: ${host}`);
      try {
        await this._probe(host);                  // plain pool, no loadBalance
        resetSmartDriverState();                   // force fresh cluster discovery
        const pool = this._makePool(host);        // real pool with loadBalance
        this._activatePool(pool, i);
        console.log(`[yb-pool] Connected via endpoint [${i}]: ${host}`);
        return;
      } catch (err) {
        if (!isConnectionError(err)) {
          console.error(`[yb-pool] Fatal error on endpoint [${i}] ${host}: ${err.message}`);
          throw err;
        }
        console.warn(`[yb-pool] Endpoint [${i}] ${host} unreachable: ${err.message}`);
      }
    }
    throw new Error(
      `[yb-pool] All ${this._endpoints.length} endpoints unreachable. ` +
      `Hosts tried: ${this._endpoints.join(', ')}`
    );
  }

  async connect() {
    if (!this._pool) throw new Error('[yb-pool] Pool not initialised — call init() first');
    try {
      return await this._pool.connect();
    } catch (err) {
      if (isConnectionError(err)) {
        console.warn(`[yb-pool] connect() failed on endpoint [${this._activeIdx}]: ${err.message}`);
        await this._failover();
        return await this._pool.connect();
      }
      throw err;
    }
  }

  async query(text, values) {
    return this.withRetry(async () => {
      if (!this._pool) throw new Error('[yb-pool] Pool not initialised');
      try {
        return await this._pool.query(text, values);
      } catch (err) {
        if (isConnectionError(err)) {
          await this._failover();
          return await this._pool.query(text, values);
        }
        throw err;
      }
    });
  }

  async withTransaction(fn) {
    return this.withRetry(async () => {
      const client = await this.connect();
      try {
        await client.query('BEGIN');
        const result = await fn(client);
        await client.query('COMMIT');
        return result;
      } catch (err) {
        try { await client.query('ROLLBACK'); } catch (_) { /* ignore */ }
        throw err;
      } finally {
        client.release();
      }
    });
  }

  async withRetry(fn) {
    let backoff = INITIAL_BACKOFF;
    let lastErr;
    for (let attempt = 0; attempt < MAX_TX_RETRIES; attempt++) {
      try {
        return await fn();
      } catch (err) {
        lastErr = err;
        if (attempt < MAX_TX_RETRIES - 1 && isRetryableQueryError(err)) {
          console.warn(`[yb-pool] Retrying (attempt ${attempt + 1}/${MAX_TX_RETRIES}): ${err.message}`);
          await sleep(backoff);
          backoff = Math.min(backoff * BACKOFF_MULT, MAX_BACKOFF);
        } else {
          throw err;
        }
      }
    }
    throw lastErr;
  }

  /**
   * pg-pool's `min` option is NOT enforced by the driver — it's silently
   * ignored. The pool only ever creates a client lazily, on demand, when a
   * connect()/query() call finds no idle client available (up to `max`).
   *
   * Prewarm establishes the underlying sockets up front so the smart driver
   * opens connections across all cluster nodes before the first real query.
   */
  async prewarm(count) {
    const target = count ?? this._poolOpts.min ?? 0;
    if (!this._pool || target <= 0) return;

    console.log(`[yb-pool] Prewarming ${target} connections on ${this.activeEndpoint}...`);
    const clients = await Promise.all(
      Array.from({ length: target }, () =>
        this._pool.connect().catch(err => {
          console.warn(`[yb-pool] Prewarm connection failed: ${err.message}`);
          return null;
        })
      )
    );
    // Release them all back into the pool's idle queue immediately —
    // we just wanted the underlying TCP/sockets established now, not held.
    clients.filter(Boolean).forEach(c => c.release());
    console.log(`[yb-pool] Prewarmed ${clients.filter(Boolean).length}/${target} connections`);
  }

  get activeEndpoint() {
    return this._activeIdx >= 0 ? this._endpoints[this._activeIdx] : null;
  }

  async end() {
    if (this._pool) {
      await this._pool.end();
      this._pool = null;
      this._activeIdx = -1;
    }
  }

  // ── Private ────────────────────────────────────────────────────────────────

  async _probe(host) {
    const probePool = new Pool({
      ...this._poolOpts,
      host,
      loadBalance: false,          // critical — no smart driver during probe
      ...PROBE_OPTIONS,
    });
    probePool.on('error', () => { });  // suppress unhandled idle errors
    try {
      const client = await probePool.connect();
      try { await client.query('SELECT 1'); }
      finally { client.release(); }
    } finally {
      await probePool.end().catch(() => { });   // always release sockets
    }
  }

  _makePool(host) {
    const pool = new Pool({ ...this._poolOpts, host });
    pool.on('error', (err) => {
      console.error(`[yb-pool] Idle client error on ${host}:`, err.message);
    });
    return pool;
  }

  _activatePool(pool, idx) {
    this._pool = pool;
    this._activeIdx = idx;
  }

  async _failover() {
    // Serialize failovers so concurrent callers don't each rotate the pool.
    this._lock = this._lock.then(() => this._doFailover());
    return this._lock;
  }

  async _doFailover() {
    const failedIdx = this._activeIdx;
    const failedHost = this._endpoints[failedIdx];

    const dying = this._pool;
    this._pool = null;
    this._activeIdx = -1;
    dying?.end().catch(e =>
      console.warn(`[yb-pool] Error draining failed pool (${failedHost}): ${e.message}`)
    );

    // Try every other endpoint, starting after the failed one (round-robin).
    const remaining = [
      ...this._endpoints.slice(failedIdx + 1),
      ...this._endpoints.slice(0, failedIdx),
    ];

    for (let i = 0; i < remaining.length; i++) {
      const host = remaining[i];
      const origIdx = this._endpoints.indexOf(host);
      console.log(`[yb-pool] Failing over to endpoint [${origIdx}]: ${host}`);
      try {
        await this._probe(host);                  // plain pool probe, no loadBalance
        resetSmartDriverState();                   // force fresh cluster discovery
        const pool = this._makePool(host);        // real pool with loadBalance
        this._activatePool(pool, origIdx);
        console.log(`[yb-pool] Failover succeeded — now on endpoint [${origIdx}]: ${host}`);
        // optional post-failover prewarm to speed up subsequent queries:
        // await this.prewarm().catch(e =>
        //   console.warn(`[yb-pool] Post-failover prewarm failed: ${e.message}`)
        // );
        return;
      } catch (err) {
        if (!isConnectionError(err)) {
          console.error(`[yb-pool] Fatal during failover on [${origIdx}] ${host}: ${err.message}`);
          throw err;
        }
        console.warn(`[yb-pool] Endpoint [${origIdx}] ${host} unreachable: ${err.message}`);
      }
    }

    throw new Error(
      `[yb-pool] All endpoints exhausted after failure of ${failedHost}. ` +
      `Hosts tried: ${remaining.join(', ')}`
    );
  }
}

module.exports = { EndpointPoolManager };
```

> **Why multiple endpoints?** The smart driver only needs one live contact point to discover the
> cluster, but if that single bootstrap host is down at startup you never connect at all. Passing
> several endpoints (ideally one per fault domain) gives the manager a list to probe and fail over
> between, so startup and recovery survive the loss of any one contact node. The constructor
> requires **at least two** endpoints for this reason.

---

## Service Layer

The manager exposes the same surface as a `pg` `Pool` — `query()` for autocommit statements and
`withTransaction()` for multi-statement units — with retries and failover handled internally, so
service code stays clean.

```javascript
async function createKV(manager, key, value) {
  return manager.withTransaction(async (client) => {
    const result = await client.query(
      'INSERT INTO kvinfo(key, value) VALUES ($1, $2) RETURNING *',
      [key, value]
    );
    return result.rows[0];
  });
}

async function getAllKV(manager) {
  const { rows } = await manager.query('SELECT * FROM kvinfo');
  return rows;
}

async function getKV(manager, key) {
  const { rows } = await manager.query('SELECT * FROM kvinfo WHERE key = $1', [key]);
  return rows[0] || null;
}

async function deleteKV(manager, key) {
  return manager.withTransaction(async (client) => {
    await client.query('DELETE FROM kvinfo WHERE key = $1', [key]);
  });
}
```

> Single-statement reads/writes can go through `manager.query()` directly — it already wraps the call
> in retry + failover. Reserve `withTransaction()` for multi-statement units that must be atomic.

---

## Express REST API

```javascript
const express = require('express');
const { EndpointPoolManager } = require('./endpoint-pool-manager');

const manager = new EndpointPoolManager(
  ['127.0.0.1', '127.0.0.2', '127.0.0.3'],
  {
    port: 5433,
    database: 'yugabyte',
    user: 'yugabyte',
    password: 'yugabyte',
    loadBalance: 'any',
    max: 10,
    connectionTimeoutMillis: 5_000,
  }
);

const app = express();
app.use(express.json());

app.post('/v1/kvinfo', async (req, res) => {
  try {
    const { key, value } = req.body;
    res.status(201).json(await createKV(manager, key, value));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/v1/kvinfo', async (req, res) => {
  try {
    res.json(await getAllKV(manager));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/v1/kvinfo/:key', async (req, res) => {
  try {
    const row = await getKV(manager, req.params.key);
    if (!row) return res.status(404).json({ error: 'Not found' });
    res.json(row);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.delete('/v1/kvinfo/:key', async (req, res) => {
  try {
    await deleteKV(manager, req.params.key);
    res.status(204).end();
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// init() probes endpoints and activates a load-balanced pool BEFORE serving.
// prewarm() then opens sockets across the cluster so the first request is fast.
async function start() {
  await manager.init();
  await manager.prewarm();                       // pg ignores `min` — prewarm manually
  console.log(`Active endpoint: ${manager.activeEndpoint}`);
  app.listen(8080, () => console.log('Server on :8080'));
}

// Drain the pool cleanly on shutdown.
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => { await manager.end(); process.exit(0); });
}

start().catch(err => { console.error('Startup failed:', err.message); process.exit(1); });
```

---

## Standalone Usage

```javascript
const { EndpointPoolManager } = require('./endpoint-pool-manager');

async function main() {
  const manager = new EndpointPoolManager(
    ['127.0.0.1', '127.0.0.2', '127.0.0.3'],
    { port: 5433, database: 'yugabyte', user: 'yugabyte', password: 'yugabyte',
      loadBalance: 'any', max: 10, connectionTimeoutMillis: 5_000 }
  );

  await manager.init();
  await manager.prewarm();    // spread connections across the cluster via the smart driver

  const { rows } = await manager.query('SELECT now() AS ts');
  console.log('Server time:', rows[0].ts);

  const inserted = await manager.withTransaction(async (client) => {
    const r = await client.query(
      `INSERT INTO kvinfo(key, value) VALUES (gen_random_uuid(), $1) RETURNING *`,
      ['hello-yb']
    );
    return r.rows[0];
  });
  console.log('Inserted:', inserted);

  await manager.end();
}

main().catch(err => { console.error('Fatal:', err.message); process.exit(1); });
```

---

## Retryable vs Fatal Errors

The manager distinguishes two error classes. Understanding the split is key to tuning behaviour.

| Class | Detector | Action |
|---|---|---|
| **Connection error** | `isConnectionError()` — `ECONNREFUSED`/`ENOTFOUND`/`ETIMEDOUT`/`ECONNRESET`, SQL states `08006`/`08001`/`08004`, connection-timeout messages | Rotate to the next endpoint via `_failover()` |
| **Retryable query error** | `isRetryableQueryError()` — SQL states `40001`/`40P01`/`57P01`/`08006`, conditional `XX000` (schema mismatch / duplicate request), connection-closed/reset messages | Retry in place with exponential backoff |
| **Fatal** | anything else (auth failures, `pg_hba` rejections, bad SQL, constraint violations) | Surface immediately — never rotate or retry |

> `XX000` (`internal_error`) is retried **only** when the message matches a known-transient cause
> (`schema version mismatch`, `duplicate request`). Blanket-retrying `XX000` would mask real bugs.

The error-classification helpers (`isConnectionError`, `isRetryableQueryError`) walk the
`err.cause` / `err.original` chain so wrapped driver errors are still matched.

---

## TypeScript Notes

The manager translates cleanly to TypeScript. Type the public surface against `pg`'s exports:

```typescript
import { Pool, PoolClient, QueryResult } from '@yugabytedb/pg';

interface PoolOptions {
  port?: number;
  database?: string;
  user?: string;
  password?: string;
  loadBalance?: 'any' | 'only-primary' | 'only-rr' | boolean;
  topologyKeys?: string;
  min?: number;
  max?: number;
  idleTimeoutMillis?: number;
  connectionTimeoutMillis?: number;
  application_name?: string;
}

class EndpointPoolManager {
  constructor(endpoints: string[], poolOptions?: PoolOptions);
  init(): Promise<void>;
  connect(): Promise<PoolClient>;
  query<T extends Record<string, any> = any>(text: string, values?: any[]): Promise<QueryResult<T>>;
  withTransaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T>;
  withRetry<T>(fn: () => Promise<T>): Promise<T>;
  prewarm(count?: number): Promise<void>;
  end(): Promise<void>;
  get activeEndpoint(): string | null;
}
```

---

## Schema Migration (node-pg-migrate or manual)

```sql
-- migrations/001_create_kvinfo.sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE IF NOT EXISTS kvinfo (
    key   uuid DEFAULT uuid_generate_v4() PRIMARY KEY,
    value text
);
```

---

## Common Pitfalls (Node.js specific)

| Pitfall | Fix |
|---|---|
| Using plain `pg` instead of `@yugabytedb/pg` | No load balancing or topology routing — switch to the smart driver |
| Relying on `min` to pre-open connections | `pg-pool` ignores `min` — call `prewarm()` after `init()` |
| Probing endpoints with `loadBalance` on | Discovery can hang past `connectionTimeoutMillis` — probe with `loadBalance: false` |
| Retiring a pool without `resetSmartDriverState()` | Stale static topology cache leaks into the next pool — reset before activating |
| Treating every error as retryable | Separate connection errors (failover) from transient query errors (retry) from fatal (surface) |
| Single contact endpoint | If it's down at startup you never connect — pass ≥2 endpoints across fault domains |
