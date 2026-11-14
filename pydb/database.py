"""An open database: storage, write-ahead log, catalog, executor, locks and sessions.

Opening a database runs crash recovery from its log (``<path>.wal``), then takes a
checkpoint. SQL runs in sessions (see ``session.py``): each session has its own
transaction, and sessions on the same database are isolated by table locks. A
``Database`` has a built-in session, used by :meth:`execute`, for single-user code;
:meth:`session` opens more, one per concurrent client.

A database file must be opened by one ``Database`` (one process) at a time; to share it,
open sessions on one ``Database``, or run the server (``python -m pydb.server``).

``close()`` rolls back every open transaction, writes every page, checkpoints and empties
the log, so a cleanly closed database needs no recovery.
"""

import os
import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from pydb.catalog.catalog import Catalog
from pydb.config import BUFFER_POOL_SIZE, INVALID_PAGE_ID, LOCK_TIMEOUT_SECONDS
from pydb.execution.executor import Executor, Result
from pydb.planner.physical_plan import PlannerSettings
from pydb.session import Session
from pydb.sql import ast
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.txn.lock_manager import LockManager
from pydb.txn.recovery import RecoveryReport, recover
from pydb.txn.transaction import Transaction
from pydb.txn.transaction_manager import TransactionManager
from pydb.txn.wal import LogManager


class Database:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        pool_size: int = BUFFER_POOL_SIZE,
        settings: PlannerSettings | None = None,
        lock_timeout: float | None = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.wal_path = Path(f"{self.path}.wal")
        self.settings = settings or PlannerSettings()
        self.latch = threading.RLock()
        """Held while a statement runs inside the engine; one statement at a time."""
        self.locks = LockManager(lock_timeout)
        self._sessions: set[Session] = set()
        self._sessions_lock = threading.Lock()
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
            self._default = self.session()
        except BaseException:
            self.log.abandon()
            self.disk.abandon()
            raise

    @property
    def catalog(self) -> Catalog:
        assert self._catalog is not None
        return self._catalog

    @property
    def closed(self) -> bool:
        return self.disk.closed

    # ----------------------------------------------------------------- sessions

    def session(self) -> Session:
        """Open a new session: a separate connection with its own transaction."""
        with self._sessions_lock:
            if self.disk.closed:
                raise ValueError("the database is closed")
            session = Session(self)
            self._sessions.add(session)
            return session

    def forget_session(self, session: Session) -> None:
        with self._sessions_lock:
            self._sessions.discard(session)

    @property
    def sessions(self) -> list[Session]:
        with self._sessions_lock:
            return list(self._sessions)

    # ----------------------------------------------------------------- the built-in session

    @property
    def txn(self) -> Transaction | None:
        return self._default.txn

    @property
    def in_transaction(self) -> bool:
        return self._default.in_transaction

    def execute(self, sql: str, params: Sequence[object] | None = None) -> Result:
        """Run one SQL statement in the built-in session."""
        return self._default.execute(sql, params)

    def execute_script(self, sql: str) -> list[Result]:
        """Run statements separated by semicolons, stopping at the first error."""
        return self._default.execute_script(sql)

    def execute_statement(self, statement: ast.Statement) -> Result:
        return self._default.execute_statement(statement)

    # ----------------------------------------------------------------- maintenance

    def checkpoint(self) -> None:
        with self.latch:
            self.tm.checkpoint()

    def index_table(self, index_name: str) -> str | None:
        """The table an index belongs to, or None if there is no such index."""
        found = self.catalog.find_index(index_name)
        return None if found is None else found[0].name

    def reload_catalog(self) -> None:
        """Re-read the catalog from its pages (after a schema change was rolled back)."""
        self._catalog = Catalog(self.pool, migrate=False)
        self.executor = Executor(self._catalog, self.settings, self.tm)

    # ----------------------------------------------------------------- closing

    def close(self) -> None:
        if self.disk.closed:
            return
        self.locks.close()  # sessions waiting for a lock give up
        with self.latch:
            try:
                for session in self.sessions:
                    session.close()  # rolls back its transaction
                for txn in self.tm.active_transactions:  # statements that gave up waiting
                    self.tm.abort(txn)
                self.tm.checkpoint()
                if not self.tm.active_transactions:
                    self.log.truncate()
            finally:
                self.log.close()
                self.disk.close()

    def simulate_crash(self) -> None:
        """Stop as a crash would: unflushed log records and dirty pages are lost."""
        self.locks.close()
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

    def _catalog_for_undo(self) -> Catalog:
        if self._catalog is None:  # during recovery
            self._catalog = Catalog(self.pool, migrate=False)
        return self._catalog
