# Java / Spring Boot Reactive (WebFlux + R2DBC) — YugabyteDB Reference

Source: https://github.com/srinivasa-vasu/yb-multids (branch: templater)

Spring WebFlux + Spring Data R2DBC provides a fully non-blocking reactive stack.
R2DBC replaces JDBC with async database access; WebFlux replaces Spring MVC with
reactive HTTP handling. The retry `Retry` spec is a Spring bean injected into the
controller, which applies `.retryWhen(retrySpec)` on each reactive pipeline.

---

## Maven Dependencies

```xml
<dependencies>
  <!-- Spring WebFlux (reactive HTTP) -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-webflux</artifactId>
  </dependency>

  <!-- Spring Data R2DBC (reactive database access) -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-data-r2dbc</artifactId>
  </dependency>

  <!-- YugabyteDB R2DBC driver (yugabyte fork of r2dbc-postgresql) -->
  <dependency>
    <groupId>com.yugabyte</groupId>
    <artifactId>r2dbc-postgresql</artifactId>
    <version>1.1.0-yb-1</version>
  </dependency>

  <!-- Flyway — runs over JDBC at startup for schema migrations -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-flyway</artifactId>
  </dependency>
  <dependency>
    <groupId>org.flywaydb</groupId>
    <artifactId>flyway-database-postgresql</artifactId>
  </dependency>
  <!-- Flyway needs a JDBC driver even in an R2DBC app -->
  <dependency>
    <groupId>com.yugabyte</groupId>
    <artifactId>jdbc-yugabytedb</artifactId>
    <version>42.7.3-yb-4</version>
  </dependency>

  <!-- AspectJ for AOP (if needed alongside reactive stack) -->
  <dependency>
    <groupId>org.aspectj</groupId>
    <artifactId>aspectjweaver</artifactId>
  </dependency>

  <!-- Actuator for health/metrics -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
  </dependency>

  <dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
  </dependency>
</dependencies>
```

---

## application.yaml

```yaml
spring:
  application:
    name: yb-spring-boot-app
  threads:
    virtual:
      enabled: true

  retry:
    initial-interval: 200    # ms — first backoff
    max-interval: 5000       # seconds — cap on backoff (used as Duration in RetryConfigSpec)
    multiplier: 2.0          # exponential growth factor
    max-attempts: 3          # maximum retry attempts
    jitter: 0.2              # jitter as a fraction (0.0–1.0)

  r2dbc:
    name: yb-r2dbc-app
    url: r2dbc:postgresql://127.0.0.1:5433/yugabyte?loadBalance=true&topologyKeys=ybcloud.ap-south-1.ap-south-1c
    username: yugabyte
    password: yugabyte
    pool:
      initial-size: 3
      max-size: 3
      max-idle-time: 2m
      validation-query: SELECT 1
      max-acquire-time: 15s
    properties:
      applicationName: yb-r2dbc-app
      tcpKeepAlive: true

  # Flyway uses JDBC — point at the same cluster
  flyway:
    url: jdbc:yugabytedb://127.0.0.1:5433/yugabyte?load-balance=true
    user: yugabyte
    password: yugabyte
```

### R2DBC URL Format

```
r2dbc:postgresql://<host>:<port>/<db>?loadBalance=true&topologyKeys=<cloud.region.zone>
```

Uses the `r2dbc:postgresql://` scheme with the YugabyteDB fork of the r2dbc-postgresql driver
(`com.yugabyte:r2dbc-postgresql`). Spring Boot auto-configures the `ConnectionFactory` and
connection pool from `spring.r2dbc.*`.

---

## RetryConfigSpec.java — Reactive Retry Bean

