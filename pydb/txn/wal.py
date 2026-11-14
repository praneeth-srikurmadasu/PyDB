"""The write-ahead log: record formats and the log manager.

File layout (``<database>.wal``)::

    header   magic "PYDBWAL1" (8) | base_lsn (u64) | checkpoint_lsn (u64)
    records  [lsn:8][prev_lsn:8][txn_id:8][type:1][length:4][payload][crc32:4] ...

A record's LSN is ``base_lsn`` plus its offset in the file, so LSNs grow monotonically and
stay unique after the log is truncated (truncation raises ``base_lsn``). ``prev_lsn`` links a
transaction's records backwards (0 = none). The CRC covers the record's header and payload;
the first record that is short or fails its CRC marks the end of the log (a torn write from
a crash), and anything after it is discarded when the log is opened.
"""

import os
import struct
import threading
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from pydb.errors import LogCorruptError

MAGIC = b"PYDBWAL1"
_FILE_HEADER = struct.Struct("<8sQQ")
FILE_HEADER_SIZE = _FILE_HEADER.size
_RECORD_HEADER = struct.Struct("<QQQBI")
_CRC = struct.Struct("<I")
NO_LSN = 0


class RecordType(IntEnum):
    BEGIN = 1  # payload: physical-undo flag
    COMMIT = 2
    ABORT = 3
    END = 4
    INSERT = 5  # logical: a row and its index entries were added
    DELETE = 6  # logical: a row and its index entries were removed (slot reserved)
    PAGE_DIFF = 7  # physical redo: changed byte ranges of one page
    CLR = 8  # compensation: undo_next_lsn and kind
    CHECKPOINT = 9  # active transactions and dirty pages


class ClrKind(IntEnum):
    PHYSICAL = 0  # compensated a PAGE_DIFF
    LOGICAL = 1  # compensated an INSERT or DELETE


# --------------------------------------------------------------------- payloads


@dataclass(frozen=True, slots=True)
class ByteRange:
    offset: int
    before: bytes
    after: bytes


@dataclass(frozen=True, slots=True)
class PageDiff:
    page_id: int
    ranges: tuple[ByteRange, ...]

    @property
    def is_full_page(self) -> bool:
        return (
            len(self.ranges) == 1 and self.ranges[0].offset == 0 and len(self.ranges[0].after) > 64
        )


@dataclass(frozen=True, slots=True)
class RowChange:
    table_id: int
    page_id: int
    slot_id: int
    row: bytes


@dataclass(frozen=True, slots=True)
class Compensation:
    undo_next_lsn: int
    kind: ClrKind


@dataclass(frozen=True, slots=True)
class TxnEntry:
    txn_id: int
    last_lsn: int
    committed: bool
    physical: bool


@dataclass(frozen=True, slots=True)
class Checkpoint:
    transactions: tuple[TxnEntry, ...]
    dirty_pages: tuple[tuple[int, int], ...]  # (page_id, rec_lsn)


