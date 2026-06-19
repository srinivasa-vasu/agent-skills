> **Experimental / WIP** — This repo is under active development. Skills and APIs may change.

# YugabyteDB Agent Skills

A collection of [Claude Code](https://claude.ai/code) agent skills purpose-built for YugabyteDB. Each skill encodes deep domain knowledge so Claude can give accurate, production-ready guidance without guessing.

## Skills

### [`yugabytedb-app-dev`](yugabytedb-app-dev/)

Build production-grade applications that connect to YugabyteDB across any language runtime or framework.

**Covers:** JDBC/Spring Boot, Spring WebFlux + R2DBC, Quarkus (SmallRye + Agroal), Python, Node.js, Go, .NET — with correct driver config, HikariCP/Agroal/Npgsql pool tuning, transaction retry logic (SQL states `40001`, `40P01`, `57P01`), Flyway migrations, and topology-aware load balancing.

**Triggers on:** *"connect my app to YugabyteDB"*, *"YugabyteDB Spring Boot"*, *"YugabyteDB retry"*, serialization failure errors, connection pool setup.

---

### [`yugabytedb-schema-design`](yugabytedb-schema-design/)

Design high-performance schemas for YugabyteDB from scratch or optimize existing ones.

**Covers:** Hash vs range sharding decisions, avoiding write hotspots on sequential/timestamp columns, partial indexes for NULL-heavy and skewed columns, low-cardinality index design, covering indexes, primary key selection, foreign key index requirements, and redundant index cleanup.

**Triggers on:** *"design a YugabyteDB schema"*, *"sharding key"*, *"tablet hotspot"*, *"YSQL indexes"*, slow or skewed writes.

---

### [`yugabytedb-sizing`](yugabytedb-sizing/)

Calculate optimal YugabyteDB cluster sizing from workload inputs, producing a complete hardware recommendation targeting ≤65% CPU utilization.

**Covers:** CPU sizing with Raft/RPC overhead, storage with LZ4 compression + WAL + compaction reserve, memory per connection, IOPS estimation, network bandwidth, 1–2 year storage growth projections, failure resilience under node/zone loss, CDC and xCluster overhead.

**Triggers on:** *"size my cluster"*, *"how many nodes"*, *"YugabyteDB capacity planning"*, any QPS + read/write ratio + table size question.

---

## Structure

```
agent-skills/
├── yugabytedb-app-dev/          # Application development skill
│   ├── SKILL.md
│   └── references/              # Per-runtime reference guides
├── yugabytedb-schema-design/    # Schema design skill
│   ├── SKILL.md
│   └── references/              # Per-topic reference guides
└── yugabytedb-sizing/           # Cluster sizing skill
    ├── SKILL.md
    └── scripts/
        └── sizing_calc.py       # Python sizing calculator
```
