"""Concurrent sessions on one database: isolation, deadlocks, timeouts and recovery."""

import random
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pydb.catalog.integrity import check_table
from pydb.database import Database
from pydb.errors import DeadlockError, LockTimeoutError, TransactionError
from pydb.session import Session
from pydb.txn.lock_manager import LockMode


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "c.db")
    yield database
    database.close()


class Background:
    """Run a call on a thread; check whether it is still blocked; collect its result."""

    def __init__(self, call: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(call,), daemon=True)
        self.thread.start()

    def _run(self, call: Callable[[], Any]) -> None:
        try:
            self.result = call()
        except BaseException as exc:
            self.error = exc

    def blocked(self, wait: float = 0.2) -> bool:
        self.thread.join(wait)
        return self.thread.is_alive()

    def get(self) -> Any:
        self.thread.join(10)
        assert not self.thread.is_alive(), "still blocked"
        if self.error is not None:
            raise self.error
        return self.result


def wait_for_waiters(db: Database, count: int) -> None:
    deadline = time.monotonic() + 5
    while len(db.locks.waiting()) < count:
        assert time.monotonic() < deadline, "sessions never started waiting"
        time.sleep(0.005)


def setup_accounts(db: Database, n: int = 4, balance: int = 100) -> None:
    db.execute("CREATE TABLE acct (id INT PRIMARY KEY, balance INT NOT NULL)")
    db.execute("INSERT INTO acct VALUES " + ", ".join(f"({i}, {balance})" for i in range(1, n + 1)))


def value(session: Session | Database, sql: str) -> Any:
    return session.execute(sql).rows[0][0]


# --------------------------------------------------------------------- isolation


def test_reader_waits_for_writer_and_sees_only_committed_data(db: Database) -> None:
    setup_accounts(db)
    writer, reader = db.session(), db.session()
    writer.execute("BEGIN")
    writer.execute("UPDATE acct SET balance = 0 WHERE id = 1")
    read = Background(lambda: value(reader, "SELECT balance FROM acct WHERE id = 1"))
    assert read.blocked()  # no dirty read: the uncommitted 0 is never visible
    writer.execute("COMMIT")
    assert read.get() == 0


def test_reader_sees_old_value_after_writer_rolls_back(db: Database) -> None:
    setup_accounts(db)
    writer, reader = db.session(), db.session()
    writer.execute("BEGIN")
    writer.execute("DELETE FROM acct")
    read = Background(lambda: value(reader, "SELECT COUNT(*) FROM acct"))
    assert read.blocked()
    writer.execute("ROLLBACK")
    assert read.get() == 4


def test_writer_waits_for_readers_transaction(db: Database) -> None:
    setup_accounts(db)
    reader, writer = db.session(), db.session()
    reader.execute("BEGIN")
    first = value(reader, "SELECT SUM(balance) FROM acct")
    update = Background(lambda: writer.execute("UPDATE acct SET balance = balance + 1"))
    assert update.blocked()  # repeatable read: the reader's table cannot change under it
    assert value(reader, "SELECT SUM(balance) FROM acct") == first
    reader.execute("COMMIT")
    assert update.get().rowcount == 4


def test_readers_share(db: Database) -> None:
    setup_accounts(db)
    a, b = db.session(), db.session()
    a.execute("BEGIN")
    b.execute("BEGIN")
    assert value(a, "SELECT COUNT(*) FROM acct") == value(b, "SELECT COUNT(*) FROM acct") == 4
    a.execute("COMMIT")
    b.execute("COMMIT")


def test_writers_to_different_tables_do_not_block(db: Database) -> None:
    db.execute("CREATE TABLE a (id INT)")
    db.execute("CREATE TABLE b (id INT)")
    one, two = db.session(), db.session()
    one.execute("BEGIN")
    two.execute("BEGIN")
    one.execute("INSERT INTO a VALUES (1)")
    two.execute("INSERT INTO b VALUES (2)")
    two.execute("COMMIT")
    one.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM a").rows == [(0,)]
    assert db.execute("SELECT COUNT(*) FROM b").rows == [(1,)]


def test_schema_change_waits_for_transactions_using_the_table(db: Database) -> None:
    setup_accounts(db)
    reader, admin = db.session(), db.session()
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM acct")
    drop = Background(lambda: admin.execute("DROP TABLE acct"))
    assert drop.blocked()
    reader.execute("COMMIT")
    drop.get()
    assert not db.catalog.has_table("acct")


