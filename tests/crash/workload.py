"""Child process for the crash tests. Not a test module.

Usage::

    python workload.py DB run START COUNT     transactions START, START+1, ...
    python workload.py DB abandon START ROWS  one big transaction that never commits
    python workload.py DB open                just open (run recovery) and stop

Every transaction ``k`` inserts rows ``10k .. 10k+2`` into ``items`` (with ``txn = k``),
adds 1 to ``counters`` row ``k % 5``, and deletes the rows of transaction ``k - 15``.
Transactions with ``k % 7 == 0`` end with ROLLBACK, the others with COMMIT. Progress is
printed (``BEGIN k``, ``COMMITTED k``, ``ROLLED k``) only after each step has returned.

``PYDB_FAULT=point:n`` makes the process die with ``os._exit`` at the n-th occurrence of:

* ``log_flush``    a log flush, before anything is written;
* ``log_flushed``  a log flush, right after the records became durable;
* ``page_write``   a data page write, after only half of the page reached the file;
* ``undo_step``    a compensation during undo (with its CLR durable).

The process never closes the database: it ends by being killed or by ``os._exit``, as in a
crash.
"""

import os
import random
import sys

from pydb.config import PAGE_SIZE
from pydb.database import Database
from pydb.storage import page
from pydb.storage.disk_manager import DiskManager
from pydb.txn.transaction import Transaction
from pydb.txn.transaction_manager import TransactionManager
from pydb.txn.wal import ClrKind, LogManager, LogRecord


def install_fault(spec: str) -> None:
    point, count_text = spec.split(":")
    target = int(count_text)
    calls = [0]

    def due() -> bool:
        calls[0] += 1
        return calls[0] == target

    if point in ("log_flush", "log_flushed"):
        original_flush = LogManager.flush

        def flush(self: LogManager, up_to_lsn: int | None = None) -> None:
            if point == "log_flush" and due():
                os._exit(70)
            original_flush(self, up_to_lsn)
            if point == "log_flushed" and due():
                os._exit(71)

        LogManager.flush = flush  # type: ignore[method-assign]
    elif point == "page_write":
        original_write = DiskManager._write_raw

        def write(self: DiskManager, page_id: int, buf: bytearray) -> None:
            if due():
                page.seal(buf)
                file = self._require_open()
                file.seek(page_id * PAGE_SIZE)
                file.write(bytes(buf[: PAGE_SIZE // 2]))  # a torn write
                os._exit(72)
            original_write(self, page_id, buf)

        DiskManager._write_raw = write  # type: ignore[method-assign]
    elif point == "undo_step":
        original_compensate = TransactionManager._compensate

        def compensate(
            self: TransactionManager, txn: Transaction, record: LogRecord, kind: ClrKind
        ) -> None:
            original_compensate(self, txn, record, kind)
            if due():
                self.log.flush()
                os._exit(73)

        TransactionManager._compensate = compensate  # type: ignore[method-assign]
    else:
        raise SystemExit(f"unknown fault point {point}")


def say(text: str) -> None:
    print(text, flush=True)


def run(db: Database, start: int, count: int) -> None:
    for k in range(start, start + count):
        rng = random.Random(k)
        say(f"BEGIN {k}")
        db.execute("BEGIN")
        values = ", ".join(f"({10 * k + i}, {k}, '{'p' * rng.randint(0, 900)}')" for i in range(3))
        db.execute(f"INSERT INTO items VALUES {values}")
        db.execute(f"UPDATE counters SET n = n + 1 WHERE id = {k % 5}")
        db.execute(f"DELETE FROM items WHERE txn = {k - 15}")
        if k % 7 == 0:
            db.execute("ROLLBACK")
            say(f"ROLLED {k}")
        else:
            db.execute("COMMIT")
            say(f"COMMITTED {k}")


def abandon(db: Database, start: int, rows: int) -> None:
    db.execute("BEGIN")
    for chunk in range(0, rows, 50):
        values = ", ".join(
            f"({10 * start + i}, {start}, '{'b' * 300}')"
            for i in range(chunk, min(chunk + 50, rows))
        )
        db.execute(f"INSERT INTO items VALUES {values}")
    db.execute("UPDATE counters SET n = n + 1000")
    db.log.flush()
    say(f"ABANDONED {start}")
    os._exit(0)


def main() -> None:
    path, mode, *args = sys.argv[1:]
    if os.environ.get("PYDB_FAULT"):
        install_fault(os.environ["PYDB_FAULT"])
    db = Database(path, pool_size=6)  # a tiny pool, so dirty uncommitted pages get written
    say("READY")
    if mode == "run":
        run(db, int(args[0]), int(args[1]))
    elif mode == "abandon":
        abandon(db, int(args[0]), int(args[1]))
    os._exit(0)


if __name__ == "__main__":
    main()
