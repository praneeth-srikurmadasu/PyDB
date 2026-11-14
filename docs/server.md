# Server, client and DB-API

## Running the server

```powershell
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pydb.server shop.db --port 5433
```

Options: `--host` (default `127.0.0.1`), `--port` (default 5433), `--max-connections`
(default 64), `--lock-timeout` (seconds, default 30). Ctrl+C stops the server cleanly: open
transactions are rolled back, the database is checkpointed and its log emptied.

**Security:** there is no authentication and no encryption. The server listens on
localhost by default. Only bind it to other addresses on a network you trust.

Connect with the shell:

```powershell
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pydb.cli.repl --connect 127.0.0.1:5433
```

`.tables` works over the network. `.schema` needs a local file; use `DESCRIBE <table>`.

## Design

`pydb/server/server.py` uses `asyncio`. The event loop only reads and writes frames. Each
connection gets:

- a **session** (its own transaction and locks), and
- **one worker thread** that runs the connection's statements in order.

A statement waiting for a table lock blocks that connection's thread. It never blocks the
event loop or other clients. When a connection ends, cleanly, by dropping, or by sending
garbage, its session is closed: the open transaction is rolled back and its locks
released.

`BackgroundServer` runs a server on its own thread and loop. Tests use it, and so can an
application that wants to embed a server:

```python
with BackgroundServer(db, port=0) as server:
    host, port = server.address
```

## Protocol

Every message is a frame: a 4-byte big-endian length, then that many bytes of UTF-8 JSON
holding one object. Frames over 16 MiB are refused. The server speaks first:

```
server → {"server": "PyDB", "version": "1.0.0", "protocol": 1}
client → {"sql": "SELECT name FROM users WHERE id = ?", "params": [7]}
server → {"ok": true, "columns": ["name"], "rows": [["ann"]], "rowcount": 1,
          "message": "SELECT 1", "in_transaction": false}
client → {"sql": "INSERT INTO users VALUES (7, 'bob')"}
server → {"ok": false, "error": {"type": "UniqueViolationError", "message": "..."},
          "in_transaction": false}
```

- Requests are answered one at a time, in order. A client that wants concurrency opens
  more connections.
- `error.type` names a class in `pydb.errors`, so the client raises the same exception
  the embedded engine would. Syntax errors also carry `line` and `column`. Unexpected
  server failures are reported as `InternalError`.
- A frame that is not valid JSON, is not an object or is too large gets a `ProtocolError`
  reply, and the connection is closed, since the byte stream can no longer be trusted. A
  well-formed request with a bad field (no `sql`, `params` not a list) gets an error and
  the connection stays open.
- INT values are JSON integers and FLOAT values always have a decimal point, so types
  survive the round trip.

Why length-prefixed JSON? It is easy to debug, is in the standard library, and its
framing never depends on parsing partial input. A binary row format would be faster for
large results. That would mean a new `protocol` number, which the greeting exists to
negotiate.

## Python client

```python
from pydb.client import Client

with Client("127.0.0.1", 5433) as client:
    client.execute("BEGIN")
    client.execute("UPDATE acct SET balance = balance - ? WHERE id = ?", [30, 1])
    client.execute("COMMIT")
    print(client.execute("SELECT * FROM acct").rows)
```

## DB-API 2.0 (PEP 249)

`pydb.dbapi` opens a database file in-process:

```python
import pydb.dbapi

conn = pydb.dbapi.connect("shop.db")
cur = conn.cursor()
cur.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT)")
cur.executemany("INSERT INTO items VALUES (?, ?)", [(1, "pen"), (2, "ink")])
conn.commit()
cur.execute("SELECT name FROM items WHERE id = ?", (2,))
print(cur.fetchone())  # ('ink',)
conn.close()
```

- `paramstyle = "qmark"`. Parameters are bound in the parser: each `?` becomes a literal
  in the syntax tree. Values are never pasted into SQL text, so they cannot inject SQL.
  `LIMIT ?` and `OFFSET ?` work too.
- **Transactions:** a transaction opens implicitly before the first INSERT, UPDATE or
  DELETE, and `commit()` or `rollback()` ends it. `with conn:` commits on success and rolls
  back on an exception. `conn.autocommit = True` commits each statement. Schema changes run
  immediately and are refused inside an open transaction.
- **Errors** follow PEP 249:

  | PyDB error | DB-API class |
  |---|---|
  | constraint violations | `IntegrityError` |
  | type, size, division by zero | `DataError` |
  | deadlock, lock timeout, storage | `OperationalError` |
  | syntax, parameters, unknown names, misuse of transactions | `ProgrammingError` |
  | not supported | `NotSupportedError` |

- `threadsafety = 1`: threads may share the module but not a connection. Several
  connections to the same file in one process share one open database and are isolated
  by table locks. The file is closed with its last connection.
- `cursor.description` gives each column's name and type (`INT`, `FLOAT`, `BOOL` or `TEXT`,
  judged from the first non-NULL value). `NUMBER` and `STRING` compare equal to the
  matching types. PyDB has no date or binary types, so `Date()`, `Binary()` and the like
  raise `NotSupportedError`.
