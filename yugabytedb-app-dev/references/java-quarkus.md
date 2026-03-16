# Java / Quarkus — YugabyteDB Application Reference

---

## Maven Dependencies

```xml
<!-- YugabyteDB JDBC driver (topology-aware, cluster-aware) -->
<dependency>
  <groupId>com.yugabyte</groupId>
  <artifactId>jdbc-yugabytedb</artifactId>
  <version>42.7.3-yb-4</version>
</dependency>

<!-- Quarkus JDBC PostgreSQL extension (brings Agroal pool) -->
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-jdbc-postgresql</artifactId>
</dependency>

<!-- Hibernate ORM with Panache (optional but idiomatic in Quarkus) -->
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-hibernate-orm-panache</artifactId>
</dependency>

<!-- SmallRye Fault Tolerance for @Retry -->
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-smallrye-fault-tolerance</artifactId>
</dependency>

<!-- RESTEasy Reactive for REST endpoints -->
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-rest</artifactId>
</dependency>
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-rest-jackson</artifactId>
</dependency>

<!-- Flyway for migrations -->
<dependency>
  <groupId>io.quarkus</groupId>
  <artifactId>quarkus-flyway</artifactId>
</dependency>
```

---

## application.properties

Quarkus uses Agroal (not HikariCP) as its connection pool. All datasource config lives in
`application.properties` / `application.yaml`.

```properties
# ---- Default (read-write) datasource ----
quarkus.datasource.db-kind=postgresql
quarkus.datasource.jdbc.driver=com.yugabyte.Driver
quarkus.datasource.jdbc.url=jdbc:yugabytedb://127.0.0.1:5433/yugabyte?load-balance=true&topology-keys=ybcloud.ap-south-1.ap-south-1c
quarkus.datasource.username=yugabyte
quarkus.datasource.password=yugabyte

# Agroal pool config
quarkus.datasource.jdbc.min-size=3
quarkus.datasource.jdbc.max-size=3
quarkus.datasource.jdbc.idle-removal-interval=2M
quarkus.datasource.jdbc.acquisition-timeout=15
quarkus.datasource.jdbc.initial-sql=prepare warmup as SELECT 1\; execute warmup\; commit

# ---- Hibernate ORM ----
quarkus.hibernate-orm.dialect=org.hibernate.dialect.PostgreSQLDialect
quarkus.hibernate-orm.datasource=<default>
quarkus.hibernate-orm.database.generation=none

# ---- Flyway ----
quarkus.flyway.migrate-at-start=true

# ---- CRITICAL: Retry interceptor priority ----
# By default @Retry priority (4000) is HIGHER than @Transactional priority (200).
# This means retries happen INSIDE the same transaction — the failed transaction is reused.
# Setting this below 200 ensures @Retry wraps OUTSIDE @Transactional so each retry
# gets a fresh transaction.
mp.fault.tolerance.interceptor.priority=100
```

---

## ⚠️ Critical: Interceptor Priority

This is the most important Quarkus-specific detail for YugabyteDB retry correctness.

By default in Quarkus/SmallRye:
- `@Retry` interceptor priority = **4000** (runs first / outermost)
- `@Transactional` interceptor priority = **200** (runs inside)

The execution order with defaults is:

```
Request → [@Retry, priority=4000] → [@Transactional, priority=200] → method
```

This means the `@Retry` interceptor fires **after** the transaction commits or rolls back.
For YugabyteDB `40001` (serialization failure), the error is thrown at **commit time**, which
happens inside the `@Transactional` boundary. The `@Retry` interceptor never sees it —
so retries **do not work** with the default priority ordering.

The fix is to lower the fault tolerance interceptor priority below `@Transactional`:

```properties
mp.fault.tolerance.interceptor.priority=100
```

This changes execution to:

```
Request → [@Retry, priority=100] → [@Transactional, priority=200] → method
```

Now `@Retry` wraps the entire transaction, and each retry attempt opens a **fresh transaction**.

**Alternative: two-bean pattern** (avoids global priority change)