# --------------------------------------------------------------------- deadlocks and timeouts


def test_deadlock_victim_is_rolled_back_and_the_other_proceeds(db: Database) -> None:
    db.execute("CREATE TABLE a (id INT)")
    db.execute("CREATE TABLE b (id INT)")
    one, two = db.session(), db.session()
    one.execute("BEGIN")
    one.execute("INSERT INTO a VALUES (1)")
    two.execute("BEGIN")
    two.execute("INSERT INTO b VALUES (2)")
    blocked = Background(lambda: one.execute("INSERT INTO b VALUES (1)"))
    wait_for_waiters(db, 1)
    with pytest.raises(DeadlockError):
        two.execute("INSERT INTO a VALUES (2)")
    assert not two.in_transaction  # the victim's whole transaction was rolled back
    blocked.get()
    one.execute("COMMIT")
    assert db.execute("SELECT * FROM a").rows == [(1,)]
    assert db.execute("SELECT * FROM b").rows == [(1,)]  # the victim's insert is gone


def test_lock_timeout_rolls_back_the_waiting_transaction(tmp_path: Path) -> None:
    with Database(tmp_path / "t.db", lock_timeout=0.2) as db:
        setup_accounts(db)
        holder, waiter = db.session(), db.session()
        holder.execute("BEGIN")
        holder.execute("UPDATE acct SET balance = 1")
        waiter.execute("BEGIN")
        with pytest.raises(LockTimeoutError):
            waiter.execute("SELECT * FROM acct")
        assert not waiter.in_transaction
        holder.execute("COMMIT")
        assert value(waiter, "SELECT MIN(balance) FROM acct") == 1


def test_autocommit_statement_that_times_out_leaves_nothing_behind(tmp_path: Path) -> None:
    with Database(tmp_path / "t.db", lock_timeout=0.1) as db:
        setup_accounts(db)
        holder, other = db.session(), db.session()
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM acct")
        with pytest.raises(LockTimeoutError):
            other.execute("DELETE FROM acct")
        assert db.tm.active_transactions == [holder.txn]
        assert db.locks.waiting() == {}
        assert holder.txn is not None
        assert db.locks.holders(("table", "acct")) == {holder.txn.txn_id: LockMode.SHARED}
        holder.execute("COMMIT")
        assert value(other, "SELECT COUNT(*) FROM acct") == 4


# --------------------------------------------------------------------- stress


def run_with_retry(session: Session, statements: list[str]) -> int:
    """Run statements as one transaction, retrying after deadlocks. Return the retries."""
    retries = 0
    while True:
        try:
            session.execute("BEGIN")
            for sql in statements:
                session.execute(sql)
            session.execute("COMMIT")
            return retries
        except DeadlockError:
            retries += 1
            assert not session.in_transaction


