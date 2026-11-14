"""Transactions, rollback and crash recovery, with crashes simulated in-process.

``Database.simulate_crash()`` drops the unflushed log tail and every dirty page that was not
yet written, exactly what a killed process loses. Real process kills are in tests/crash.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.integrity import check_table
from pydb.config import PAGE_SIZE
from pydb.database import Database
from pydb.errors import TransactionError, UniqueViolationError
from pydb.sql.parser import parse
from pydb.storage.slotted_page import SlottedPage
from pydb.storage.tuple import Row

SCHEMA = "CREATE TABLE t (id INT PRIMARY KEY, grp INT, pad TEXT); CREATE INDEX t_grp ON t (grp)"


def rows(db: Database, sql: str = "SELECT id, grp FROM t ORDER BY id") -> list[Row]:
    return db.execute(sql).rows


def consistent(db: Database) -> int:
    return check_table(db.catalog.get_table("t"))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    p = tmp_path / "txn.db"
    with Database(p) as db:
        db.execute_script(SCHEMA)
    return p


@pytest.fixture
def db(path: Path) -> Iterator[Database]:
    with Database(path, pool_size=8) as database:
        yield database


def insert(db: Database, ids: range, grp: int = 0, pad: int = 10) -> None:
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, {grp}, '{'p' * pad}')" for i in ids))


# ------------------------------------------------------------------ BEGIN / COMMIT / ROLLBACK


def test_commit_makes_changes_visible_and_durable(path: Path) -> None:
    db = Database(path, pool_size=8)
    db.execute("BEGIN")
    assert db.in_transaction
    insert(db, range(5))
    db.execute("UPDATE t SET grp = 1 WHERE id < 2")
    db.execute("COMMIT")
    assert not db.in_transaction
    db.simulate_crash()  # nothing was closed or flushed apart from the log
    with Database(path) as reopened:
        assert rows(reopened) == [(0, 1), (1, 1), (2, 0), (3, 0), (4, 0)]
        assert consistent(reopened) == 5


def test_rollback_undoes_inserts_updates_and_deletes(db: Database) -> None:
    insert(db, range(10))
    db.execute("BEGIN")
    insert(db, range(10, 20))
    db.execute("UPDATE t SET grp = 7, pad = 'longer value' WHERE id < 5")
    db.execute("DELETE FROM t WHERE id >= 5 AND id < 10")
    assert len(rows(db)) == 15
    db.execute("ROLLBACK")
    assert rows(db) == [(i, 0) for i in range(10)]
    assert consistent(db) == 10
    insert(db, range(10, 12))  # keys freed by the rollback are usable again
    assert consistent(db) == 12


def test_failed_statement_rolls_back_only_itself(db: Database) -> None:
    db.execute("BEGIN")
    insert(db, range(3))
    with pytest.raises(UniqueViolationError):
        db.execute("INSERT INTO t VALUES (10, 0, 'a'), (11, 0, 'b'), (1, 0, 'dup')")
    assert db.in_transaction
    insert(db, range(3, 5))
    db.execute("COMMIT")
    assert [r[0] for r in rows(db)] == [0, 1, 2, 3, 4]
    assert consistent(db) == 5


def test_autocommit_statement_failure_changes_nothing(db: Database) -> None:
    insert(db, range(3))
    with pytest.raises(UniqueViolationError):
        db.execute("UPDATE t SET id = 1")
    assert [r[0] for r in rows(db)] == [0, 1, 2]
    assert consistent(db) == 3


def test_deleted_space_is_reserved_until_commit(db: Database) -> None:
    insert(db, range(30), pad=100)
    table = db.catalog.get_table("t")
    first = table.heap.first_page_id

    def free_space() -> int:
        with db.pool.page(first) as frame:
            return SlottedPage(frame.data).reclaimable_space()

    full = free_space()
    db.execute("BEGIN")
    db.execute("DELETE FROM t")
    assert free_space() == full  # deleted rows keep their bytes so ROLLBACK can restore them
    db.execute("COMMIT")
    assert free_space() > full + 1000  # committed: the space is free
    assert consistent(db) == 0


def test_schema_changes_are_refused_inside_a_transaction(db: Database) -> None:
    db.execute("BEGIN")
    with pytest.raises(TransactionError, match="cannot run inside a transaction"):
        db.execute("CREATE TABLE u (a INT)")
    with pytest.raises(TransactionError):
        db.execute("ANALYZE t")
    db.execute("ROLLBACK")


def test_failed_schema_change_is_undone(db: Database) -> None:
    insert(db, range(5))
    db.execute("INSERT INTO t VALUES (100, 0, 'x')")
    with pytest.raises(UniqueViolationError):
        db.execute("CREATE UNIQUE INDEX t_grp_unique ON t (grp)")
    assert db.catalog.find_index("t_grp_unique") is None
    db.execute("CREATE INDEX t_pad ON t (pad)")
    assert consistent(db) == 6


def test_read_only_statements_write_no_log(db: Database) -> None:
    insert(db, range(3))
    written = db.log.records_written
    rows(db)
    db.execute("BEGIN")
    rows(db)
    db.execute("COMMIT")
    assert db.log.records_written == written


def test_close_rolls_back_an_open_transaction(path: Path) -> None:
    with Database(path) as db:
        db.execute("BEGIN")
        insert(db, range(5))
    with Database(path) as db:
        assert rows(db) == []


def test_clean_close_empties_the_log(path: Path) -> None:
    with Database(path) as db:
        insert(db, range(50))
    assert Path(f"{path}.wal").stat().st_size <= 64


# ------------------------------------------------------------------ crash recovery


def test_uncommitted_changes_vanish_after_a_crash(path: Path) -> None:
    db = Database(path, pool_size=4)  # tiny pool: dirty uncommitted pages reach the disk
    insert(db, range(20))
    db.execute("BEGIN")
    insert(db, range(100, 400), grp=5, pad=200)
    db.execute("UPDATE t SET grp = 9 WHERE id < 10")
    db.execute("DELETE FROM t WHERE id >= 10 AND id < 20")
    assert db.pool.stats.evictions > 0
    db.log.flush()  # even with every record durable, nothing committed
    db.simulate_crash()
    with Database(path) as reopened:
        assert len(reopened.recovery.losers) == 1
        assert rows(reopened) == [(i, 0) for i in range(20)]
        assert consistent(reopened) == 20


def test_committed_survive_and_uncommitted_vanish_together(path: Path) -> None:
    db = Database(path, pool_size=4)
    for start in range(0, 100, 10):
        db.execute("BEGIN")
        insert(db, range(start, start + 10), grp=start // 10)
        db.execute("COMMIT")
    db.execute("BEGIN")
    db.execute("DELETE FROM t WHERE grp < 5")
    insert(db, range(500, 520), grp=99)
    db.simulate_crash()
    with Database(path) as reopened:
        assert [r[0] for r in rows(reopened)] == list(range(100))
        assert consistent(reopened) == 100


def test_recovery_after_a_checkpoint(path: Path) -> None:
    db = Database(path, pool_size=4)
    insert(db, range(30))
    db.execute("BEGIN")
    insert(db, range(30, 60))
    db.checkpoint()  # the open transaction is recorded in the checkpoint
    insert(db, range(60, 90))
    db.simulate_crash()
    with Database(path) as reopened:
        assert [r[0] for r in rows(reopened)] == list(range(30))
        assert consistent(reopened) == 30


def test_schema_change_without_commit_is_undone(path: Path) -> None:
    db = Database(path, pool_size=8)
    insert(db, range(10))
    txn = db.tm.begin(physical=True)
    with db.tm.activate(txn):
        db.executor.execute(parse("CREATE TABLE u (a INT)"))
        db.executor.execute(parse("DROP TABLE t"))
    db.log.flush()
    db.simulate_crash()  # crashed before the DDL transaction committed
    with Database(path) as reopened:
        assert not reopened.catalog.has_table("u")
        assert [r[0] for r in rows(reopened)] == list(range(10))
        assert consistent(reopened) == 10


def test_torn_page_is_repaired_from_its_full_image(path: Path) -> None:
    db = Database(path, pool_size=8)
    insert(db, range(40), pad=50)
    db.execute("BEGIN")
    db.execute("UPDATE t SET grp = 3 WHERE id < 40")
    db.execute("COMMIT")
    table_page = db.catalog.get_table("t").heap.first_page_id
    db.simulate_crash()
    with open(path, "r+b") as f:  # the page's last write was torn half-way
        f.seek(table_page * PAGE_SIZE + PAGE_SIZE // 2)
        f.write(b"\xab" * (PAGE_SIZE // 2))
    with Database(path) as reopened:
        assert rows(reopened) == [(i, 3) for i in range(40)]
        assert consistent(reopened) == 40


def test_recovery_is_idempotent(path: Path) -> None:
    db = Database(path, pool_size=4)
    insert(db, range(10))
    db.execute("BEGIN")
    insert(db, range(10, 200), pad=100)
    db.log.flush()
    db.simulate_crash()
    for _ in range(3):  # each recovery here is itself cut off before closing
        crashed = Database(path, pool_size=4)
        crashed.simulate_crash()
    with Database(path) as reopened:
        assert [r[0] for r in rows(reopened)] == list(range(10))
        assert consistent(reopened) == 10


def test_new_pages_are_rebuilt_when_nothing_reached_the_disk(path: Path) -> None:
    # A large pool means none of the transaction's pages are written before the crash, so
    # pages it allocated (B+ tree splits, new heap pages) exist only in the log. Their first
    # log record must not be skipped by redo.
    db = Database(path, pool_size=256)
    db.execute("BEGIN")
    for start in range(0, 600, 50):
        insert(db, range(start, start + 50), grp=1, pad=300)
    db.execute("COMMIT")
    db.execute("BEGIN")
    insert(db, range(1000, 1400), grp=2, pad=300)
    db.log.flush()
    db.simulate_crash()
    with Database(path) as reopened:
        assert [r[0] for r in rows(reopened)] == list(range(600))
        assert consistent(reopened) == 600


def test_pages_freed_before_the_crash_can_be_reused_by_undo(path: Path) -> None:
    # Redo reads every page named in the log, including B+ tree nodes that the loser's
    # deletes freed. Undo then re-inserts the keys, and the splits allocate those very pages
    # again; the cached copies of the freed pages must not linger in the pool.
    db = Database(path, pool_size=64)
    for start in range(0, 2000, 100):
        insert(db, range(start, start + 100))
    db.checkpoint()
    db.execute("BEGIN")
    db.execute("DELETE FROM t WHERE id < 1900")
    db.log.flush()
    db.simulate_crash()
    with Database(path, pool_size=64) as reopened:
        reopened.pool.check_invariants()
        assert consistent(reopened) == 2000
        reopened.pool.check_invariants()
