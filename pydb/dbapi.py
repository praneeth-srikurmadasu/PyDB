"""A PEP 249 (DB-API 2.0) interface to an embedded PyDB database.

::

    import pydb.dbapi

    with pydb.dbapi.connect("shop.db") as conn:      # commits on success
        cur = conn.cursor()
        cur.execute("INSERT INTO items VALUES (?, ?)", (1, "pen"))
        cur.execute("SELECT name FROM items WHERE id = ?", (1,))
        print(cur.fetchone())
    conn.close()

**Transactions.** As PEP 249 requires, changes are not committed until :meth:`Connection.commit`:
a transaction is opened implicitly before the first ``INSERT``, ``UPDATE`` or ``DELETE`` and
ended by ``commit()`` or ``rollback()``. Queries outside a transaction run on their own, and
schema changes (``CREATE``, ``DROP``, ``ANALYZE``) run immediately and are refused while a
transaction is open, so commit first. Set ``autocommit = True`` to commit every statement
as it runs.

**Several connections.** Connections to the same file in one process share one open
database (one buffer pool, one log) and each has its own session, so they are isolated from
each other by table locks exactly like clients of the server. The database is closed when
its last connection closes. A file must not be opened by two processes at once.
"""

import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydb import errors as pydb_errors
from pydb.config import BUFFER_POOL_SIZE, LOCK_TIMEOUT_SECONDS
from pydb.database import Database
from pydb.execution.executor import Result
from pydb.session import Session
from pydb.sql import ast
from pydb.sql.parser import parse
from pydb.storage.tuple import Row

apilevel = "2.0"
threadsafety = 1  # threads may share the module, but not connections
paramstyle = "qmark"


# --------------------------------------------------------------------- exceptions


class Warning(Exception):  # noqa: N818 - name required by PEP 249
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


_ERROR_MAP: tuple[tuple[type[pydb_errors.PyDBError], type[DatabaseError]], ...] = (
    (pydb_errors.ConstraintViolationError, IntegrityError),
    (pydb_errors.DuplicateKeyError, IntegrityError),
    (pydb_errors.TypeMismatchError, DataError),
    (pydb_errors.ValueTooLargeError, DataError),
    (pydb_errors.DivisionByZeroError, DataError),
    (pydb_errors.DeadlockError, OperationalError),
    (pydb_errors.LockTimeoutError, OperationalError),
    (pydb_errors.StorageError, OperationalError),
    (pydb_errors.LogCorruptError, OperationalError),
    (pydb_errors.NotSupportedError, NotSupportedError),
    (pydb_errors.SqlSyntaxError, ProgrammingError),
    (pydb_errors.ParameterError, ProgrammingError),
    (pydb_errors.CatalogError, ProgrammingError),
    (pydb_errors.GroupingError, ProgrammingError),
    (pydb_errors.TransactionError, ProgrammingError),
)


def _translate(exc: pydb_errors.PyDBError) -> DatabaseError:
    for source, target in _ERROR_MAP:
        if isinstance(exc, source):
            return target(str(exc))
    return DatabaseError(str(exc))


# --------------------------------------------------------------------- type objects


class _TypeObject:
    """Compares equal to every column type name in its group (PEP 249 type objects)."""

    def __init__(self, *names: str) -> None:
        self.names = frozenset(names)

    def __eq__(self, other: object) -> bool:
        return other in self.names

    def __hash__(self) -> int:
        return hash(self.names)


STRING = _TypeObject("TEXT")
NUMBER = _TypeObject("INT", "FLOAT", "BOOL")
BINARY = _TypeObject()
DATETIME = _TypeObject()
ROWID = _TypeObject()


def _unsupported(name: str) -> Any:
    def constructor(*args: object) -> Any:
        raise NotSupportedError(f"PyDB has no {name} type")

    constructor.__name__ = name
    return constructor


Date = _unsupported("Date")
Time = _unsupported("Time")
Timestamp = _unsupported("Timestamp")
DateFromTicks = _unsupported("DateFromTicks")
TimeFromTicks = _unsupported("TimeFromTicks")
TimestampFromTicks = _unsupported("TimestampFromTicks")
Binary = _unsupported("Binary")


# --------------------------------------------------------------------- shared databases


class _Shared:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.connections = 0


_open: dict[Path, _Shared] = {}
_open_lock = threading.Lock()


def connect(
    database: str | os.PathLike[str],
    *,
    pool_size: int = BUFFER_POOL_SIZE,
    lock_timeout: float | None = LOCK_TIMEOUT_SECONDS,
    autocommit: bool = False,
) -> "Connection":
    """Open a connection to the database file ``database``, creating it if missing.

    ``pool_size`` and ``lock_timeout`` apply when this call opens the file; later connections
    to an already open file share its settings.
    """
    path = Path(database).resolve()
    with _open_lock:
        shared = _open.get(path)
        if shared is None:
            try:
                shared = _Shared(Database(path, pool_size=pool_size, lock_timeout=lock_timeout))
            except pydb_errors.PyDBError as exc:
                raise _translate(exc) from exc
            except OSError as exc:
                raise OperationalError(f"cannot open {path}: {exc}") from exc
            _open[path] = shared
        shared.connections += 1
        try:
            session = shared.db.session()
        except BaseException:
            _release(path, shared)
            raise
    return Connection(path, shared, session, autocommit)


