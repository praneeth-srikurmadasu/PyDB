# Architecture

```
Client: REPL (pydb/cli/repl.py) or Python (pydb.database.Database)
  │  SQL text
  ▼
Lexer → Parser → AST                      pydb/sql/
  ▼
Binder (names, types) ← Catalog           pydb/planner/binder.py, pydb/catalog/
  ▼
Operator tree (Volcano)                   pydb/execution/
  ▼
TableHeap (slotted pages)  +  B+ trees    pydb/storage/, pydb/index/
  ▼
Buffer pool (LRU) → DiskManager → one database file
```

Each layer only calls the one below it. Everything is in one process and one file.

## The life of a query

`SELECT name FROM users WHERE age > 30`:

1. **Lex and parse** (`parse`): tokens, then
   `Select(items=(SelectItem(ColumnRef('name')),), from_table=TableRef('users'),
   where=BinaryOp('>', ColumnRef('age'), Literal(30)))`.
2. **Bind** (`Binder.bind_select`): `users` is looked up in the catalog; `name` and `age`
   become row positions 1 and 3; `age > 30` is checked (INT compared with INT) and typed
   BOOL; the WHERE clause must be BOOL.
3. **Plan** (`build_select` in `pydb/planner/physical_plan.py`):
   `Projection([col 1]) ← Filter(col 3 > 30) ← SeqScan(users)`. Joins, grouping, sorting
   and limits add stages to this pipeline (see query-processing.md).
   Bound expressions are compiled once into Python closures.
4. **Execute**: `Projection.next()` asks `Filter.next()`, which asks `SeqScan.next()` until a
   row's predicate is TRUE. `SeqScan` walks the table's page chain through the buffer pool,
   and each page read on a miss is checksum-verified by the DiskManager.

A write such as `INSERT INTO users VALUES (...)` binds the target columns, validates every
value against its column (type, VARCHAR length, NOT NULL), checks each unique index, then
writes the heap and every index (`pydb/execution/table_writer.py`).

## Catalog

The catalog is stored in the database file as three system tables, which describe
themselves as well as user tables:

| Table | Columns |
|---|---|
| `__tables__` | `table_id`, `name`, `first_page` |
| `__columns__` | `table_id`, `position`, `name`, `type`, `length`, `not_null`, `default_sql` |
| `__indexes__` | `index_id`, `name`, `table_id`, `header_page`, `is_unique`, `is_primary`, `columns` |

The header page's `catalog_root` is the first page of `__tables__`. Opening a database
scans `__tables__` to find the other two, then loads every table and index description into
memory (`pydb/catalog/catalog.py`). Defaults are stored as SQL literals and parsed back.

- `PRIMARY KEY` creates a unique index `<table>_pkey` and makes its columns NOT NULL.
- `UNIQUE` on a column creates a unique index `<table>_<column>_key`.
- `CREATE INDEX` builds the tree from the table's existing rows; a duplicate in a unique
  index aborts it and frees the tree's pages.
- `DROP TABLE` frees the heap and index pages and deletes the catalog rows.

System tables can be read with `SELECT` but not written. User table names may not start
with `__`.

## Durability (current state)

Changes are made in buffer-pool pages and written to the file when pages are evicted and
when the database is closed. There is no write-ahead log yet, so a process that stops
without closing the database can lose recent changes or leave the file inconsistent. A
statement that fails part-way is undone by the statement undo log in `TableWriter`.
