"""An open database: storage, write-ahead log, catalog and executor, plus one session.

Opening a database runs crash recovery from its log (``<path>.wal``), then takes a
checkpoint. Statements run in transactions:

* outside ``BEGIN``, each statement is its own transaction (autocommit);
* after ``BEGIN``, statements join that transaction until ``COMMIT`` or ``ROLLBACK``; a
  statement that fails is rolled back on its own and the transaction stays open;
* DDL and ``ANALYZE`` always run as their own transaction and are refused inside ``BEGIN``.

``close()`` rolls back an open transaction, writes every page, checkpoints and empties the
log, so a cleanly closed database needs no recovery.
"""

import os
from pathlib import Path
from types import TracebackType
from typing import Self

from pydb.catalog.catalog import Catalog
from pydb.config import BUFFER_POOL_SIZE, INVALID_PAGE_ID
from pydb.errors import TransactionError
from pydb.execution.executor import Executor, Result
from pydb.planner.physical_plan import PlannerSettings
from pydb.sql import ast
from pydb.sql.parser import parse, parse_script
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.txn.recovery import RecoveryReport, recover
from pydb.txn.transaction import Transaction
from pydb.txn.transaction_manager import TransactionManager
from pydb.txn.wal import LogManager

SCHEMA_STATEMENTS = (ast.CreateTable, ast.DropTable, ast.CreateIndex, ast.DropIndex, ast.Analyze)


class Database:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        pool_size: int = BUFFER_POOL_SIZE,
        settings: PlannerSettings | None = None,
    ) -> None:
        self.path = Path(path)
        self.wal_path = Path(f"{self.path}.wal")
        self.settings = settings or PlannerSettings()
        self.disk = DiskManager(self.path)
        try:
            self.log = LogManager(self.wal_path)
        except BaseException:
            self.disk.close()
            raise
        try:
            self.pool = BufferPoolManager(self.disk, pool_size, log_flusher=self.log.flush)
            self.tm = TransactionManager(self.log, self.pool)
            self._catalog: Catalog | None = None
            self.tm.catalog_provider = self._catalog_for_undo
            self.recovery: RecoveryReport = recover(self.tm)
            self._catalog = self._open_catalog()
            self.executor = Executor(self._catalog, self.settings, self.tm)
            self.tm.checkpoint()
            self.txn: Transaction | None = None
        except BaseException:
            self.log.abandon()
            self.disk.abandon()
            raise

    @property
    def catalog(self) -> Catalog:
        assert self._catalog is not None
        return self._catalog

    @property
    def in_transaction(self) -> bool:
        return self.txn is not None

    # ----------------------------------------------------------------- running SQL

    def execute(self, sql: str) -> Result:
        """Run one SQL statement."""
        return self.execute_statement(parse(sql))

    def execute_script(self, sql: str) -> list[Result]:
        """Run statements separated by semicolons, stopping at the first error."""
        return [self.execute_statement(statement) for statement in parse_script(sql)]

    def execute_statement(self, statement: ast.Statement) -> Result:
        match statement:
            case ast.Begin():
                if self.txn is not None:
                    raise TransactionError("a transaction is already in progress")
                self.txn = self.tm.begin()
                return Result(message="BEGIN")
            case ast.Commit():
                txn = self._take_transaction("COMMIT")
                self.tm.commit(txn)
                return Result(message="COMMIT")
            case ast.Rollback():
                txn = self._take_transaction("ROLLBACK")
                self.tm.abort(txn)
                return Result(message="ROLLBACK")
        if isinstance(statement, SCHEMA_STATEMENTS):
            return self._run_schema_change(statement)
        if self.txn is not None:
            savepoint = self.txn.last_lsn
            try:
                with self.tm.activate(self.txn):
                    return self.executor.execute(statement)
            except BaseException:
                self.tm.rollback_to(self.txn, savepoint)
                raise
        txn = self.tm.begin()
        try:
            with self.tm.activate(txn):
                result = self.executor.execute(statement)
        except BaseException:
            self.tm.abort(txn)
            raise
        self.tm.commit(txn)
        return result

    def checkpoint(self) -> None:
        self.tm.checkpoint()

    # ----------------------------------------------------------------- closing

    def close(self) -> None:
        if self.disk.closed:
            return
        try:
            if self.txn is not None:
                self.tm.abort(self.txn)
                self.txn = None
            self.tm.checkpoint()
            if not self.tm.active_transactions:
                self.log.truncate()
        finally:
            self.log.close()
            self.disk.close()

    def simulate_crash(self) -> None:
        """Stop as a crash would: unflushed log records and dirty pages are lost."""
        self.log.abandon()
        self.disk.abandon()

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

    def _take_transaction(self, verb: str) -> Transaction:
        if self.txn is None:
            raise TransactionError(f"{verb} without BEGIN: no transaction is in progress")
        txn, self.txn = self.txn, None
        return txn

    def _run_schema_change(self, statement: ast.Statement) -> Result:
        if self.txn is not None:
            name = type(statement).__name__.upper()
            raise TransactionError(
                f"{name} cannot run inside a transaction; COMMIT or ROLLBACK first"
            )
        txn = self.tm.begin(physical=True)
        try:
            with self.tm.activate(txn):
                result = self.executor.execute(statement)
        except BaseException:
            self.tm.abort(txn)
            self._reload_catalog()  # the in-memory catalog may hold the undone change
            raise
        self.tm.commit(txn)
        return result

    def _open_catalog(self) -> Catalog:
        if self.disk.catalog_root == INVALID_PAGE_ID:
            txn = self.tm.begin(physical=True)
            with self.tm.activate(txn):
                catalog = Catalog(self.pool)
            self.tm.commit(txn)
            catalog.record_root()
            return catalog
        catalog = Catalog(self.pool, migrate=False)
        if catalog.needs_migration():
            txn = self.tm.begin(physical=True)
            with self.tm.activate(txn):
                catalog.migrate()
            self.tm.commit(txn)
        return catalog

    def _reload_catalog(self) -> None:
        self._catalog = Catalog(self.pool, migrate=False)
        self.executor = Executor(self._catalog, self.settings, self.tm)

    def _catalog_for_undo(self) -> Catalog:
        if self._catalog is None:  # during recovery
            self._catalog = Catalog(self.pool, migrate=False)
        return self._catalog