@dataclass(frozen=True, slots=True)
class LogRecord:
    lsn: int
    prev_lsn: int
    txn_id: int
    type: RecordType
    payload: bytes = field(repr=False)

    @property
    def size(self) -> int:
        return _RECORD_HEADER.size + len(self.payload) + _CRC.size

    def page_diff(self) -> PageDiff:
        page_id, count = struct.unpack_from("<iH", self.payload, 0)
        pos, ranges = 6, []
        for _ in range(count):
            offset, length = struct.unpack_from("<HH", self.payload, pos)
            pos += 4
            before = self.payload[pos : pos + length]
            after = self.payload[pos + length : pos + 2 * length]
            pos += 2 * length
            ranges.append(ByteRange(offset, before, after))
        return PageDiff(page_id, tuple(ranges))

    def row_change(self) -> RowChange:
        table_id, page_id, slot_id, length = struct.unpack_from("<iiHI", self.payload, 0)
        return RowChange(table_id, page_id, slot_id, self.payload[14 : 14 + length])

    def compensation(self) -> Compensation:
        lsn, kind = struct.unpack_from("<QB", self.payload, 0)
        return Compensation(lsn, ClrKind(kind))

    def physical_flag(self) -> bool:
        return bool(self.payload and self.payload[0])

    def checkpoint(self) -> Checkpoint:
        count = struct.unpack_from("<I", self.payload, 0)[0]
        pos, txns = 4, []
        for _ in range(count):
            txn_id, last_lsn, committed, physical = struct.unpack_from("<QQBB", self.payload, pos)
            txns.append(TxnEntry(txn_id, last_lsn, bool(committed), bool(physical)))
            pos += 18
        pages = struct.unpack_from("<I", self.payload, pos)[0]
        pos += 4
        dirty = [struct.unpack_from("<iQ", self.payload, pos + 12 * i) for i in range(pages)]
        return Checkpoint(tuple(txns), tuple((int(pid), int(lsn)) for pid, lsn in dirty))


def encode_page_diff(diff: PageDiff) -> bytes:
    parts = [struct.pack("<iH", diff.page_id, len(diff.ranges))]
    for r in diff.ranges:
        parts += [struct.pack("<HH", r.offset, len(r.after)), r.before, r.after]
    return b"".join(parts)


def encode_row_change(change: RowChange) -> bytes:
    header = struct.pack("<iiHI", change.table_id, change.page_id, change.slot_id, len(change.row))
    return header + change.row


def encode_compensation(clr: Compensation) -> bytes:
    return struct.pack("<QB", clr.undo_next_lsn, clr.kind)


def encode_checkpoint(checkpoint: Checkpoint) -> bytes:
    parts = [struct.pack("<I", len(checkpoint.transactions))]
    parts += [
        struct.pack("<QQBB", t.txn_id, t.last_lsn, t.committed, t.physical)
        for t in checkpoint.transactions
    ]
    parts.append(struct.pack("<I", len(checkpoint.dirty_pages)))
    parts += [struct.pack("<iQ", page, lsn) for page, lsn in checkpoint.dirty_pages]
    return b"".join(parts)


# --------------------------------------------------------------------- the log manager


