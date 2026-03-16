# Java / Spring Boot — YugabyteDB Reference Implementation

Source: https://github.com/srinivasa-vasu/yb-multids (branch: template)

---

## Maven Dependencies

```xml
<dependencies>
  <!-- Spring Boot JPA (brings Hibernate + HikariCP) -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-data-jpa</artifactId>
  </dependency>

  <!-- YugabyteDB JDBC driver (topology-aware, cluster-aware) -->
  <dependency>
    <groupId>com.yugabyte</groupId>
    <artifactId>jdbc-yugabytedb</artifactId>
    <version>42.7.3-yb-4</version>
  </dependency>

  <!-- Spring Web -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-web</artifactId>
  </dependency>

  <!-- Flyway for schema migrations (PostgreSQL dialect) -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-flyway</artifactId>
  </dependency>
  <dependency>
    <groupId>org.flywaydb</groupId>
    <artifactId>flyway-database-postgresql</artifactId>
  </dependency>

  <!-- Lombok for boilerplate reduction -->
  <dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
    <optional>true</optional>
  </dependency>
</dependencies>
```

> **Note:** No `spring-retry` dependency is needed. Spring Framework 7 (Boot 4.x) ships
> `org.springframework.core.retry.RetryTemplate` and `RetryPolicy` built-in.

---

## application.yaml

```yaml
spring:
  application:
    name: yb-spring-boot-app
  threads:
    virtual:
      enabled: true          # Java 21+ virtual threads work well with YB

  retry:
    initial-interval: 200    # ms — first backoff
    max-interval: 5000       # ms — cap on backoff
    multiplier: 3            # exponential growth factor
    max-attempts: 3          # maximum retry attempts
    jitter: 100              # ms — jitter added to each backoff interval

  jpa:
    database-platform: org.hibernate.dialect.PostgreSQLDialect
    open-in-view: false      # always false for YB — avoid lazy loading anti-pattern

  datasource:
    driver-class-name: com.yugabyte.Driver
    url: jdbc:yugabytedb://127.0.0.1:5433/yugabyte?load-balance=true&topology-keys=ybcloud.ap-south-1.ap-south-1c
    username: yugabyte
    password: yugabyte
    hikari:
      pool-name: yb-pool
      minimum-idle: 2
      maximum-pool-size: 2
      auto-commit: false                   # CRITICAL: always false
      keepalive-time: 120000
      connection-init-sql: "begin read only; prepare warmup as SELECT 1; execute warmup; commit;"
      connection-timeout: 15000
      data-source-properties:
        ApplicationName: yb-spring-boot-app
        socketTimeout: 15                  # seconds — critical for node failure detection
        yb-servers-refresh-interval: 180   # seconds — refreshes cluster topology
        loginTimeout: 10
        connectTimeout: 5
```

---

## KVRetryPolicy.java — Full Annotated Implementation

Implements `org.springframework.core.retry.RetryPolicy` (Spring Framework 7 built-in).
Bundles its own `ExponentialBackOff` configured from `spring.retry.*` properties.

```java
package io.data;

import io.data.YBApplication.RetryPropertyConfig;
import java.sql.SQLException;
import java.sql.SQLRecoverableException;
import java.sql.SQLTransientConnectionException;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Predicate;
import org.hibernate.TransactionException;
import org.jspecify.annotations.NonNull;
import org.springframework.core.retry.RetryPolicy;
import org.springframework.dao.TransientDataAccessException;
import org.springframework.stereotype.Component;
import org.springframework.util.backoff.BackOff;
import org.springframework.util.backoff.ExponentialBackOff;

@Component
public class KVRetryPolicy implements RetryPolicy {

  @lombok.Getter private final BackOff backOff;

  // Backoff is wired here from config — YBApplication just passes RetryPropertyConfig
  public KVRetryPolicy(RetryPropertyConfig config) {
    ExponentialBackOff backOff = new ExponentialBackOff();
    backOff.setInitialInterval(config.getInitialInterval());
    backOff.setMultiplier(config.getMultiplier());
    backOff.setMaxInterval(config.getMaxInterval());
    backOff.setMaxAttempts(config.getMaxAttempts());
    backOff.setJitter(config.getJitter());
    this.backOff = backOff;
  }

  // SQL states that ALWAYS warrant a retry:
  // 40001 — optimistic concurrency / serialization_failure (most common in YB)
  // 40P01 — deadlock detected
  // 08006 — connection failure (socket timeout, kill -9 on a node)
  // 57P01 — admin_shutdown (node restart, kill -15)
  private final String SQL_STATE = "^(40001)|(40P01)|(57P01)|(08006)";

  // Message-based retry for OOM/crash mid-transaction scenarios
  private final String SQL_MSG = "^(connection is closed)|(connection reset by peer)";

  // XX000 (internal_error) must NOT be retried blindly — only retry specific sub-cases
  private final Map<String, List<String>> specialCodes =
      Map.of("XX000", List.of("schema version mismatch", "duplicate request"));

  private final Predicate<SQLException> sqlStatePredicate =
      exception -> {
        String state = exception.getSQLState();
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

  private final Predicate<SQLException> sqlMsgPredicate =
      exception ->
          Optional.ofNullable(exception.getMessage())
              .filter(msg -> msg.toLowerCase().matches(SQL_MSG))
              .isPresent();

  // Covers non-SQL transient errors:
  // SQLTransientConnectionException — 08001/08003 (connection refused, pool timeout)
  // TransactionException — begin/commit/rollback failures at Hibernate level
  private final Predicate<Throwable> exceptionPredicate =
      exception ->
          (exception instanceof SQLRecoverableException
              || exception instanceof SQLTransientConnectionException
              || exception instanceof TransientDataAccessException
              || exception instanceof TransactionException);

  @Override
  public boolean shouldRetry(@NonNull Throwable cause) {
    // Walk the cause chain — the retry-worthy exception may be wrapped several levels deep
    do {
      if (exceptionPredicate.test(cause)
          || (cause instanceof SQLException exception
              && (sqlStatePredicate.or(sqlMsgPredicate).test(exception)))) {
        return true;
      }
      cause = cause.getCause();
    } while (cause != null);
    return false;
  }
}
```