```java
@ApplicationScoped
public class KVService {

    @Inject KVTransactionalService inner;

    // @Retry sits OUTSIDE the transaction — sees the commit-time error
    @Retry(maxRetries = 5, retryOn = YugabyteRetryException.class,
           delay = 200, jitter = 50, maxDuration = 30000)
    public KeyValue save(KeyValue kv) {
        return inner.save(kv);   // delegates into the @Transactional bean
    }
}

@ApplicationScoped
class KVTransactionalService {

    @Inject KVRepository repo;

    @Transactional
    KeyValue save(KeyValue kv) {   // package-private, not private (must be interceptable)
        return repo.save(kv);
    }
}
```

---

## Exception Classifier

SmallRye `@Retry` uses `retryOn` / `abortOn` lists. Since it can't express SQL-state logic directly,
create a custom exception type and a CDI interceptor or producer that translates YugabyteDB errors:

```java
package io.data;

/** Marker exception for retry-worthy YugabyteDB errors. */
public class YbRetryableException extends RuntimeException {
    public YbRetryableException(Throwable cause) { super(cause); }
}
```

```java
package io.data;

import jakarta.interceptor.AroundInvoke;
import jakarta.interceptor.Interceptor;
import jakarta.interceptor.InvocationContext;
import java.sql.SQLException;
import java.util.List;
import java.util.Set;

/**
 * Translates YugabyteDB transient SQL errors into YbRetryableException
 * so SmallRye @Retry can detect them via retryOn.
 *
 * Bind this interceptor to service methods using @YbTransactional (see below).
 */
@YbTransactional
@Interceptor
@jakarta.annotation.Priority(150)   // between @Retry(100) and @Transactional(200)
public class YbExceptionTranslatorInterceptor {

    private static final Set<String> RETRY_STATES = Set.of("40001","40P01","57P01","08006");
    private static final List<String> RETRY_XX000 = List.of("schema version mismatch","duplicate request");
    private static final List<String> RETRY_MSGS  = List.of("connection is closed","connection reset by peer");

    @AroundInvoke
    public Object translate(InvocationContext ctx) throws Exception {
        try {
            return ctx.proceed();
        } catch (Exception ex) {
            if (isRetryable(ex)) throw new YbRetryableException(ex);
            throw ex;
        }
    }

    private boolean isRetryable(Throwable t) {
        while (t != null) {
            if (t instanceof SQLException sql) {
                String state = sql.getSQLState();
                if (state != null && RETRY_STATES.contains(state)) return true;
                if ("XX000".equals(state)) {
                    String msg = (sql.getMessage() != null) ? sql.getMessage().toLowerCase() : "";
                    if (RETRY_XX000.stream().anyMatch(msg::contains)) return true;
                }
                String msg = (sql.getMessage() != null) ? sql.getMessage().toLowerCase() : "";
                if (RETRY_MSGS.stream().anyMatch(msg::contains)) return true;
            }
            t = t.getCause();
        }
        return false;
    }
}
```

```java
package io.data;

import jakarta.interceptor.InterceptorBinding;
import java.lang.annotation.*;

/** Binding annotation: marks a method for YB exception translation + retry. */
@InterceptorBinding
@Target({ElementType.TYPE, ElementType.METHOD})
@Retention(RetentionPolicy.RUNTIME)
public @interface YbTransactional {}
```

---

## Entity (Panache)

```java
package io.data;

import io.quarkus.hibernate.orm.panache.PanacheEntityBase;
import jakarta.persistence.*;
import org.hibernate.annotations.UuidGenerator;
import java.util.UUID;

@Entity
@Table(name = "kvinfo")
public class KeyValue extends PanacheEntityBase {

    @Id
    @UuidGenerator
    @Column(name = "key")
    public UUID key;

    @Column(name = "value")
    public String value;
}
```

---

## Repository

```java
package io.data;

import io.quarkus.hibernate.orm.panache.PanacheRepositoryBase;
import jakarta.enterprise.context.ApplicationScoped;
import java.util.UUID;

@ApplicationScoped
public class KVRepository implements PanacheRepositoryBase<KeyValue, UUID> {
    // All CRUD inherited; add custom queries as needed
}
```

