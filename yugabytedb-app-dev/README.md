# YugabyteDB Application Development Skill

An AI agent skill that encodes **best practices for building production-grade applications** that connect to YugabyteDB using multiple language runtimes and frameworks.

## Overview

Writing applications for YugabyteDB requires attention to areas that standard PostgreSQL applications often skip. This skill ensures applications are built with proper driver configuration, topology-aware load balancing, connection pool tuning, and crucial transaction retry logic to handle transient errors in a distributed SQL environment.

## What It Covers

| Language / Framework | Reference | Key Topics |
|---|---|---|
| **Java / Spring Boot** | [`java-spring.md`](references/java-spring.md) | `jdbc-yugabytedb`, HikariCP, Spring `RetryPolicy`, AOP aspects |
| **Java / Spring WebFlux** | [`java-spring-reactive.md`](references/java-spring-reactive.md) | `r2dbc-yugabytedb`, Reactor `.retryWhen()` |
| **Java / Quarkus** | [`java-quarkus.md`](references/java-quarkus.md) | Agroal pool, SmallRye `@Retry`, CDI interceptors |
| **Python** | [`python.md`](references/python.md) | `psycopg2` / `asyncpg`, retry decorators |
| **Node.js** | [`nodejs.md`](references/nodejs.md) | `pg` (node-postgres), async retry loops |
| **Go** | [`go.md`](references/go.md) | `pgx` driver, explicit retry loops |
| **.NET** | [`dotnet.md`](references/dotnet.md) | `Npgsql`, EF Core, Polly `AsyncRetryPolicy` |

## When to Use

This skill activates when a user:

- Asks to **connect an application to YugabyteDB**
- Needs help with **YugabyteDB driver configuration** or connection pooling
- Encounters **transaction failures**, **serialization errors** (`40001`), or deadlocks (`40P01`) and needs to implement retries
- Is migrating an existing PostgreSQL application to YugabyteDB
- Mentions keywords like *"YugabyteDB Spring Boot"*, *"YugabyteDB retry"*, or *"Quarkus YugabyteDB"*

## Core Principles

- **Driver:** Always use the YugabyteDB smart driver if available for cluster and topology-aware load balancing.
- **Retries:** Distributed transactions can throw transient errors. **Always implement application-side retries**.
- **Transactions:** Disable `auto-commit` and manage transactions explicitly.

## Project Structure

```
yugabytedb-app-dev/
├── SKILL.md                          # Skill definition, concepts, and prompts
├── README.md                         # This overview file
└── references/
    ├── dotnet.md                     # .NET / EF Core best practices
    ├── go.md                         # Go / pgx best practices
    ├── java-quarkus.md               # Quarkus / Agroal best practices
    ├── java-spring-reactive.md       # Spring WebFlux / R2DBC best practices
    ├── java-spring.md                # Spring Boot / HikariCP best practices
    ├── nodejs.md                     # Node.js / pg best practices
    └── python.md                     # Python / psycopg2 best practices
```
