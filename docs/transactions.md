# Transactions and recovery

PyDB keeps a write-ahead log (`<database>.wal`) and recovers from crashes with an
ARIES-style algorithm (`pydb/txn/`). Every change to a data page is logged before the page
can reach the disk, and a transaction is committed when its COMMIT record is on disk.

## Transactions

- **Autocommit.** Outside `BEGIN`, every statement is its own transaction.
- **Explicit.** `BEGIN` starts a transaction; statements join it until `COMMIT` or
  `ROLLBACK`. A statement that fails inside a transaction is rolled back on its own (to a
  savepoint taken before it) and the transaction stays open.
- **DDL** (`CREATE`/`DROP TABLE`, `CREATE`/`DROP INDEX`) and `ANALYZE` always run as their own
  transaction, and are refused inside `BEGIN`.
- `COMMIT` returns only after the log is fsynced up to its COMMIT record.
- A transaction that changes nothing writes nothing to the log.
- Each transaction is `ACTIVE`, then `COMMITTED` or `ABORTED` (`pydb/txn/transaction.py`).

## The log

```
header   "PYDBWAL1" | base_lsn (u64) | checkpoint_lsn (u64)
record   [lsn:8][prev_lsn:8][txn_id:8][type:1][length:4][payload][crc32:4]
```

A record's LSN is `base_lsn` + its offset in the file, so LSNs only grow, even after the
log is emptied (which raises `base_lsn`). `prev_lsn` chains a transaction's records
backwards. On open, the first record that is incomplete or fails its CRC marks the end of
the log and the rest is cut off: that is a write torn by the crash.

| Type | Payload | Used by |
|---|---|---|
| `BEGIN` | physical-undo flag | analysis |
| `COMMIT`, `ABORT`, `END` | | analysis |
| `PAGE_DIFF` | page id, changed byte ranges (before and after) | redo; undo of interrupted operations and DDL |
| `INSERT`, `DELETE` | table id, RID, row bytes | logical undo |
| `CLR` | `undo_next_lsn`, physical or logical | undo after a crash during undo |
| `CHECKPOINT` | active transactions, dirty pages | analysis start |

`PAGE_DIFF` is PyDB's addition to the usual list; there is no `PAGE_ALLOC` record because
allocating a page is written to the data file immediately (see *Page allocation*).

## What is logged

**Physical redo.** When code takes a page's write latch through the buffer pool, the pool
copies the page; when the latch is released it compares the two, logs the changed 32-byte
granules as a `PAGE_DIFF` and sets the page's `page_lsn` to the record's LSN. Every page
change (heap rows, B+ tree splits and merges, catalog rows) is captured without any code
knowing about the log. The first change to a page after a checkpoint logs the whole page,
so redo can rebuild a page whose last write to disk was torn by the crash (PostgreSQL calls
this a full-page write).

**Logical undo.** After a row operation has changed the heap and every index, the table
writer logs `INSERT` or `DELETE` with the row. Undoing an `INSERT` removes the row and its
index entries; undoing a `DELETE` restores the row in its slot and re-adds its index entries.
Undo by row, not by page, stays correct after other transactions have changed the same
pages (for example, split the same B+ tree leaf). To make it always possible:

- an `UPDATE` is logged as `DELETE` of the old row plus `INSERT` of the new one;
- a `DELETE` *reserves* the slot (a flag bit on its length) instead of freeing it, so the row
  can be restored in place; the space is freed when the transaction commits.

**Interrupted operations.** If the process stops in the middle of a row operation, its
`PAGE_DIFF` records come after the transaction's last `INSERT`/`DELETE`. Undo writes back
their before-images. This is safe because statements run one at a time (under the engine
latch, see concurrency.md): those records are the newest in the log and nobody else has
touched those bytes. Pages that a B+ tree merge
frees during an operation are released only after the operation's record is written, since
undoing an interrupted merge restores the parent's pointer to them.

**DDL** is a *physical* transaction, undone entirely by before-images. Each DDL transaction
is one statement, run start to finish under the engine latch, so no other change lands
between its records and this is safe; pages it frees are released at its commit.

## Write-ahead rule and buffer policy

The buffer pool uses **steal** (a dirty page of an uncommitted transaction can be written to
disk) and **no-force** (commit writes only the log). Before writing a page, the pool flushes
the log up to the page's `page_lsn`, so the log always holds what is needed to undo a stolen
page or redo a committed one.

## Checkpoints

A checkpoint writes every dirty page, fsyncs the data file, and then logs `CHECKPOINT`
with the active transactions (the dirty-page table is empty because every page was just
written). Its LSN is stored in the log header. Redo can therefore start at the checkpoint,
and every page changed afterwards has a full image after it. A checkpoint is taken when the
database opens, on `Database.checkpoint()`, after a commit once 5000 records were written
since the last one, and on close. A clean close then empties the log.

## Recovery

`recover()` (`pydb/txn/recovery.py`) runs when the database opens:

1. **Analysis.** Start from the checkpoint's list of active transactions and scan forward,
   recording each transaction's newest record and whether it committed or ended.
2. **Redo** (in the same scan). Apply every `PAGE_DIFF` whose page has an older `page_lsn`.
   If a page cannot be read because it was torn, its first record after the checkpoint is a
   full image, which replaces it. This restores the exact state at the crash.
3. **Undo.** Roll back every transaction that did not commit, newest first, writing a `CLR`
   after each step. A logical CLR marks an operation as compensated; a physical CLR marks a
   restored before-image. If recovery is interrupted, the next run redoes the CLRs' page
   changes and continues from where they point, so nothing is undone twice.

Committed transactions without an `END` record get one. A checkpoint follows.

## Page allocation

Allocating and freeing pages updates the data file's header page directly, outside the log:

- a page is stamped with an LSN between all existing records and all future ones when it is
  allocated or freed, so redo never applies records from a previous use of the page;
- the log is flushed before a page is freed, so no durable page refers to a free page;
- a page taken from the free list is unlinked (header written) before it is overwritten, so
  a crash between the two writes leaks a page instead of corrupting the free list;
- after recovery, a freed page that redo had read can be allocated again; the pool drops
  its stale cached copy.

A crash can leak pages (allocated, never referenced); it cannot make a page both free and in
use.

## Testing

- `tests/unit/test_wal.py`: the log format, torn and corrupt tails, truncation, codecs.
- `tests/integration/test_transactions.py`: commit, rollback, savepoints, reserved deletes,
  DDL rules, and crashes simulated in-process (`Database.simulate_crash()` discards the
  unflushed log tail and unwritten pages), including torn pages, interrupted DDL and
  repeatedly interrupted recovery.
- `tests/property/test_crash_model.py`: random transactions, checkpoints and crashes against
  a model of committed and in-flight rows.
- `tests/crash/`: a child process runs transactions with a 6-frame pool and is killed with
  `Popen.kill()` at random times, or exits with `os._exit` at an injected point (before or
  after a log fsync, half-way through a page write, during undo). After each crash the parent
  checks that every reported commit is present, nothing uncommitted is present even in part,
  every index matches its heap, every B+ tree passes its checks, and the buffer pool holds no
  page twice. `PYDB_CRASH_ROUNDS` and `PYDB_CRASH_SEED` scale and vary the runs.

## Limits

- Concurrent sessions are isolated by table locks (see concurrency.md); one process per
  database file.
- Pages leaked by a crash are not reclaimed.
- The log is emptied only at a clean close; a long-running process keeps it growing.
