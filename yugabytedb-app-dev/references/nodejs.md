# Node.js — YugabyteDB Application Reference

---

## Dependencies

```bash
npm install pg               # node-postgres (works with YugabyteDB's PostgreSQL compatibility)
npm install pg-pool          # connection pooling (included in pg)
```

For TypeScript:
```bash
npm install --save-dev @types/pg
```

---

## Connection

```javascript
const { Pool } = require('pg');

const rwPool = new Pool({
  host: '127.0.0.1',
  port: 5433,
  database: 'yugabyte',
  user: 'yugabyte',
  password: 'yugabyte',
  min: 3,
  max: 3,
  idleTimeoutMillis: 120000,
  connectionTimeoutMillis: 15000,
  statement_timeout: 15000,
  application_name: 'myapp-rw',
});

```

---

## Retry Logic

```javascript
const RETRY_SQL_STATES = new Set(['40001', '40P01', '57P01', '08006']);
const RETRY_XX000_MESSAGES = ['schema version mismatch', 'duplicate request'];
const RETRY_MESSAGES = ['connection is closed', 'connection reset by peer'];

const MAX_RETRIES = 5;
const INITIAL_BACKOFF_MS = 200;
const MAX_BACKOFF_MS = 5000;
const MULTIPLIER = 3;

function shouldRetry(err) {
  // Walk cause chain
  let cause = err;
  while (cause) {
    const code = cause.code;
    if (code && RETRY_SQL_STATES.has(code)) return true;
    if (code === 'XX000') {
      const msg = (cause.message || '').toLowerCase();
      if (RETRY_XX000_MESSAGES.some(m => msg.includes(m))) return true;
    }
    const msg = (cause.message || '').toLowerCase();
    if (RETRY_MESSAGES.some(m => msg.includes(m))) return true;
    cause = cause.cause || cause.original;
  }
  return false;
}

async function withRetry(fn) {
  let backoff = INITIAL_BACKOFF_MS;
  let lastErr;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (attempt < MAX_RETRIES - 1 && shouldRetry(err)) {
        console.warn(`Retrying (attempt ${attempt + 1}/${MAX_RETRIES}): ${err.message}`);
        await new Promise(resolve => setTimeout(resolve, backoff));
        backoff = Math.min(backoff * MULTIPLIER, MAX_BACKOFF_MS);
      } else {
        throw err;
      }
    }
  }
  throw lastErr;
}
```

---

## Service Layer

```javascript
async function createKV(key, value) {
  return withRetry(async () => {
    const client = await rwPool.connect();
    try {
      await client.query('BEGIN');
      const result = await client.query(
        'INSERT INTO kvinfo(key, value) VALUES ($1, $2) RETURNING *',
        [key, value]
      );
      await client.query('COMMIT');
      return result.rows[0];
    } catch (err) {
      await client.query('ROLLBACK');
      throw err;
    } finally {
      client.release();
    }
  });
}

async function getAllKV() {
  return withRetry(async () => {
    const result = await rwPool.query('SELECT * FROM kvinfo');
    return result.rows;
  });
}

async function getKV(key) {
  return withRetry(async () => {
    const result = await rwPool.query(
      'SELECT * FROM kvinfo WHERE key = $1',
      [key]
    );
    return result.rows[0] || null;
  });
}

async function deleteKV(key) {
  return withRetry(async () => {
    const client = await rwPool.connect();
    try {
      await client.query('BEGIN');
      await client.query('DELETE FROM kvinfo WHERE key = $1', [key]);
      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      throw err;
    } finally {
      client.release();
    }
  });
}
```

---

## Express REST API

```javascript
const express = require('express');
const app = express();
app.use(express.json());

app.post('/v1/kvinfo', async (req, res) => {
  try {
    const { key, value } = req.body;
    const row = await createKV(key, value);
    res.status(201).json(row);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/v1/kvinfo', async (req, res) => {
  try {
    res.json(await getAllKV());
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/v1/kvinfo/:key', async (req, res) => {
  try {
    const row = await getKV(req.params.key);
    if (!row) return res.status(404).json({ error: 'Not found' });
    res.json(row);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.delete('/v1/kvinfo/:key', async (req, res) => {
  try {
    await deleteKV(req.params.key);
    res.status(204).end();
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.listen(8080, () => console.log('Server on :8080'));
```

---

## TypeScript Version

```typescript
import { Pool, PoolClient } from 'pg';

const RETRY_SQL_STATES = new Set(['40001', '40P01', '57P01', '08006']);

async function withRetry<T>(fn: () => Promise<T>, maxRetries = 5): Promise<T> {
  let backoff = 200;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err: any) {
      if (attempt < maxRetries - 1 && err?.code && RETRY_SQL_STATES.has(err.code)) {
        await new Promise(r => setTimeout(r, backoff));
        backoff = Math.min(backoff * 3, 5000);
      } else {
        throw err;
      }
    }
  }
  throw new Error('Max retries exceeded');
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
) ;
```