def _release(path: Path, shared: _Shared) -> None:
    """Drop one connection's claim on a shared database (caller holds ``_open_lock``)."""
    shared.connections -= 1
    if shared.connections == 0:
        del _open[path]
        shared.db.close()


# --------------------------------------------------------------------- connections


_WRITES = (ast.Insert, ast.Update, ast.Delete)


class Connection:
    def __init__(self, path: Path, shared: _Shared, session: Session, autocommit: bool) -> None:
        self._path = path
        self._shared = shared
        self._session: Session | None = session
        self.autocommit = autocommit

    # PEP 249 makes the exceptions reachable from the connection, too.
    Warning = Warning
    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError

    @property
    def closed(self) -> bool:
        return self._session is None

    @property
    def in_transaction(self) -> bool:
        return self._session is not None and self._session.in_transaction

    def cursor(self) -> "Cursor":
        self._require_session()
        return Cursor(self)

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> "Cursor":
        """Shortcut: create a cursor, run ``sql`` on it and return it."""
        cursor = self.cursor()
        cursor.execute(sql, parameters)
        return cursor

    def commit(self) -> None:
        session = self._require_session()
        if session.in_transaction:
            self._run(session, ast.Commit())

    def rollback(self) -> None:
        session = self._require_session()
        if session.in_transaction:
            self._run(session, ast.Rollback())

    def close(self) -> None:
        """Close the connection, rolling back uncommitted changes."""
        session, self._session = self._session, None
        if session is None:
            return
        with _open_lock:
            try:
                session.close()
            finally:
                _release(self._path, self._shared)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit if the block succeeded, roll back if it raised; the connection stays open."""
        if self._session is None:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    # ----------------------------------------------------------------- internals

    def _execute(self, sql: str, parameters: Sequence[object]) -> Result:
        session = self._require_session()
        if isinstance(parameters, Mapping | str | bytes):
            raise ProgrammingError("parameters must be a sequence such as a tuple or list")
        try:
            statement = parse(sql, list(parameters))
        except pydb_errors.PyDBError as exc:
            raise _translate(exc) from exc
        if not self.autocommit and isinstance(statement, _WRITES) and not session.in_transaction:
            self._run(session, ast.Begin())
        return self._run(session, statement)

    def _run(self, session: Session, statement: ast.Statement) -> Result:
        try:
            return session.execute_statement(statement)
        except pydb_errors.PyDBError as exc:
            raise _translate(exc) from exc

    def _require_session(self) -> Session:
        if self._session is None:
            raise InterfaceError("the connection is closed")
        return self._session


# --------------------------------------------------------------------- cursors

Description = tuple[str, str | None, None, None, None, None, None]


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.arraysize = 1
        self.description: tuple[Description, ...] | None = None
        self.rowcount = -1
        self.lastrowid: int | None = None
        self.messages: list[str] = []
        self._rows: list[Row] = []
        self._position = 0
        self._closed = False

    def execute(self, operation: str, parameters: Sequence[object] = ()) -> Self:
        self._require_open()
        result = self.connection._execute(operation, parameters)
        self._load(result)
        return self

    def executemany(self, operation: str, seq_of_parameters: Sequence[Sequence[object]]) -> Self:
        self._require_open()
        total = 0
        for parameters in seq_of_parameters:
            result = self.connection._execute(operation, parameters)
            if result.columns:
                raise ProgrammingError("executemany() cannot run a query that returns rows")
            total += max(result.rowcount, 0)
        self._load(Result(rowcount=total))
        return self

    def fetchone(self) -> Row | None:
        self._require_results()
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[Row]:
        self._require_results()
        count = self.arraysize if size is None else size
        rows = self._rows[self._position : self._position + count]
        self._position += len(rows)
        return rows

    def fetchall(self) -> list[Row]:
        self._require_results()
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def close(self) -> None:
        self._closed = True
        self._rows = []

    def setinputsizes(self, sizes: object) -> None:
        pass

    def setoutputsize(self, size: object, column: object = None) -> None:
        pass

    def __iter__(self) -> Iterator[Row]:
        return self

    def __next__(self) -> Row:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

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

    def _load(self, result: Result) -> None:
        self._rows = result.rows
        self._position = 0
        self.rowcount = result.rowcount
        if result.columns:
            self.description = tuple(
                (name, _type_name(result.rows, i), None, None, None, None, None)
                for i, name in enumerate(result.columns)
            )
        else:
            self.description = None
            self._rows = []

    def _require_open(self) -> None:
        if self._closed:
            raise InterfaceError("the cursor is closed")
        if self.connection.closed:
            raise InterfaceError("the connection is closed")

    def _require_results(self) -> None:
        self._require_open()
        if self.description is None:
            raise ProgrammingError("the last statement did not return rows")


def _type_name(rows: list[Row], position: int) -> str | None:
    """The column's type, judged from its first non-NULL value (None if all are NULL)."""
    for row in rows:
        value = row[position]
        if value is None:
            continue
        if isinstance(value, bool):
            return "BOOL"
        if isinstance(value, int):
            return "INT"
        if isinstance(value, float):
            return "FLOAT"
        return "TEXT"
    return None
