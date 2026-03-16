# Go — YugabyteDB Application Reference

---

## Dependencies

```bash
go get github.com/jackc/pgx/v5
go get github.com/jackc/pgx/v5/pgxpool
```

---

## Connection Pool Setup

```go
package main

import (
    "context"
    "fmt"
    "log"
    "time"

    "github.com/jackc/pgx/v5/pgxpool"
)

var (
    rwPool *pgxpool.Pool
)

func initPools(ctx context.Context) error {
    var err error

    // Read-Write pool
    rwConfig, err := pgxpool.ParseConfig(
        "postgresql://yugabyte:yugabyte@127.0.0.1:5433/yugabyte" +
        "?application_name=myapp-rw&connect_timeout=5&statement_timeout=15000",
    )
    if err != nil {
        return fmt.Errorf("pool config: %w", err)
    }
    rwConfig.MinConns = 3
    rwConfig.MaxConns = 3
    rwConfig.MaxConnIdleTime = 2 * time.Minute

    rwPool, err = pgxpool.NewWithConfig(ctx, rwConfig)
    if err != nil {
        return fmt.Errorf("pool create: %w", err)
    }
    return err
}
```

---

## Retry Logic

```go
package main

import (
    "context"
    "errors"
    "log"
    "math"
    "time"

    "github.com/jackc/pgx/v5/pgconn"
)

// SQL states warranting retry
var retrySQLStates = map[string]bool{
    "40001": true, // serialization_failure
    "40P01": true, // deadlock_detected
    "57P01": true, // admin_shutdown (node restart)
    "08006": true, // connection_failure
}

var retryXX000Messages = []string{"schema version mismatch", "duplicate request"}
var retryMessages      = []string{"connection is closed", "connection reset by peer"}

const (
    maxRetries     = 5
    initialBackoff = 200 * time.Millisecond
    maxBackoff     = 5 * time.Second
    backoffMult    = 3.0
)

func shouldRetry(err error) bool {
    if err == nil {
        return false
    }
    var pgErr *pgconn.PgError
    if errors.As(err, &pgErr) {
        if retrySQLStates[pgErr.Code] {
            return true
        }
        if pgErr.Code == "XX000" {
            msg := strings.ToLower(pgErr.Message)
            for _, m := range retryXX000Messages {
                if strings.Contains(msg, m) {
                    return true
                }
            }
        }
        msg := strings.ToLower(pgErr.Message)
        for _, m := range retryMessages {
            if strings.Contains(msg, m) {
                return true
            }
        }
    }
    return false
}

func withRetry(ctx context.Context, fn func(ctx context.Context) error) error {
    backoff := initialBackoff
    var lastErr error
    for attempt := 0; attempt < maxRetries; attempt++ {
        if err := fn(ctx); err != nil {
            lastErr = err
            if attempt < maxRetries-1 && shouldRetry(err) {
                log.Printf("Retrying (attempt %d/%d): %v", attempt+1, maxRetries, err)
                select {
                case <-time.After(backoff):
                case <-ctx.Done():
                    return ctx.Err()
                }
                backoff = time.Duration(math.Min(
                    float64(backoff)*backoffMult,
                    float64(maxBackoff),
                ))
                continue
            }
            return err
        }
        return nil
    }
    return lastErr
}
```

---

## Service Layer

```go
package main

import (
    "context"
    "fmt"

    "github.com/jackc/pgx/v5"
    "github.com/jackc/pgx/v5/pgxpool"
)

type KeyValue struct {
    Key   string `json:"key"`
    Value string `json:"value"`
}

func createKV(ctx context.Context, value string) (*KeyValue, error) {
    var kv KeyValue
    err := withRetry(ctx, func(ctx context.Context) error {
        return rwPool.AcquireFunc(ctx, func(conn *pgxpool.Conn) error {
            tx, err := conn.Begin(ctx)
            if err != nil {
                return err
            }
            defer tx.Rollback(ctx)

            row := tx.QueryRow(ctx,
                "INSERT INTO kvinfo(key, value) VALUES (gen_random_uuid(), $1) RETURNING key, value",
                value,
            )
            if err := row.Scan(&kv.Key, &kv.Value); err != nil {
                return err
            }
            return tx.Commit(ctx)
        })
    })
    if err != nil {
        return nil, fmt.Errorf("createKV: %w", err)
    }
    return &kv, nil
}

func getAllKV(ctx context.Context) ([]KeyValue, error) {
    var results []KeyValue
    err := withRetry(ctx, func(ctx context.Context) error {
        rows, err := rwPool.Query(ctx, "SELECT key, value FROM kvinfo")
        if err != nil {
            return err
        }
        defer rows.Close()

        results, err = pgx.CollectRows(rows, pgx.RowToStructByName[KeyValue])
        return err
    })
    if err != nil {
        return nil, fmt.Errorf("getAllKV: %w", err)
    }
    return results, nil
}

func deleteKV(ctx context.Context, key string) error {
    return withRetry(ctx, func(ctx context.Context) error {
        return rwPool.AcquireFunc(ctx, func(conn *pgxpool.Conn) error {
            tx, err := conn.Begin(ctx)
            if err != nil {
                return err
            }
            defer tx.Rollback(ctx)
            if _, err := tx.Exec(ctx, "DELETE FROM kvinfo WHERE key = $1", key); err != nil {
                return err
            }
            return tx.Commit(ctx)
        })
    })
}
```

---

## HTTP Handlers (net/http)

```go
package main

import (
    "encoding/json"
    "net/http"
)

func handleCreate(w http.ResponseWriter, r *http.Request) {
    var req struct{ Value string `json:"value"` }
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }
    kv, err := createKV(r.Context(), req.Value)
    if err != nil {
        http.Error(w, err.Error(), http.StatusInternalServerError)
        return
    }
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusCreated)
    json.NewEncoder(w).Encode(kv)
}

func handleGetAll(w http.ResponseWriter, r *http.Request) {
    kvs, err := getAllKV(r.Context())
    if err != nil {
        http.Error(w, err.Error(), http.StatusInternalServerError)
        return
    }
    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(kvs)
}

func main() {
    ctx := context.Background()
    if err := initPools(ctx); err != nil {
        log.Fatalf("pool init failed: %v", err)
    }
    defer rwPool.Close()

    mux := http.NewServeMux()
    mux.HandleFunc("POST /v1/kvinfo", handleCreate)
    mux.HandleFunc("GET /v1/kvinfo", handleGetAll)

    log.Fatal(http.ListenAndServe(":8080", mux))
}
```

---

## Schema Migration (golang-migrate)

```bash
go get github.com/golang-migrate/migrate/v4
```

`migrations/000001_create_kvinfo.up.sql`:
```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE TABLE IF NOT EXISTS kvinfo (
    key   uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    value text
) ;
```

`migrations/000001_create_kvinfo.down.sql`:
```sql
DROP TABLE IF EXISTS kvinfo;
```
