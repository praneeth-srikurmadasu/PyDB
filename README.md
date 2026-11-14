# PyDB

A disk-based relational database engine in pure Python, using only the standard library.
It has SQL, B+ tree indexes, a cost-based query planner, ACID transactions with crash
recovery, concurrent sessions with deadlock detection, a TCP server and a DB-API driver.

PyDB is built to be read. Every layer is small, typed (`mypy --strict`), documented and
tested against a model.

## Features

- **Storage:** 4 KiB checksummed pages, a free list, an LRU buffer pool with pin counts and
  latches, slotted-page heaps. [storage.md](docs/storage.md)
- **Indexes:** B+ trees with order-preserving key encoding, unique and composite keys,
  range scans. [indexing.md](docs/indexing.md)
- **SQL:** `CREATE/DROP TABLE`, `CREATE/DROP INDEX`, `INSERT`, `UPDATE`, `DELETE`, and
  `SELECT` with `INNER/LEFT JOIN`, `WHERE`, `GROUP BY`/`HAVING`, `COUNT/SUM/AVG/MIN/MAX`,
  `DISTINCT`, `ORDER BY`, `LIMIT/OFFSET`, `LIKE`, `IN`, `BETWEEN`, `IS NULL`, three-valued
  NULL logic, `?` parameters. Types: `INT`, `FLOAT`, `BOOL`, `TEXT`, `VARCHAR(n)`.
  Constraints: `PRIMARY KEY`, `UNIQUE`, `NOT NULL`, `DEFAULT`.
- **Query processing:** binder, Volcano operators, three join algorithms (nested loop,
  index nested loop, hash), external merge sort, hash aggregation, statistics (`ANALYZE`), a
  System R–style cost model, `EXPLAIN [ANALYZE]`. [query-processing.md](docs/query-processing.md)
- **Transactions:** `BEGIN`/`COMMIT`/`ROLLBACK`, a write-ahead log, ARIES-style recovery
  (physical redo with torn-page repair, logical undo, compensation records), checkpoints.
  Crash-tested by killing real processes. [transactions.md](docs/transactions.md)
- **Concurrency:** sessions isolated by strict two-phase table locking (serializable),
  waits-for deadlock detection, lock timeouts. [concurrency.md](docs/concurrency.md)
- **Access:** interactive shell, an asyncio TCP server and client, and a PEP 249 DB-API
  module. [server.md](docs/server.md)

How the pieces fit: [architecture.md](docs/architecture.md).

## Install

Requires Python 3.12 or newer. On Windows PowerShell:

```powershell
git clone https://github.com/praneeth-srikurmadasu/PyDB.git
cd PyDB
py -3.13 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
```

On Linux or macOS, use `python3 -m venv .venv` and `.venv/bin/python` instead.

## Use it

**Shell:**

```powershell
& ".\.venv\Scripts\python.exe" -m pydb.cli.repl shop.db
```

```sql
pydb> CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, city TEXT);
pydb> INSERT INTO users VALUES (1, 'ann', 'Pune'), (2, 'bob', 'Delhi'), (3, 'cy', 'Pune');
pydb> CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total FLOAT);
pydb> INSERT INTO orders VALUES (1, 1, 9.5), (2, 1, 20), (3, 2, 4);
pydb> SELECT u.name, COUNT(o.id) AS n, SUM(o.total) AS spent FROM users u
  ...> LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.name ORDER BY spent DESC;
pydb> BEGIN;
pydb*> DELETE FROM orders;
pydb*> ROLLBACK;
pydb> EXPLAIN SELECT name FROM users WHERE id = 2;
```

Meta commands: `.tables`, `.schema [table]`, `.timer on|off`, `.help`, `.quit`.

**Server and clients:**

```powershell
& ".\.venv\Scripts\python.exe" -m pydb.server shop.db --port 5433
# in other terminals:
& ".\.venv\Scripts\python.exe" -m pydb.cli.repl --connect 127.0.0.1:5433
```

```python
from pydb.client import Client

with Client("127.0.0.1", 5433) as client:
    client.execute("UPDATE users SET city = ? WHERE id = ?", ["Mumbai", 2])
    print(client.execute("SELECT * FROM users").rows)
```

The server has no authentication or TLS and listens on localhost by default.

**DB-API (embedded):**

```python
import pydb.dbapi

conn = pydb.dbapi.connect("shop.db")
with conn:  # commits on success, rolls back on error
    conn.execute("INSERT INTO users VALUES (?, ?, ?)", (4, "dee", "Goa"))
print(conn.execute("SELECT name FROM users WHERE city = ?", ("Goa",)).fetchall())
conn.close()
```

**Embedded engine:**

```python
from pydb.database import Database

with Database("shop.db") as db:
    print(db.execute("SELECT COUNT(*) FROM users").rows)
    session = db.session()  # a second, independent connection
```

Each database is a file plus a write-ahead log, `shop.db.wal`, which is emptied on a
clean close.

## Development

```powershell
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
& ".\.venv\Scripts\python.exe" -m mypy
& ".\.venv\Scripts\python.exe" -m pytest
```

The suite has over 850 tests: unit tests, Hypothesis model tests, a SQLite-checked
query suite, threaded concurrency tests and crash tests that kill child processes.
Property tests run 30 examples each locally (`dev` profile) and 100 in CI (`ci`). For a
deeper run:

```powershell
$env:HYPOTHESIS_PROFILE = "thorough"
& ".\.venv\Scripts\python.exe" -m pytest
Remove-Item Env:HYPOTHESIS_PROFILE
```

`PYDB_CRASH_ROUNDS` and `PYDB_CRASH_SEED` scale and vary the crash tests. See
[CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Benchmarks

`benchmarks/compare_sqlite.py` runs the same SQL on PyDB and SQLite (file databases,
explicit transactions) and prints the best of three runs:

```powershell
& ".\.venv\Scripts\python.exe" benchmarks/compare_sqlite.py --rows 20000
```

One run on Linux, Python 3.13 (your numbers will differ):

| workload | PyDB 1.0.0 ms | SQLite 3.53 ms | ratio |
|---|---:|---:|---:|
| load 20,000 rows x 2 tables, build 2 indexes | 9,587 | 131 | 73x |
| 2,000 primary-key lookups | 449 | 6.6 | 68x |
| range scan on PK (1% of rows) | 1.7 | <0.1 | — |
| index lookup on city (2%) | 3.3 | 0.1 | 33x |
| full scan + filter | 45 | 0.5 | 83x |
| GROUP BY 50 groups | 51 | 3.4 | 15x |
| ORDER BY + LIMIT 10 | 42 | 0.7 | 64x |
| join users x orders | 84 | 0.9 | 93x |
| 500 updates in one transaction | 317 | 2.8 | 113x |

SQLite is C and has been tuned for over 20 years. PyDB is an interpreted, readable
implementation, so it is one to two orders of magnitude slower. The useful signal is the
*shape* of the numbers. An index lookup costs a few milliseconds where a full scan costs
tens. Aggregation narrows the gap because it is dominated by per-row work that both engines
must do.

## Limits

- Table-level locks: writers to the same table run one after another.
- One statement runs inside the engine at a time. One process per database file (share
  through the server).
- No subqueries, views, `ALTER TABLE`, or date and binary types.
- The server has no authentication or encryption.

## License

MIT, see [LICENSE](LICENSE).
