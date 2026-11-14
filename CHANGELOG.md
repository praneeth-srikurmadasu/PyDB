# Changelog

All notable changes to PyDB are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-14

First release.

### Added

- **Storage:** 4 KiB pages with CRC32 checksums, a header page with a free list, an LRU
  buffer pool with pin counts and per-frame latches, slotted-page table heaps, and a row
  format with a NULL bitmap.
- **Indexes:** B+ trees with order-preserving key encoding, unique and non-unique,
  composite keys, range scans, and merges on delete.
- **SQL:**
  - a hand-written lexer, a recursive-descent parser and Pratt expression parsing;
  - DDL (`CREATE/DROP TABLE`, `CREATE/DROP INDEX`) and DML (`INSERT`, `UPDATE`,
    `DELETE`);
  - `SELECT` with joins, grouping, aggregates, `DISTINCT`, ordering and `LIMIT/OFFSET`;
  - `SHOW TABLES`, `DESCRIBE`, and `?` parameters.
- **Catalog** stored in system tables inside the database file.
- **Query processing:**
  - a binder and Volcano operators;
  - nested loop, index nested loop and hash joins;
  - external merge sort, top-N sort and hash aggregation.
- **Optimizer:** `ANALYZE` statistics, selectivity estimates, a cost model, index
  selection, join ordering, and `EXPLAIN [ANALYZE]`.
- **Transactions:**
  - `BEGIN`/`COMMIT`/`ROLLBACK`, autocommit, statement-level rollback;
  - a write-ahead log with physical redo (with torn-page repair) and logical undo, and
    compensation log records;
  - sharp checkpoints and ARIES-style crash recovery.
- **Concurrency:** sessions isolated by strict two-phase table locking with upgrades, FIFO
  fairness, waits-for deadlock detection and lock timeouts; an engine latch serializing
  statements.
- **Server:** an asyncio TCP server with a length-prefixed JSON protocol, a Python client,
  and `--connect` in the shell.
- **DB-API 2.0** (PEP 249) module `pydb.dbapi`.
- **Shell** (`python -m pydb.cli.repl`) with meta commands.
- **Benchmark** script comparing PyDB with SQLite.
- **Tests:**
  - unit tests and Hypothesis model tests for every layer;
  - a query suite checked against SQLite;
  - threaded concurrency tests;
  - crash tests that kill real processes at random and injected points.
- **CI** on Ubuntu and Windows with Python 3.12 and 3.13.

[1.0.0]: https://github.com/praneeth-srikurmadasu/PyDB/releases/tag/v1.0.0
