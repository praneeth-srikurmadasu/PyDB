# Concurrency

Many sessions can use one database at once: threads in one process
(`Database.session()`, or several `pydb.dbapi` connections) or clients of the server
(`python -m pydb.server`). Each session has its own transaction. Two mechanisms keep them
apart, at two levels.

| | Table locks | Engine latch |
|---|---|---|
| Protects | transactions from each other | page structures from concurrent statements |
| Held | from first use until COMMIT or ROLLBACK | for one statement |
| Code | `pydb/txn/lock_manager.py`, `lock_plan.py` | `Database.latch`, `pydb/session.py` |

## Table locks: strict two-phase locking

Before a statement runs, the session works out which tables it touches
(`lock_plan.required_locks`) and locks them in name order:

| Statement | Lock |
|---|---|
| `SELECT`, `EXPLAIN [ANALYZE]` | SHARED on every table in FROM and JOIN |
| `INSERT`, `UPDATE`, `DELETE` | EXCLUSIVE on the target table |
| `CREATE TABLE`, `DROP TABLE`, `CREATE INDEX`, `DROP INDEX` | EXCLUSIVE on the table |
| `ANALYZE` | SHARED on the table |
| `SHOW TABLES`, `DESCRIBE`, `BEGIN`, `COMMIT`, `ROLLBACK` | none |

Locks are released only when the transaction ends (*strict* 2PL). Only SHARED with SHARED is
compatible.

What this guarantees:

- **No dirty reads.** A writer holds EXCLUSIVE until it commits, so nobody reads its rows
  before then. This is also what makes logical undo safe: no other transaction can have
  built on rows that a rollback removes.
- **Repeatable reads, no phantoms.** A reader holds SHARED on the whole table until it
  commits, so neither existing rows nor the set of rows can change under it.
- **Serializable.** Under strict 2PL, every schedule of committed transactions is
  equivalent to running them one after another in commit order.

The price is granularity: two transactions writing *different rows* of the same table run
one after the other. Row locks would need intention locks on tables, plus index-range or
predicate locks to prevent phantoms. That is the natural next step, and it fits behind
the same `LockManager` interface.

### Granting and fairness

A request is granted if it is compatible with every other holder **and** no incompatible
request is queued ahead of it. So a writer waiting behind readers is not starved by a
stream of new readers. A SHARED holder asking for EXCLUSIVE is an **upgrade**: it queues
ahead of ordinary requests (it already holds part of the lock) and is granted once the
other readers leave.

### Deadlocks

```
txn 1: holds X(a), wants X(b)  ──waits for──▶  txn 2
txn 2: holds X(b), wants X(a)  ──waits for──▶  txn 1     cycle → txn 2 is the victim
```

When a request has to wait, the lock manager first walks the *waits-for graph* from the
requesting transaction. An edge A → B means A waits for a lock that B holds in an
incompatible mode, or that B has queued ahead of A. If the walk gets back to the requester,
the request fails at once with `DeadlockError`, and the requester is the victim.

Checking only at that moment is enough, because a cycle can only form when some
transaction starts waiting. (A transaction that has just been granted a lock is running,
not waiting, so new edges pointing at it cannot close a cycle.)

The session then rolls back the victim's **whole** transaction, so that its locks are
released and the other transactions can proceed. The application retries. The classic
case is two transactions that both read and then update the same table. Each holds SHARED
and asks for EXCLUSIVE, and one of them must give way.

### Timeouts

A wait longer than the lock timeout (30 s by default; `Database(lock_timeout=...)`,
`--lock-timeout` on the server) fails with `LockTimeoutError` and rolls back the waiting
transaction. Deadlocks never need the timeout. It exists for a client that holds locks and
stops sending statements.

## The engine latch

B+ tree splits, heap pages, the free list and the log are not safe for two statements to
change at the same time. Crash recovery also relies on that: at most one operation is half
done at a crash. So only one statement runs inside the engine at a time, under
`Database.latch`.

The order matters. A statement takes its **locks first, without the latch**, then takes
the latch to run. If it held the latch while waiting for a lock, the transaction holding
that lock could never run its next statement, and both would wait forever.

```
session A: [lock t] ──latch── UPDATE t ──unlatch──            [lock u: waits ...]
session B:            [lock u] ──(waits for latch)── INSERT u ── ... COMMIT → A proceeds
```

Statements are short, and Python runs one thread at a time anyway, so the latch costs
little throughput. Transactions still interleave statement by statement.

## Sessions and transactions

`pydb/session.py`:

- Autocommit statement: begin, lock, run under the latch, commit, release locks.
- Inside `BEGIN`: lock, then run under the latch with a savepoint. A failing statement is
  rolled back to the savepoint and the transaction continues. A deadlock or lock timeout
  rolls back the whole transaction.
- DDL and `ANALYZE` run as their own transaction and are refused inside `BEGIN`.
- `Session.close()` rolls back the open transaction. `Database.close()` closes every
  session, wakes any session waiting for a lock (it fails), checkpoints and empties the log.

## Testing

- `tests/unit/test_lock_manager.py`: the compatibility matrix, FIFO fairness, upgrade
  priority, two- and three-transaction deadlocks, upgrade deadlocks, timeouts, cleanup of
  failed requests, waking waiters on close, and eight threads hammering one lock.
- `tests/property/test_lock_manager_model.py`: random requests and releases from five
  transactions against a model of holders.
- `tests/integration/test_concurrency.py`:
  - isolation: blocked dirty reads, repeatable reads, readers sharing, writers on
    different tables, DDL waiting for readers;
  - deadlock victims and lock timeouts;
  - read-modify-write counters that lose no update (upgrade deadlocks retried);
  - bank transfers whose total, read by a concurrent auditor, never changes;
  - closing with open sessions;
  - a crash with three interleaved transactions.
- `tests/integration/test_server.py` and `test_dbapi.py`: the same guarantees across
  connections.

## Limits

- Table-level locks only: writers to the same table serialize.
- One statement runs in the engine at a time.
- One process per database file. Sharing across processes goes through the server.