`RetryConfigSpec` is both a `@ConfigurationProperties` binder and a `@Configuration` class.
It exposes a `Retry` bean (Reactor's `reactor.util.retry.Retry`) driven by `spring.retry.*`
properties. The same SQL-state classification logic as the blocking stack is applied, adapted
for `R2dbcException` instead of `SQLException`.

```java
package io.data;

import io.r2dbc.spi.R2dbcException;
import java.sql.SQLRecoverableException;
import java.sql.SQLTransientConnectionException;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Predicate;
import lombok.Data;
import org.jspecify.annotations.NonNull;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.dao.TransientDataAccessException;
import reactor.util.retry.Retry;

@ConfigurationProperties(prefix = "spring.retry")
@Configuration
@Data
public class RetryConfigSpec {

  private int maxInterval;
  private int initialInterval;
  private double multiplier;
  private int maxAttempts;
  private double jitter;              // fraction, e.g. 0.2 = ±20% randomisation

  // SQL states that ALWAYS warrant a retry
  private static final String SQL_STATE = "^(40001)|(40P01)|(57P01)|(08006)";

  // Message-based retry for OOM/crash mid-transaction
  private static final String SQL_MSG = "^(connection is closed)|(connection reset by peer)";

  // XX000 (internal_error) — only retry for these specific sub-cases
  private final Map<String, List<String>> specialCodes =
      Map.of("XX000", List.of("schema version mismatch", "duplicate request"));

  private final Predicate<R2dbcException> sqlStatePredicate =
      exception -> {
        String state = exception.getSqlState();
        if (state == null) return false;
        if (state.matches(SQL_STATE)) return true;
        if (specialCodes.containsKey(state)) {
          String msg = exception.getMessage();
          if (msg != null) {
            return specialCodes.get(state).stream()
                .anyMatch(code -> msg.toLowerCase().contains(code));
          }
        }
        return false;
      };

  private final Predicate<R2dbcException> sqlMsgPredicate =
      exception ->
          Optional.ofNullable(exception.getMessage())
              .filter(msg -> msg.toLowerCase().matches(SQL_MSG))
              .isPresent();

  private final Predicate<Throwable> exceptionPredicate =
      exception ->
          (exception instanceof SQLRecoverableException
              || exception instanceof SQLTransientConnectionException
              || exception instanceof TransientDataAccessException);

  public boolean shouldRetry(@NonNull Throwable cause) {
    // Walk the cause chain — retryable exception may be wrapped
    do {
      if ((cause instanceof R2dbcException exception
          && (sqlStatePredicate.or(sqlMsgPredicate).test(exception)))) {
        return true;
      }
      cause = cause.getCause();
    } while (cause != null);
    return false;
  }

  @Bean
  public Retry retrySpec() {
    return Retry.backoff(maxAttempts, Duration.ofMillis(initialInterval))
        .multiplier(multiplier)
        .maxBackoff(Duration.ofSeconds(maxInterval))
        .jitter(jitter)
        .filter(this::shouldRetry)
        .onRetryExhaustedThrow((spec, signal) -> signal.failure());
  }
}
```

---

## YBApplication.java — Entry Point

```java
package io.data;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(proxyBeanMethods = false)
public class YBApplication {

  public static void main(String[] args) {
    SpringApplication.run(YBApplication.class, args);
  }
}
```

---

## KeyValue.java — R2DBC Entity

```java
package io.data;

import java.util.UUID;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;
import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Table;

@Table(name = "kvinfo")
@Data
@AllArgsConstructor
@NoArgsConstructor
public class KeyValue {
  @Id UUID key;    // org.springframework.data.annotation.@Id — NOT jakarta.persistence.@Id
  String value;
}
```

> **Note:** R2DBC uses `org.springframework.data.annotation.@Id`. Leave `key` null on insert —
> the DB default (`uuid_generate_v4()`) generates the UUID server-side.

---

## KVRepository.java

```java
package io.data;

import org.springframework.data.r2dbc.repository.R2dbcRepository;
import org.springframework.stereotype.Repository;

@Repository
public interface KVRepository extends R2dbcRepository<KeyValue, String> {}
```

---

## KVService.java — Reactive Service

The service contains only business logic and transaction management. **No retry here.**
Retry is applied at the controller level via the injected `Retry` bean.

```java
package io.data;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

@Service
public class KVService {

  private final KVRepository repository;

  public KVService(KVRepository repository) {
    this.repository = repository;
  }

  @Transactional
  public Mono<KeyValue> save(KeyValue resource) {
    return repository.save(resource);
  }

  @Transactional(readOnly = true)
  public Mono<KeyValue> getKey(String id) {
    return repository.findById(id);
  }

  @Transactional(readOnly = true)
  public Flux<KeyValue> getAllKeys() {
    return repository.findAll();
  }

  @Transactional
  public Mono<Void> deleteKey(String key) {
    return repository.deleteById(key);
  }
}
```

---

## KVController.java — WebFlux Controller with Retry

The injected `Retry` bean (from `RetryConfigSpec`) is applied via `.retryWhen(retrySpec)`
on every reactive pipeline. This is where retry actually fires.

```java
package io.data;

import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.util.retry.Retry;

@RestController
@RequestMapping("/v1/kvinfo")
public class KVController {

  private final KVService kvService;
  private final Retry retrySpec;

  public KVController(KVService kvService, Retry retrySpec) {
    this.kvService = kvService;
    this.retrySpec = retrySpec;
  }

  @PostMapping
  public Mono<KeyValue> saveKey(@RequestBody KeyValue info) {
    return kvService.save(info).retryWhen(retrySpec);
  }

  @PutMapping
  public Mono<KeyValue> updateKey(@RequestBody KeyValue info) {
    return kvService.save(info).retryWhen(retrySpec);
  }

  @GetMapping("/{key}")
  public Mono<KeyValue> getKey(@PathVariable String key) {
    return kvService.getKey(key).retryWhen(retrySpec);
  }

  @GetMapping
  public Flux<KeyValue> getAllKeys() {
    return kvService.getAllKeys().retryWhen(retrySpec);
  }

  @DeleteMapping("/{key}")
  public Mono<Void> deleteKey(@PathVariable String key) {
    return kvService.deleteKey(key).retryWhen(retrySpec);
  }
}
```

---

## Flyway Migration Scripts

Flyway uses JDBC (not R2DBC) — runs once at startup before the reactive app begins serving.

**V1_0_0__create.sql:**
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE IF NOT EXISTS kvinfo (
    key   uuid PRIMARY KEY,
    value text
) ;
```

**V1_0_1__data.sql:**
```sql
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Spring Boot');
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Yugabyte YFTT');
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Yugabyte University');
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Yugabyte Blogs');
INSERT INTO kvinfo(key, value) VALUES (uuid_generate_v4(), 'Yugabyte Community Slack');
```

---

## Key Files Summary

| File | Purpose |
|---|---|
| `RetryConfigSpec.java` | `@ConfigurationProperties` + `@Configuration`; owns SQL-state retry logic; exposes `Retry` bean |
| `KVController.java` | Applies `.retryWhen(retrySpec)` on every reactive pipeline |
| `KVService.java` | Pure service logic with `@Transactional` — no retry here |
| `KVRepository.java` | Extends `R2dbcRepository` |
| `application.yaml` | R2DBC pool + retry config |

---

## Key Reactive-Specific Notes

| Topic | Guidance |
|---|---|
| R2DBC driver | `com.yugabyte:r2dbc-postgresql` (YugabyteDB fork) — use `r2dbc:postgresql://` URL scheme |
| Retry placement | `.retryWhen(retrySpec)` in the **controller** — wraps the full reactive pipeline including the transaction |
| `RetryConfigSpec` | Single class handles both config binding and `Retry` bean creation |
| `jitter` type | `double` in the YAML (e.g. `0.2`) — fraction, not milliseconds (unlike blocking stack) |
| `@Transactional` scope | Works reactively via `ReactiveTransactionManager` auto-configured by Spring Boot |
| Flyway + R2DBC | Flyway requires JDBC; include both `jdbc-yugabytedb` and `flyway-database-postgresql` |
