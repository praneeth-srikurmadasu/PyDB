"""A session: one client's connection to an open database, with its own transaction.

Sessions share the database's storage, catalog and log. Two mechanisms keep them apart:

* **Table locks** (strict two-phase locking, see ``txn/lock_manager.py``) isolate
  *transactions*. A statement takes the locks it needs before it runs and keeps them
  until its transaction ends, so concurrent transactions are serializable.
* **The engine latch** isolates *statements*. Only one statement runs inside the engine at
  a time, so page-level structures (B+ trees, heaps, the free list, the log) see one
  operation at a time, as crash recovery assumes. Lock waits happen *before* a statement
  takes the latch, so a transaction waiting for a lock never blocks the one holding it.

Transactions:

* outside ``BEGIN``, each statement is its own transaction (autocommit);
* after ``BEGIN``, statements join that transaction until ``COMMIT`` or ``ROLLBACK``; a
  statement that fails is rolled back on its own and the transaction stays open, except
  after a deadlock or lock timeout, which roll back the whole transaction so that its locks
  are released;
* DDL and ``ANALYZE`` always run as their own transaction and are refused inside ``BEGIN``.
"""

from collections.abc import Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Self

from pydb.errors import DeadlockError, LockTimeoutError, TransactionError
from pydb.execution.executor import Result
from pydb.sql import ast
from pydb.sql.parser import parse, parse_script
from pydb.txn.lock_plan import required_locks
from pydb.txn.transaction import Transaction

if TYPE_CHECKING:
    from pydb.database import Database

SCHEMA_STATEMENTS = (ast.CreateTable, ast.DropTable, ast.CreateIndex, ast.DropIndex, ast.Analyze)


class Session:
    def __init__(self, db: "Database") -> None:
        self.db = db
        self.txn: Transaction | None = None
        self.closed = False

    @property
    def in_transaction(self) -> bool:
        return self.txn is not None

    # ----------------------------------------------------------------- running SQL

    def execute(self, sql: str, params: Sequence[object] | None = None) -> Result:
        """Run one SQL statement. Each ``?`` in it takes the next value of ``params``."""
        return self.execute_statement(parse(sql, params))

    def execute_script(self, sql: str) -> list[Result]:
        """Run statements separated by semicolons, stopping at the first error."""
        return [self.execute_statement(statement) for statement in parse_script(sql)]

    def execute_statement(self, statement: ast.Statement) -> Result:
        if self.closed:
            closed = "database" if self.db.closed else "session"
            raise TransactionError(f"the {closed} is closed")
        match statement:
            case ast.Begin():
                if self.txn is not None:
                    raise TransactionError("a transaction is already in progress")
                self.txn = self.db.tm.begin()
                return Result(message="BEGIN")
            case ast.Commit():
                self._end(self._take_transaction("COMMIT"), commit=True)
                return Result(message="COMMIT")
            case ast.Rollback():
                self._end(self._take_transaction("ROLLBACK"), commit=False)
                return Result(message="ROLLBACK")
        if isinstance(statement, SCHEMA_STATEMENTS):
            if self.txn is not None:
                name = type(statement).__name__.upper()
                raise TransactionError(
                    f"{name} cannot run inside a transaction; COMMIT or ROLLBACK first"
                )
            return self._run_alone(statement, physical=True)
        if self.txn is None:
            return self._run_alone(statement, physical=False)
        return self._run_in_transaction(self.txn, statement)

    # ----------------------------------------------------------------- closing

    def close(self) -> None:
        """Roll back any open transaction and detach from the database."""
        if self.closed:
            return
        try:
            if self.txn is not None:
                txn, self.txn = self.txn, None
                self._end(txn, commit=False)
        finally:
            self.closed = True
            self.db.forget_session(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ----------------------------------------------------------------- internals

    def _run_alone(self, statement: ast.Statement, *, physical: bool) -> Result:
        """Run ``statement`` as its own transaction."""
        db = self.db
        txn = db.tm.begin(physical=physical)
        try:
            self._lock(txn, statement)
            with db.latch:
                try:
                    with db.tm.activate(txn):
                        result = db.executor.execute(statement)
                except BaseException:
                    db.tm.abort(txn)
                    if physical:
                        db.reload_catalog()  # the in-memory catalog may hold the undone change
                    raise
                db.tm.commit(txn)
            return result
        finally:
            if txn.active:  # failed before taking the latch, e.g. a deadlock
                with db.latch:
                    db.tm.abort(txn)
            db.locks.release_all(txn.txn_id)

    def _run_in_transaction(self, txn: Transaction, statement: ast.Statement) -> Result:
        db = self.db
        try:
            self._lock(txn, statement)
        except (DeadlockError, LockTimeoutError):
            self.txn = None
            self._end(txn, commit=False)
            raise
        with db.latch:
            savepoint = txn.last_lsn
            try:
                with db.tm.activate(txn):
                    return db.executor.execute(statement)
            except BaseException:
                db.tm.rollback_to(txn, savepoint)
                raise

    def _lock(self, txn: Transaction, statement: ast.Statement) -> None:
        """Take the statement's table locks, in name order so statements agree on order."""
        db = self.db
        with db.latch:  # the catalog is only read under the latch
            locks = required_locks(statement, db.index_table)
        for resource in sorted(locks):
            db.locks.acquire(txn.txn_id, resource, locks[resource])

    def _end(self, txn: Transaction, *, commit: bool) -> None:
        db = self.db
        try:
            with db.latch:
                if commit:
                    db.tm.commit(txn)
                else:
                    db.tm.abort(txn)
        finally:
            db.locks.release_all(txn.txn_id)

    def _take_transaction(self, verb: str) -> Transaction:
        if self.txn is None:
            raise TransactionError(f"{verb} without BEGIN: no transaction is in progress")
        txn, self.txn = self.txn, None
        return txn
