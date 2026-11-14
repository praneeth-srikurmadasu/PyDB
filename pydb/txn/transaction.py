"""A transaction's in-memory state."""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from pydb.storage.table_heap import RID
from pydb.txn.wal import NO_LSN

if TYPE_CHECKING:
    from pydb.catalog.schema import TableInfo


class TxnState(Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"


@dataclass(eq=False)
class Transaction:
    """One unit of work: ACTIVE until it commits or aborts.

    ``physical`` transactions (DDL, ANALYZE, catalog creation) are undone by restoring page
    before-images; others are undone logically, row by row. ``last_lsn`` is the newest log
    record the transaction wrote; nothing is logged until its first change, so read-only
    transactions never touch the log.
    """

    txn_id: int
    physical: bool = False
    state: TxnState = TxnState.ACTIVE
    last_lsn: int = NO_LSN
    began: bool = False  # whether its BEGIN record has been written
    pending_purges: list[tuple["TableInfo", RID]] = field(default_factory=list)
    """Rows deleted by this transaction, whose reserved space is freed when it commits."""
    deferred_frees: list[int] = field(default_factory=list)
    """Pages freed by this (physical) transaction, released to the free list on commit."""
    operation_frees: list[int] = field(default_factory=list)
    """Pages freed during the row operation in progress (a B+ tree merge), released once
    the operation's log record is written."""

    @property
    def active(self) -> bool:
        return self.state is TxnState.ACTIVE