---

## Service Layer

```java
package io.data;

import io.smallrye.faulttolerance.api.ExponentialBackoff;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import jakarta.transaction.Transactional;
import org.eclipse.microprofile.faulttolerance.Fallback;
import org.eclipse.microprofile.faulttolerance.Retry;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

@ApplicationScoped
public class KVService {

    @Inject
    KVRepository repository;

    // @Retry wraps @Transactional because mp.fault.tolerance.interceptor.priority=100
    @Retry(maxRetries = 5, retryOn = YbRetryableException.class,
           delay = 200, jitter = 50, maxDuration = 30000)
    @YbTransactional   // translates SQL errors → YbRetryableException (priority 150)
    @Transactional     // manages transaction boundary (priority 200)
    public KeyValue save(KeyValue kv) {
        repository.persist(kv);
        return kv;
    }

    @Retry(maxRetries = 5, retryOn = YbRetryableException.class,
           delay = 200, jitter = 50, maxDuration = 30000)
    @YbTransactional
    @Transactional(Transactional.TxType.SUPPORTS)
    public Optional<KeyValue> getKey(UUID id) {
        return repository.findByIdOptional(id);
    }

    @Retry(maxRetries = 5, retryOn = YbRetryableException.class,
           delay = 200, jitter = 50, maxDuration = 30000)
    @YbTransactional
    @Transactional(Transactional.TxType.SUPPORTS)
    public List<KeyValue> getAllKeys() {
        return repository.listAll();
    }

    @Retry(maxRetries = 5, retryOn = YbRetryableException.class,
           delay = 200, jitter = 50, maxDuration = 30000)
    @YbTransactional
    @Transactional
    public void deleteKey(UUID key) {
        repository.deleteById(key);
    }
}
```

---

## REST Resource

```java
package io.data;

import jakarta.inject.Inject;
import jakarta.ws.rs.*;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.UUID;

@Path("/v1/kvinfo")
@Produces(MediaType.APPLICATION_JSON)
@Consumes(MediaType.APPLICATION_JSON)
public class KVResource {

    @Inject
    KVService service;

    @POST
    public Response create(KeyValue kv) {
        return Response.status(201).entity(service.save(kv)).build();
    }

    @GET
    public Response getAll() {
        return Response.ok(service.getAllKeys()).build();
    }

    @GET
    @Path("/{key}")
    public Response get(@PathParam("key") UUID key) {
        return service.getKey(key)
            .map(kv -> Response.ok(kv).build())
            .orElse(Response.status(404).build());
    }

    @DELETE
    @Path("/{key}")
    public Response delete(@PathParam("key") UUID key) {
        service.deleteKey(key);
        return Response.noContent().build();
    }
}
```

---

## Flyway Migration Scripts

Place in `src/main/resources/db/migration/`:

**V1_0_0__create.sql:**
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE IF NOT EXISTS kvinfo (
    key   uuid DEFAULT uuid_generate_v4() PRIMARY KEY,
    value text
) ;
```

**V1_0_1__data.sql:**
```sql
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Quarkus');
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'YugabyteDB');
```

---

## Key Quarkus-Specific Notes

| Topic | Guidance |
|---|---|
| Pool implementation | Quarkus uses **Agroal**, not HikariCP. Config keys are `quarkus.datasource.jdbc.*` |
| Custom JDBC driver | Set `jdbc.driver=com.yugabyte.Driver` explicitly — Quarkus won't auto-detect it from the YB URL |
| `initial-sql` escaping | Semicolons in `application.properties` must be escaped as `\;` |
| Native image | The YugabyteDB driver is not compatible with GraalVM native compilation — use JVM mode |
| `@Retry` + `@Transactional` ordering | **Must** set `mp.fault.tolerance.interceptor.priority=100` or use the two-bean pattern |
| Exception cause chain | SmallRye non-compatible mode (default in Quarkus) walks the cause chain automatically for `retryOn` matching |
| Dev Services | In dev mode Quarkus auto-starts a PostgreSQL container — disable with `quarkus.datasource.devservices.enabled=false` when pointing to a real YugabyteDB |
