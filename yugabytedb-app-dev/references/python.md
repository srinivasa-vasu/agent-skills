# Python — YugabyteDB Application Reference

---

## Dependencies

```bash
pip install psycopg2-binary       # sync
pip install asyncpg               # async (FastAPI, etc.)
pip install sqlalchemy            # ORM (works with both drivers)
pip install alembic               # migrations
```

---

## Connection String

```python
DSN = "postgresql://yugabyte:yugabyte@127.0.0.1:5433/yugabyte?load_balance=true"
```

Note: The YugabyteDB psycopg2 driver is compatible with the standard `psycopg2` library.
For topology-aware load balancing, use the `yb-psycopg2` package or append `load_balance=true`.

---

## Retry Logic (psycopg2)

```python
import psycopg2
import psycopg2.errors
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

# SQL states to retry
RETRY_SQL_STATES = {"40001", "40P01", "57P01", "08006"}
# XX000 with specific messages
RETRY_XX000_MESSAGES = ["schema version mismatch", "duplicate request"]
# Message-based retry
RETRY_MESSAGES = ["connection is closed", "connection reset by peer"]

MAX_RETRIES = 5
INITIAL_BACKOFF = 0.2   # seconds
MAX_BACKOFF = 5.0
MULTIPLIER = 3

def should_retry(exc: Exception) -> bool:
    """Determine if an exception warrants a retry."""
    cause = exc
    while cause is not None:
        if isinstance(cause, psycopg2.Error):
            state = cause.pgcode
            if state in RETRY_SQL_STATES:
                return True
            if state == "XX000":
                msg = str(cause).lower()
                if any(m in msg for m in RETRY_XX000_MESSAGES):
                    return True
            msg = str(cause).lower()
            if any(m in msg for m in RETRY_MESSAGES):
                return True
        cause = cause.__cause__ or cause.__context__
    return False

def with_retry(func):
    """Decorator that retries a function on transient YugabyteDB errors."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        backoff = INITIAL_BACKOFF
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if attempt < MAX_RETRIES - 1 and should_retry(exc):
                    logger.warning(
                        "Retrying after transient error (attempt %d/%d): %s",
                        attempt + 1, MAX_RETRIES, exc
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * MULTIPLIER, MAX_BACKOFF)
                else:
                    raise
    return wrapper
```

---

## Connection Pool (psycopg2 + psycopg2.pool)

```python
from psycopg2 import pool

rw_pool = pool.ThreadedConnectionPool(
    minconn=3,
    maxconn=3,
    dsn="postgresql://yugabyte:yugabyte@127.0.0.1:5433/yugabyte"
        "?load_balance=true&application_name=myapp-rw"
        "&connect_timeout=5&options=-c%20statement_timeout%3D15000"
)
```

---

## Service Layer Example

```python
import psycopg2.extras

@with_retry
def create_kv(key: str, value: str) -> dict:
    conn = rw_pool.getconn()
    try:
        with conn:  # context manager: commits on exit, rolls back on exception
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO kvinfo(key, value) VALUES (%s, %s) RETURNING *",
                    (key, value)
                )
                return dict(cur.fetchone())
    finally:
        rw_pool.putconn(conn)

@with_retry
def get_all_kv() -> list:
    conn = rw_pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kvinfo")
            return [dict(row) for row in cur.fetchall()]
    finally:
        rw_pool.putconn(conn)
```

---

## Async (asyncpg + FastAPI)

```python
import asyncpg
import asyncio
from fastapi import FastAPI

app = FastAPI()
pool: asyncpg.Pool = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        dsn="postgresql://yugabyte:yugabyte@127.0.0.1:5433/yugabyte",
        min_size=3,
        max_size=10,
        command_timeout=15,
        server_settings={"application_name": "yb-fastapi"}
    )

RETRY_STATES = {"40001", "40P01", "57P01", "08006"}

async def execute_with_retry(fn, max_retries=5):
    backoff = 0.2
    for attempt in range(max_retries):
        try:
            return await fn()
        except asyncpg.PostgresError as exc:
            if attempt < max_retries - 1 and exc.sqlstate in RETRY_STATES:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 3, 5.0)
            else:
                raise

@app.post("/v1/kvinfo")
async def create_kv(key: str, value: str):
    async def op():
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await conn.fetchrow(
                    "INSERT INTO kvinfo(key, value) VALUES ($1, $2) RETURNING *",
                    key, value
                )
    row = await execute_with_retry(op)
    return dict(row)

@app.get("/v1/kvinfo")
async def get_all():
    async def op():
        async with pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM kvinfo")
    rows = await execute_with_retry(op)
    return [dict(r) for r in rows]
```

---

## SQLAlchemy Integration

```python
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

engine = create_engine(
    "postgresql+psycopg2://yugabyte:yugabyte@127.0.0.1:5433/yugabyte",
    pool_size=3,
    max_overflow=0,
    pool_pre_ping=True,          # validates connections before use
    connect_args={
        "connect_timeout": 5,
        "application_name": "yb-sqlalchemy"
    }
)

Session = sessionmaker(bind=engine, autocommit=False)

@with_retry
def upsert_kv(key: str, value: str):
    with Session() as session:
        with session.begin():
            # your ORM operations here
            pass
```

---

## Alembic Migration

`alembic/versions/001_create_kvinfo.py`:

```python
def upgrade():
    op.execute("""
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        CREATE TABLE IF NOT EXISTS kvinfo (
            key   uuid PRIMARY KEY,
            value text
        ) ;
    """)

def downgrade():
    op.execute("DROP TABLE IF EXISTS kvinfo")
```