class LogManager:
    """Appends records to an in-memory tail and writes them out on ``flush``.

    ``flush(lsn)`` makes every record up to and including ``lsn`` durable (written and
    fsynced). Thread-safe.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        is_new = not self.path.exists() or self.path.stat().st_size < FILE_HEADER_SIZE
        self._file = open(self.path, "w+b" if is_new else "r+b", buffering=0)  # noqa: SIM115
        try:
            if is_new:
                self.base_lsn = FILE_HEADER_SIZE
                self.checkpoint_lsn = NO_LSN
                self._write_header()
                end = FILE_HEADER_SIZE
            else:
                self._file.seek(0)
                magic, self.base_lsn, self.checkpoint_lsn = _FILE_HEADER.unpack(
                    self._file.read(FILE_HEADER_SIZE)
                )
                if magic != MAGIC:
                    raise LogCorruptError(f"{self.path} is not a PyDB log")
                end = self._valid_end()
                self._file.truncate(end)  # drop a torn tail left by a crash
        except BaseException:
            self._file.close()
            raise
        self._tail: list[bytes] = []
        self._next_lsn = self._lsn_at(end)
        self.flushed_lsn = self._next_lsn  # everything before this LSN is durable
        self.records_written = 0
        self.flush_count = 0

    # ----------------------------------------------------------------- appending

    @property
    def next_lsn(self) -> int:
        with self._lock:
            return self._next_lsn

    def append(self, txn_id: int, prev_lsn: int, type_: RecordType, payload: bytes = b"") -> int:
        with self._lock:
            lsn = self._next_lsn
            head = _RECORD_HEADER.pack(lsn, prev_lsn, txn_id, type_, len(payload))
            body = head + payload
            self._tail.append(body + _CRC.pack(zlib.crc32(body)))
            self._next_lsn += len(body) + _CRC.size
            self.records_written += 1
            return lsn

    def flush(self, up_to_lsn: int | None = None) -> None:
        """Make the log durable at least up to ``up_to_lsn`` (everything, if None)."""
        with self._lock:
            target = self._next_lsn if up_to_lsn is None else up_to_lsn
            if target < self.flushed_lsn or not self._tail:
                return
            self._file.seek(self._offset_of(self.flushed_lsn))
            self._file.write(b"".join(self._tail))
            os.fsync(self._file.fileno())
            self._tail.clear()
            self.flushed_lsn = self._next_lsn
            self.flush_count += 1

    # ----------------------------------------------------------------- reading

    def read(self, lsn: int) -> LogRecord:
        """Read one record. Records not yet flushed are flushed first."""
        with self._lock:
            if lsn >= self.flushed_lsn:
                self.flush()
            record = self._read_at(self._offset_of(lsn))
            if record is None or record.lsn != lsn:
                raise LogCorruptError(f"no log record at LSN {lsn}")
            return record

    def scan(self, from_lsn: int | None = None) -> Iterator[LogRecord]:
        """Every durable record from ``from_lsn`` (the start of the log if None) onwards."""
        self.flush()
        offset = FILE_HEADER_SIZE if from_lsn is None else self._offset_of(from_lsn)
        while True:
            with self._lock:
                record = self._read_at(offset)
            if record is None:
                return
            yield record
            offset += record.size

    # ----------------------------------------------------------------- checkpoints

    def set_checkpoint(self, lsn: int) -> None:
        """Record where the latest checkpoint is (after it has been flushed)."""
        with self._lock:
            self.flush(lsn)
            self.checkpoint_lsn = lsn
            self._write_header()

    def truncate(self) -> None:
        """Discard every record. Only safe when the data file is fully up to date."""
        with self._lock:
            self.flush()
            self.base_lsn = self._next_lsn
            self.checkpoint_lsn = NO_LSN
            self._file.truncate(FILE_HEADER_SIZE)
            self._write_header()
            self._next_lsn = self.flushed_lsn = self._lsn_at(FILE_HEADER_SIZE)

    def close(self) -> None:
        with self._lock:
            if self._file.closed:
                return
            self.flush()
            self._file.close()

    def abandon(self) -> None:
        """Close without writing the unflushed tail, as a crash would."""
        with self._lock:
            self._tail.clear()
            self._file.close()

    # ----------------------------------------------------------------- internals

    def _offset_of(self, lsn: int) -> int:
        return lsn - self.base_lsn + FILE_HEADER_SIZE

    def _lsn_at(self, offset: int) -> int:
        return self.base_lsn + offset - FILE_HEADER_SIZE

    def _write_header(self) -> None:
        self._file.seek(0)
        self._file.write(_FILE_HEADER.pack(MAGIC, self.base_lsn, self.checkpoint_lsn))
        os.fsync(self._file.fileno())

    def _valid_end(self) -> int:
        offset = FILE_HEADER_SIZE
        while (record := self._read_at(offset)) is not None:
            offset += record.size
        return offset

    def _read_at(self, offset: int) -> LogRecord | None:
        self._file.seek(offset)
        head = self._file.read(_RECORD_HEADER.size)
        if head is None or len(head) < _RECORD_HEADER.size:
            return None
        lsn, prev_lsn, txn_id, type_, length = _RECORD_HEADER.unpack(head)
        rest = self._file.read(length + _CRC.size)
        if rest is None or len(rest) < length + _CRC.size:
            return None
        payload, crc = rest[:length], _CRC.unpack(rest[length:])[0]
        if zlib.crc32(head + payload) != crc or lsn != self._lsn_at(offset):
            return None
        try:
            kind = RecordType(type_)
        except ValueError:
            return None
        return LogRecord(lsn, prev_lsn, txn_id, kind, payload)