def test_concurrent_read_modify_write_loses_no_updates(db: Database) -> None:
    """Each transaction reads a counter and writes back read+1, from application code.

    The read takes a SHARED lock and the write upgrades it, so concurrent transactions
    deadlock on the upgrade; the victims retry. Without locking, updates would be lost.
    """
    db.execute("CREATE TABLE counter (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    threads, increments = 6, 15
    retries: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        session = db.session()
        try:
            for _ in range(increments):
                while True:
                    try:
                        session.execute("BEGIN")
                        n = value(session, "SELECT n FROM counter WHERE id = 1")
                        session.execute(f"UPDATE counter SET n = {n + 1} WHERE id = 1")
                        session.execute("COMMIT")
                        break
                    except DeadlockError:
                        retries.append(1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            session.close()

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(60)
    assert not errors
    assert value(db, "SELECT n FROM counter") == threads * increments
    db.locks.check_invariants()


def test_transfers_preserve_the_total_seen_by_concurrent_readers(db: Database) -> None:
    db.execute("CREATE TABLE acct (id INT PRIMARY KEY, balance INT NOT NULL)")
    db.execute("CREATE TABLE audit (id INT, amount INT)")
    db.execute("INSERT INTO acct VALUES " + ", ".join(f"({i}, 1000)" for i in range(1, 11)))
    total = 10 * 1000
    stop = threading.Event()
    errors: list[BaseException] = []
    sums: list[int] = []

    def transfer(seed: int) -> None:
        rng = random.Random(seed)
        session = db.session()
        try:
            for _ in range(25):
                src, dst = rng.sample(range(1, 11), 2)
                amount = rng.randint(1, 50)
                # Locks acct then audit in one order and audit then acct in the other, so
                # transfers deadlock against each other; victims retry.
                statements = [
                    f"UPDATE acct SET balance = balance - {amount} WHERE id = {src}",
                    f"UPDATE acct SET balance = balance + {amount} WHERE id = {dst}",
                    f"INSERT INTO audit VALUES ({src}, {amount})",
                ]
                if rng.random() < 0.5:
                    statements.insert(0, statements.pop())
                run_with_retry(session, statements)
        except BaseException as exc:
            errors.append(exc)
        finally:
            session.close()

    def audit() -> None:
        session = db.session()
        try:
            while not stop.is_set():
                session.execute("BEGIN")
                sums.append(value(session, "SELECT SUM(balance) FROM acct"))
                session.execute("COMMIT")
        except BaseException as exc:
            errors.append(exc)
        finally:
            session.close()

    auditor = threading.Thread(target=audit)
    auditor.start()
    workers = [threading.Thread(target=transfer, args=(seed,)) for seed in range(5)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(60)
    stop.set()
    auditor.join(10)
    assert not errors
    assert sums and all(s == total for s in sums)  # never a half-done transfer
    assert value(db, "SELECT SUM(balance) FROM acct") == total
    assert value(db, "SELECT COUNT(*) FROM audit") == 5 * 25
    for table in db.catalog.tables():
        check_table(table)


# --------------------------------------------------------------------- closing and crashing


def test_close_rolls_back_every_open_session(tmp_path: Path) -> None:
    path = tmp_path / "close.db"
    db = Database(path)
    setup_accounts(db)
    writer, reader = db.session(), db.session()
    writer.execute("BEGIN")
    writer.execute("INSERT INTO acct VALUES (10, 1)")
    reader.execute("BEGIN")
    db.close()
    assert writer.closed and reader.closed
    with Database(path) as reopened:
        assert value(reopened, "SELECT COUNT(*) FROM acct") == 4
        assert reopened.recovery.losers == []  # a clean close needs no recovery


def test_close_wakes_a_session_waiting_for_a_lock(tmp_path: Path) -> None:
    db = Database(tmp_path / "w.db")
    setup_accounts(db)
    holder, waiter = db.session(), db.session()
    holder.execute("BEGIN")
    holder.execute("DELETE FROM acct")
    blocked = Background(lambda: waiter.execute("SELECT * FROM acct"))
    wait_for_waiters(db, 1)
    db.close()
    with pytest.raises(TransactionError):
        blocked.get()
    with Database(tmp_path / "w.db") as reopened:
        assert value(reopened, "SELECT COUNT(*) FROM acct") == 4


def test_closed_session_and_database_refuse_statements(db: Database) -> None:
    session = db.session()
    session.close()
    with pytest.raises(TransactionError, match="session is closed"):
        session.execute("SELECT 1")


def test_crash_with_interleaved_sessions_keeps_only_committed_work(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    db = Database(path, pool_size=16)
    for name in ("a", "b", "c"):
        db.execute(f"CREATE TABLE {name} (id INT PRIMARY KEY, pad TEXT)")
    sessions = {name: db.session() for name in ("a", "b", "c")}
    for session in sessions.values():
        session.execute("BEGIN")
    pad = "x" * 200
    for i in range(60):  # statements of the three transactions interleave in the log
        for name, session in sessions.items():
            session.execute(f"INSERT INTO {name} VALUES ({i}, '{pad}')")
            if i % 7 == 0:
                session.execute(f"DELETE FROM {name} WHERE id = {i}")
    sessions["b"].execute("COMMIT")
    db.simulate_crash()
    with Database(path) as reopened:
        assert len(reopened.recovery.losers) == 2
        counts = {name: value(reopened, f"SELECT COUNT(*) FROM {name}") for name in ("a", "b", "c")}
        assert counts == {"a": 0, "b": 60 - 9, "c": 0}
        for table in reopened.catalog.tables():
            check_table(table)
