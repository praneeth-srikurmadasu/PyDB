# Architecture

```
Clients: REPL, pydb.client ──TCP──▶ Server (asyncio)      pydb/cli/, pydb/client.py, pydb/server/
         pydb.dbapi, pydb.database.Database               pydb/dbapi.py, pydb/database.py
  │  SQL text (+ ? parameters)
  ▼
Session: transaction, table locks, engine latch           pydb/session.py, pydb/txn/lock_*.py
  ▼
Lexer → Parser → AST                      pydb/sql/
  ▼
Binder (names, types) ← Catalog           pydb/planner/binder.py, pydb/catalog/
  ▼
Optimizer (rules, statistics, costs)      pydb/planner/optimizer.py, stats.py
  ▼
Operator tree (Volcano)                   pydb/execution/
  ▼
TableHeap (slotted pages)  +  B+ trees    pydb/storage/, pydb/index/
  ▼
Buffer pool (LRU) → DiskManager → database file
  │ every page change is logged
  ▼
Transactions, write-ahead log, recovery   pydb/txn/  → <database>.wal
```

Each layer only calls the one below it. A database is one data file plus one log file.

## Package map

| Package | What it does | Docs |
|---|---|---|
| `pydb/storage/` | pages, disk manager, buffer pool, slotted pages, heaps, tuples | storage.md |
| `pydb/index/` | B+ tree and key encoding | indexing.md |
| `pydb/sql/` | lexer, parser, AST | query-processing.md |
| `pydb/catalog/` | schema stored as system tables | this file |
| `pydb/planner/` | binder, statistics, optimizer, EXPLAIN | query-processing.md |
| `pydb/execution/` | Volcano operators, expressions, table writer | query-processing.md |
| `pydb/txn/` | WAL, transactions, recovery, lock manager | transactions.md, concurrency.md |
| `pydb/session.py`, `database.py` | sessions on an open database | concurrency.md |
| `pydb/server/`, `client.py`, `dbapi.py`, `cli/` | ways in | server.md |

## The life of a query

`SELECT name FROM users WHERE age > 30`:

1. **Lex and parse** (`parse`): tokens, then
   `Select(items=(SelectItem(ColumnRef('name')),), from_table=TableRef('users'),
   where=BinaryOp('>', ColumnRef('age'), Literal(30)))`.
2. **Bind** (`Binder.bind_select`): `users` is looked up in the catalog; `name` and `age`
   become row positions 1 and 3; `age > 30` is checked (INT compared with INT) and typed
   BOOL; the WHERE clause must be BOOL.
3. **Optimize** (`Optimizer.plan_select`): constants are folded, `age > 30` is estimated to
   keep a third of the rows (or a measured fraction after `ANALYZE`), so an index on `age`
   would not be used, and the plan is
   `Projection([col 1]) ← Filter(col 3 > 30) ← SeqScan(users)`. `EXPLAIN` shows this tree.
   Joins, grouping, sorting and limits add stages (see query-processing.md).
   Bound expressions are compiled once into Python closures.
4. **Execute**: `Projection.next()` asks `Filter.next()`, which asks `SeqScan.next()` until a
   row's predicate is TRUE. `SeqScan` walks the table's page chain through the buffer pool,
   and each page read on a miss is checksum-verified by the DiskManager.

A write such as `INSERT INTO users VALUES (...)` binds the target columns, validates every
value against its column (type, VARCHAR length, NOT NULL), checks each unique index, then
writes the heap and every index (`pydb/execution/table_writer.py`).

## Catalog

The catalog is stored in the database file as system tables, which describe
themselves as well as user tables:

| Table | Columns |
|---|---|
| `__tables__` | `table_id`, `name`, `first_page` |
| `__columns__` | `table_id`, `position`, `name`, `type`, `length`, `not_null`, `default_sql` |
| `__indexes__` | `index_id`, `name`, `table_id`, `header_page`, `is_unique`, `is_primary`, `columns` |
| `__stats__` | `table_id`, `position` (-1 for the table), `row_count`, `page_count`, `distinct_count`, `null_count`, `min_sql`, `max_sql` |

The header page's `catalog_root` is the first page of `__tables__`. Opening a database
scans `__tables__` to find the others (adding `__stats__` to files made before it existed), then loads every table and index description into
memory (`pydb/catalog/catalog.py`). Defaults are stored as SQL literals and parsed back.

- `PRIMARY KEY` creates a unique index `<table>_pkey` and makes its columns NOT NULL.
- `UNIQUE` on a column creates a unique index `<table>_<column>_key`.
- `CREATE INDEX` builds the tree from the table's existing rows; a duplicate in a unique
  index aborts it and frees the tree's pages.
- `DROP TABLE` frees the heap and index pages and deletes the catalog rows.

System tables can be read with `SELECT` but not written. User table names may not start
with `__`.

## Durability

Every statement runs in a transaction. Page changes are logged by the buffer pool, completed
row operations by the table writer, and a commit returns once its log record is on disk.
Opening a database replays and rolls back from the log as needed. See transactions.md.

## Concurrency

Sessions isolate transactions with table locks under strict two-phase locking
(serializable), and statements run one at a time under the engine latch. Locks are taken
before the latch, so lock waits never block the engine. See concurrency.md.
