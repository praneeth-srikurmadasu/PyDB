"""Crash recovery (ARIES-lite): analysis, redo, undo.

1. **Analysis** reads the last checkpoint's table of active transactions, then scans the log
   forward from it, tracking each transaction's newest record and whether it committed or
   ended. Transactions that never committed are the *losers*.
2. **Redo** repeats history, in the same forward scan: every PAGE_DIFF record is applied to
   its page if the page's ``page_lsn`` is older than the record. Redo restores the state at
   the moment of the crash, including the losers' changes. A page that cannot be read (torn
   by the crash) is rebuilt from its full image, which is always the first record for that
   page after the checkpoint.
3. **Undo** rolls back each loser, newest first, with ``TransactionManager.undo``, which
   writes a CLR per step. If recovery itself crashes, the next recovery redoes the CLRs'
   page changes and resumes undo where the CLRs point, so no step is undone twice.

Checkpoints flush every dirty page before they are written (see
``TransactionManager.checkpoint``), so redo can start at the checkpoint.
"""

from dataclasses import dataclass, field

from pydb.errors import CorruptPageError
from pydb.storage import page
from pydb.txn.transaction_manager import TransactionManager
from pydb.txn.wal import NO_LSN, LogRecord, RecordType


@dataclass(slots=True)
class _Entry:
    last_lsn: int
    committed: bool = False
    physical: bool = False


@dataclass(slots=True)
class RecoveryReport:
    redone: int = 0
    winners: list[int] = field(default_factory=list)
    losers: list[int] = field(default_factory=list)


def recover(tm: TransactionManager) -> RecoveryReport:
    """Bring the database back to a consistent state using its log."""
    log = tm.log
    report = RecoveryReport()
    table: dict[int, _Entry] = {}
    max_txn = 0
    start: int | None = None
    if log.checkpoint_lsn != NO_LSN:
        checkpoint = log.read(log.checkpoint_lsn).checkpoint()
        for saved in checkpoint.transactions:
            table[saved.txn_id] = _Entry(saved.last_lsn, saved.committed, saved.physical)
            max_txn = max(max_txn, saved.txn_id)
        start = log.checkpoint_lsn

    for record in log.scan(start):  # analysis and redo in one forward pass
        if record.txn_id:
            max_txn = max(max_txn, record.txn_id)
            entry = table.setdefault(record.txn_id, _Entry(record.lsn))
            entry.last_lsn = record.lsn
            if record.type is RecordType.BEGIN:
                entry.physical = record.physical_flag()
            elif record.type is RecordType.COMMIT:
                entry.committed = True
            elif record.type is RecordType.END:
                del table[record.txn_id]
        if record.type is RecordType.PAGE_DIFF:
            report.redone += _redo(tm, record)

    tm.next_txn_id = max(tm.next_txn_id, max_txn + 1)
    for txn_id, state in sorted(table.items(), key=lambda item: -item[1].last_lsn):
        if state.committed:
            tm.write_end(txn_id, state.last_lsn)
            report.winners.append(txn_id)
        else:
            txn = tm.adopt(txn_id, state.last_lsn, state.physical)
            tm.abort(txn)
            report.losers.append(txn_id)
    log.flush()
    return report


def _redo(tm: TransactionManager, record: LogRecord) -> int:
    """Apply one PAGE_DIFF if its page has not seen it yet. Return 1 if applied."""
    diff = record.page_diff()
    pool = tm.pool
    try:
        frame = pool.fetch_page(diff.page_id)
    except CorruptPageError:
        if not diff.is_full_page:
            raise
        frame = pool.install_page(diff.page_id, diff.ranges[0].after)
        _stamp(frame.data, record.lsn)
        pool.unpin_page(diff.page_id, dirty=True)
        return 1
    applied = False
    try:
        if page.read_header(frame.data).page_lsn < record.lsn:
            for change in diff.ranges:
                frame.data[change.offset : change.offset + len(change.after)] = change.after
            _stamp(frame.data, record.lsn)
            applied = True
    finally:
        pool.unpin_page(diff.page_id, dirty=applied)
    return int(applied)


def _stamp(data: bytearray, lsn: int) -> None:
    header = page.read_header(data)
    page.write_header(data, page.PageHeader(header.page_id, header.page_type, lsn))
