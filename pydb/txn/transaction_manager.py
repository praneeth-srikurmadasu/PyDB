"""Transactions over the write-ahead log: begin, commit, abort, savepoints and checkpoints.

Logging (see ``wal.py`` for the record formats):

* **PAGE_DIFF**: every change to a page made through the buffer pool is logged as its
  changed byte ranges, before and after. These records drive redo, and the page's
  ``page_lsn`` is set to the record's LSN.
* **INSERT / DELETE**: written *after* a row operation (the heap row and all its index
  entries) has completed. These records drive undo, logically: undoing an INSERT deletes
  the row and its index entries; undoing a DELETE restores the reserved row and re-adds its
  index entries.
* **CLR**: written after each compensation, pointing at the next record to undo, so that
  undo resumes correctly after a crash during undo.

**Undo.** Walking a transaction's records backwards, PAGE_DIFF records newer than its last
completed row operation belong to an operation interrupted half-way; they are undone
physically, by writing back their before-images. (Operations run one at a time, so such
records are the newest in the log and no other transaction has touched those bytes since.)
Older PAGE_DIFF records are covered by the logical record of their operation and skipped. A
*physical* transaction (DDL) is undone entirely by before-images; DDL never runs alongside
other transactions.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from pydb.config import CHECKPOINT_INTERVAL_RECORDS
from pydb.errors import TransactionError
from pydb.storage.buffer_pool import BufferPoolManager, ByteRanges
from pydb.storage.table_heap import RID
from pydb.txn.transaction import Transaction, TxnState
from pydb.txn.wal import (
    NO_LSN,
    ByteRange,
    Checkpoint,
    ClrKind,
    Compensation,
    LogManager,
    LogRecord,
    PageDiff,
    RecordType,
    RowChange,
    TxnEntry,
    encode_checkpoint,
    encode_compensation,
    encode_page_diff,
    encode_row_change,
)

if TYPE_CHECKING:
    from pydb.catalog.catalog import Catalog
    from pydb.catalog.schema import TableInfo


class TransactionManager:
    def __init__(self, log: LogManager, pool: BufferPoolManager) -> None:
        self.log = log
        self.pool = pool
        pool.logger = self
        self.next_txn_id = 1
        self.catalog_provider: Callable[[], Catalog] | None = None
        self._active: dict[int, Transaction] = {}
        self._local = threading.local()
        self._lock = threading.RLock()
        self._records_at_checkpoint = 0

    # ----------------------------------------------------------------- lifecycle

    def begin(self, *, physical: bool = False) -> Transaction:
        with self._lock:
            txn = Transaction(self.next_txn_id, physical)
            self.next_txn_id += 1
            self._active[txn.txn_id] = txn
            return txn

    @property
    def current(self) -> Transaction | None:
        txn: Transaction | None = getattr(self._local, "txn", None)
        return txn

    @contextmanager
    def activate(self, txn: Transaction | None) -> Iterator[None]:
        """Make ``txn`` the transaction that page changes on this thread belong to."""
        previous = self.current
        self._local.txn = txn
        try:
            yield
        finally:
            self._local.txn = previous

    def commit(self, txn: Transaction) -> None:
        self._require_active(txn)
        with self.activate(txn):
            for table, rid in txn.pending_purges:
                table.heap.purge(rid)
            txn.pending_purges.clear()
            if txn.began:
                lsn = self._append(txn, RecordType.COMMIT)
                self.log.flush(lsn)  # durable before COMMIT returns
                self._append(txn, RecordType.END)
        txn.state = TxnState.COMMITTED
        self._finish(txn)
        for page_id in txn.deferred_frees:
            self.pool.delete_page(page_id, deferred=True)
        txn.deferred_frees.clear()
        self.maybe_checkpoint()

    def abort(self, txn: Transaction) -> None:
        self._require_active(txn)
        with self.activate(txn):
            if txn.began:
                self._append(txn, RecordType.ABORT)
                self.undo(txn, NO_LSN)
                self._append(txn, RecordType.END)
        txn.state = TxnState.ABORTED
        txn.pending_purges.clear()
        txn.deferred_frees.clear()  # the pages stay allocated; their contents were restored
        self._finish(txn)

    def rollback_to(self, txn: Transaction, savepoint: int) -> None:
        """Undo everything ``txn`` did after it last wrote ``savepoint``."""
        self._require_active(txn)
        with self.activate(txn):
            self.undo(txn, savepoint)

    def adopt(self, txn_id: int, last_lsn: int, physical: bool) -> Transaction:
        """Re-create a transaction found unfinished in the log during recovery."""
        txn = Transaction(txn_id, physical, last_lsn=last_lsn, began=True)
        with self._lock:
            self._active[txn_id] = txn
        return txn

    def write_end(self, txn_id: int, last_lsn: int) -> None:
        """Finish a transaction recovery found committed but without its END record."""
        self.log.append(txn_id, last_lsn, RecordType.END)

    # ----------------------------------------------------------------- PageLogger protocol

    def log_page_change(self, page_id: int, ranges: ByteRanges) -> int:
        txn = self.current
        if txn is None:
            raise TransactionError(f"page {page_id} changed outside a transaction")
        diff = PageDiff(page_id, tuple(ByteRange(o, b, a) for o, b, a in ranges))
        return self._append(txn, RecordType.PAGE_DIFF, encode_page_diff(diff))

    def current_lsn(self) -> int:
        """An LSN between every record already written and every record still to come.

        Pages are stamped with it when allocated or freed, so redo skips records about the
        page's previous life but applies the very next record, even if that record gets
        ``log.next_lsn`` itself.
        """
        return self.log.next_lsn - 1

    def flush_log(self) -> None:
        self.log.flush()

    def defer_free(self, page_id: int) -> bool:
        """Delay freeing a page until it can no longer be needed by an undo.

        A physical transaction may restore any page it changed, so its frees wait for its
        commit. A row operation (such as a B+ tree merge inside a delete) that is interrupted
        is undone physically, which brings back the parent's pointer to the freed node, so
        the free waits until the operation's log record is written.
        """
        txn = self.current
        if txn is None:
            return False
        if txn.physical:
            txn.deferred_frees.append(page_id)
        else:
            txn.operation_frees.append(page_id)
        return True

    # ----------------------------------------------------------------- row journal

    def log_insert(self, table: "TableInfo", rid: RID, row: bytes) -> None:
        self._log_row(RecordType.INSERT, table, rid, row)

    def log_delete(self, table: "TableInfo", rid: RID, row: bytes) -> None:
        self._log_row(RecordType.DELETE, table, rid, row)
        txn = self._require_current()
        txn.pending_purges.append((table, rid))

    def _log_row(self, type_: RecordType, table: "TableInfo", rid: RID, row: bytes) -> None:
        txn = self._require_current()
        change = RowChange(table.table_id, rid.page_id, rid.slot_id, row)
        self._append(txn, type_, encode_row_change(change))
        self._release_operation_frees(txn)

    def _release_operation_frees(self, txn: Transaction) -> None:
        """The operation is complete and logged: its freed pages can go to the free list."""
        while txn.operation_frees:
            self.pool.delete_page(txn.operation_frees.pop(0), deferred=True)

    # ----------------------------------------------------------------- undo

    def undo(self, txn: Transaction, stop_lsn: int) -> None:
        """Undo ``txn``'s records newer than ``stop_lsn`` (all of them if NO_LSN)."""
        lsn = txn.last_lsn
        txn.operation_frees.clear()  # the interrupted operation is undone, its nodes kept
        completed_seen = False  # passed the transaction's last completed row operation?
        while lsn != NO_LSN and lsn > stop_lsn:
            record = self.log.read(lsn)
            kind = record.type
            if kind is RecordType.CLR:
                clr = record.compensation()
                completed_seen = completed_seen or clr.kind is ClrKind.LOGICAL
                lsn = clr.undo_next_lsn
            elif kind is RecordType.PAGE_DIFF:
                if txn.physical or not completed_seen:
                    self._undo_page(record)
                    self._compensate(txn, record, ClrKind.PHYSICAL)
                lsn = record.prev_lsn
            elif kind in (RecordType.INSERT, RecordType.DELETE):
                completed_seen = True
                self._undo_row(txn, record)
                self._compensate(txn, record, ClrKind.LOGICAL)
                lsn = record.prev_lsn
            elif kind is RecordType.BEGIN:
                break
            else:
                lsn = record.prev_lsn

    def _undo_page(self, record: LogRecord) -> None:
        diff = record.page_diff()
        with self.pool.page(diff.page_id, write=True) as frame:
            for change in reversed(diff.ranges):
                end = change.offset + len(change.before)
                frame.data[change.offset : end] = change.before

    def _undo_row(self, txn: Transaction, record: LogRecord) -> None:
        if self.catalog_provider is None:
            raise TransactionError("logical undo needs the catalog")
        change = record.row_change()
        catalog = self.catalog_provider()
        table = catalog.table_by_id(change.table_id)
        row = table.layout.deserialize(change.row)
        rid = RID(change.page_id, change.slot_id)
        if record.type is RecordType.INSERT:
            for index in table.indexes:
                index.tree.delete(index.key_of(row), rid)
            table.heap.delete(rid)
        else:
            table.heap.restore(rid)
            for index in table.indexes:
                index.tree.insert(index.key_of(row), rid)
            txn.pending_purges = [
                (t, r)
                for t, r in txn.pending_purges
                if not (t.table_id == table.table_id and r == rid)
            ]

    def _compensate(self, txn: Transaction, record: LogRecord, kind: ClrKind) -> None:
        payload = encode_compensation(Compensation(record.prev_lsn, kind))
        self._append(txn, RecordType.CLR, payload)
        self._release_operation_frees(txn)

    # ----------------------------------------------------------------- checkpoints

    def checkpoint(self) -> int:
        """Flush every dirty page, then log the active transactions.

        Because all pages are on disk when the record is written, redo can start at the
        checkpoint, and every page changed afterwards gets a full image in the log.
        """
        with self._lock:
            self.pool.flush_all()
            self.pool.disk.sync()
            self.pool.start_checkpoint()
            entries = tuple(
                TxnEntry(t.txn_id, t.last_lsn, False, t.physical)
                for t in self._active.values()
                if t.began
            )
            lsn = self.log.append(
                0, NO_LSN, RecordType.CHECKPOINT, encode_checkpoint(Checkpoint(entries, ()))
            )
            self.log.set_checkpoint(lsn)
            self._records_at_checkpoint = self.log.records_written
            return lsn

    def maybe_checkpoint(self) -> None:
        if self.log.records_written - self._records_at_checkpoint >= CHECKPOINT_INTERVAL_RECORDS:
            self.checkpoint()

    @property
    def active_transactions(self) -> list[Transaction]:
        with self._lock:
            return list(self._active.values())

    # ----------------------------------------------------------------- internals

    def _append(self, txn: Transaction, type_: RecordType, payload: bytes = b"") -> int:
        with self._lock:
            if not txn.began:
                txn.began = True
                flag = b"\x01" if txn.physical else b"\x00"
                txn.last_lsn = self.log.append(txn.txn_id, NO_LSN, RecordType.BEGIN, flag)
            txn.last_lsn = self.log.append(txn.txn_id, txn.last_lsn, type_, payload)
            return txn.last_lsn

    def _finish(self, txn: Transaction) -> None:
        with self._lock:
            self._active.pop(txn.txn_id, None)

    def _require_active(self, txn: Transaction) -> None:
        if not txn.active:
            raise TransactionError(f"transaction {txn.txn_id} is {txn.state.value}")

    def _require_current(self) -> Transaction:
        txn = self.current
        if txn is None:
            raise TransactionError("row change outside a transaction")
        return txn