---

## DataSourceAspect.java — AOP Retry Wrapper

Wraps every `@Service` method with `RetryTemplate`. Retry logic is transparent to service code.

```java
package io.data;

import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.slf4j.Logger;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.core.retry.RetryTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@Aspect
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)  // run BEFORE @Transactional so retries get fresh transactions
public class DataSourceAspect {

  private final RetryTemplate retryTemplate;
  private static final Logger log = org.slf4j.LoggerFactory.getLogger(DataSourceAspect.class);

  public DataSourceAspect(RetryTemplate retryTemplate) {
    this.retryTemplate = retryTemplate;
  }

  @Around(
      "@annotation(org.springframework.stereotype.Service) || @within(org.springframework.stereotype.Service)")
  public Object wrapper(ProceedingJoinPoint pjp) throws Throwable {
    if (log.isDebugEnabled())
      log.debug("Is Txn active?: {}", TransactionSynchronizationManager.isSynchronizationActive());
    return retryTemplate.execute(pjp::proceed);
  }
}
```

**Important:** `@Order(Ordered.HIGHEST_PRECEDENCE)` ensures the retry aspect wraps the transaction
interceptor. Each retry attempt opens a **fresh transaction** rather than reusing the failed one.

---

## YBApplication.java — Entry Point and RetryTemplate Bean

```java
package io.data;

import lombok.Data;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.retry.RetryTemplate;

@SpringBootApplication(proxyBeanMethods = false)
public class YBApplication {

  public static void main(String[] args) {
    SpringApplication.run(YBApplication.class, args);
  }

  // Bind retry config from application.yaml spring.retry.*
  @ConfigurationProperties(prefix = "spring.retry")
  @Configuration
  @Data
  public static class RetryPropertyConfig {
    private int maxInterval;
    private int initialInterval;
    private int multiplier;
    private int maxAttempts;
    private int jitter;
  }

  @Bean
  public RetryTemplate retryTemplate(KVRetryPolicy retryPolicy) {
    RetryTemplate retryTemplate = new RetryTemplate();
    retryTemplate.setRetryPolicy(retryPolicy);
    // Backoff is embedded in KVRetryPolicy — no separate BackOffPolicy needed here
    return retryTemplate;
  }
}
```

---

## KVService.java — Service Layer

```java
package io.data;

import java.util.Optional;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class KVService {

  private final KVRepository repository;

  public KVService(KVRepository repository) {
    this.repository = repository;
  }

  @Transactional
  public KeyValue save(KeyValue resource) {
    return repository.save(resource);
  }

  @Transactional(readOnly = true)
  public Optional<KeyValue> getKey(String id) {
    return repository.findById(id);
  }

  @Transactional(readOnly = true)
  public Iterable<KeyValue> getAllKeys() {
    return repository.findAll();
  }

  @Transactional
  public void deleteKey(String key) {
    repository.deleteById(key);
  }
}
```

---

## KVRepository.java

```java
package io.data;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

@Repository
public interface KVRepository extends JpaRepository<KeyValue, String> {}
```

---

## KeyValue.java — JPA Entity

```java
package io.data;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.util.UUID;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;
import org.hibernate.annotations.UuidGenerator;

@Entity
@Table(name = "kvinfo")
@Data
@AllArgsConstructor
@NoArgsConstructor
public class KeyValue {
  @Id @UuidGenerator UUID key;
  String value;
}
```

---

## KVController.java — REST Controller

```java
package io.data;

import java.util.Optional;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/kvinfo")
public class KVController {

  private final KVService kvService;

  public KVController(KVService kvService) {
    this.kvService = kvService;
  }

  @PostMapping
  public KeyValue saveKey(@RequestBody KeyValue info) { return kvService.save(info); }

  @PutMapping
  public KeyValue updateKey(@RequestBody KeyValue info) { return kvService.save(info); }

  @GetMapping("/{key}")
  public Optional<KeyValue> getKey(@PathVariable String key) { return kvService.getKey(key); }

  @GetMapping
  public Iterable<KeyValue> getAllKeys() { return kvService.getAllKeys(); }

  @DeleteMapping("/{key}")
  public void deleteKey(@PathVariable String key) { kvService.deleteKey(key); }
}
```

---

## Flyway Migration Scripts

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
| `KVRetryPolicy.java` | Implements `RetryPolicy` — classifies retryable exceptions; owns `ExponentialBackOff` |
| `DataSourceAspect.java` | AOP aspect wrapping all `@Service` methods with `RetryTemplate` |
| `YBApplication.java` | Entry point; wires `RetryTemplate` bean and binds `RetryPropertyConfig` |
| `KVService.java` | Service using `@Transactional` |
| `application.yaml` | Full driver + pool + retry config |

---

## Aspect Order — Critical Detail

```
Request → [DataSourceAspect HIGHEST_PRECEDENCE] → [TransactionInterceptor] → Service method
```

If `DataSourceAspect` had lower precedence than `TransactionInterceptor`, a retry attempt would
reuse the already-failed transaction. With `HIGHEST_PRECEDENCE`, each retry opens a fresh transaction.
