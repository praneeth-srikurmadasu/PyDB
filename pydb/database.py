"""An open database file: the storage stack, the catalog and the executor, wired together."""

import os
from types import TracebackType
from typing import Self

from pydb.catalog.catalog import Catalog
from pydb.config import BUFFER_POOL_SIZE
from pydb.execution.executor import Executor, Result
from pydb.planner.physical_plan import PlannerSettings
from pydb.sql.parser import parse, parse_script
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager


class Database:
    """``Database(path)`` opens or creates a database file.

    Changes live in the buffer pool and reach the file when pages are evicted and on
    ``close()``. Until the write-ahead log exists, a process that stops without closing the
    database can lose or corrupt recent changes.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        pool_size: int = BUFFER_POOL_SIZE,
        settings: PlannerSettings | None = None,
    ) -> None:
        self.disk = DiskManager(path)
        try:
            self.pool = BufferPoolManager(self.disk, pool_size)
            self.catalog = Catalog(self.pool)
            self.executor = Executor(self.catalog, settings)
        except BaseException:
            self.disk.close()
            raise

    def execute(self, sql: str) -> Result:
        """Run one SQL statement."""
        return self.executor.execute(parse(sql))

    def execute_script(self, sql: str) -> list[Result]:
        """Run statements separated by semicolons, stopping at the first error."""
        return [self.executor.execute(statement) for statement in parse_script(sql)]

    def close(self) -> None:
        if self.disk.closed:
            return
        try:
            self.pool.flush_all()
        finally:
            self.disk.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
