from pathlib import Path

import pytest

from pydb.errors import LogCorruptError
from pydb.storage.buffer_pool import diff_pages
from pydb.txn.wal import (
    FILE_HEADER_SIZE,
    NO_LSN,
    ByteRange,
    Checkpoint,
    ClrKind,
    Compensation,
    LogManager,
    PageDiff,
    RecordType,
    RowChange,
    TxnEntry,
    encode_checkpoint,
    encode_compensation,
    encode_page_diff,
    encode_row_change,
)


def test_append_flush_read_and_scan(tmp_path: Path) -> None:
    log = LogManager(tmp_path / "a.wal")
    first = log.append(1, NO_LSN, RecordType.BEGIN, b"\x00")
    second = log.append(1, first, RecordType.COMMIT)
    assert first == FILE_HEADER_SIZE and second > first
    assert log.flushed_lsn == first  # nothing durable yet
    log.flush(second)
    assert log.flushed_lsn > second
    record = log.read(second)
    assert (record.lsn, record.prev_lsn, record.txn_id, record.type) == (
        second,
        first,
        1,
        RecordType.COMMIT,
    )
    assert [r.type for r in log.scan()] == [RecordType.BEGIN, RecordType.COMMIT]
    next_lsn = log.next_lsn
    log.close()
    reopened = LogManager(tmp_path / "a.wal")
    assert reopened.next_lsn == next_lsn
    assert [r.lsn for r in reopened.scan()] == [first, second]
    reopened.close()


def test_unflushed_records_are_lost_on_crash(tmp_path: Path) -> None:
    log = LogManager(tmp_path / "b.wal")
    durable = log.append(1, NO_LSN, RecordType.BEGIN)
    log.flush()
    log.append(1, durable, RecordType.COMMIT)
    log.abandon()
    reopened = LogManager(tmp_path / "b.wal")
    assert [r.type for r in reopened.scan()] == [RecordType.BEGIN]
    reopened.close()


def test_torn_tail_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "c.wal"
    log = LogManager(path)
    a = log.append(1, NO_LSN, RecordType.BEGIN)
    b = log.append(1, a, RecordType.INSERT, b"x" * 50)
    log.close()
    path.write_bytes(path.read_bytes()[:-10])  # the last record was only partly written
    reopened = LogManager(path)
    assert [r.lsn for r in reopened.scan()] == [a]
    assert reopened.next_lsn == b  # the next record takes the torn one's place
    c = reopened.append(2, NO_LSN, RecordType.BEGIN)
    reopened.flush()
    assert [r.lsn for r in reopened.scan()] == [a, c]
    reopened.close()


def test_corrupt_record_ends_the_log(tmp_path: Path) -> None:
    path = tmp_path / "d.wal"
    log = LogManager(path)
    a = log.append(1, NO_LSN, RecordType.BEGIN)
    log.append(1, a, RecordType.INSERT, b"payload")
    log.close()
    data = bytearray(path.read_bytes())
    data[-6] ^= 0xFF  # damage the second record's payload: its CRC no longer matches
    path.write_bytes(bytes(data))
    reopened = LogManager(path)
    assert [r.lsn for r in reopened.scan()] == [a]
    reopened.close()


def test_not_a_log(tmp_path: Path) -> None:
    path = tmp_path / "e.wal"
    path.write_bytes(b"x" * 64)
    with pytest.raises(LogCorruptError):
        LogManager(path)


def test_truncate_keeps_lsns_increasing(tmp_path: Path) -> None:
    path = tmp_path / "f.wal"
    log = LogManager(path)
    for _ in range(5):
        log.append(1, NO_LSN, RecordType.BEGIN)
    before = log.next_lsn
    log.truncate()
    assert list(log.scan()) == []
    assert path.stat().st_size == FILE_HEADER_SIZE
    after = log.append(2, NO_LSN, RecordType.BEGIN)
    assert after >= before
    log.close()
    reopened = LogManager(path)
    assert [r.lsn for r in reopened.scan()] == [after]
    reopened.close()


def test_checkpoint_pointer_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "g.wal"
    log = LogManager(path)
    entries = (TxnEntry(7, 123, False, True),)
    lsn = log.append(
        0, NO_LSN, RecordType.CHECKPOINT, encode_checkpoint(Checkpoint(entries, ((5, 99),)))
    )
    log.set_checkpoint(lsn)
    log.close()
    reopened = LogManager(path)
    assert reopened.checkpoint_lsn == lsn
    checkpoint = reopened.read(lsn).checkpoint()
    assert checkpoint.transactions == entries
    assert checkpoint.dirty_pages == ((5, 99),)
    reopened.close()


def test_payload_codecs_round_trip(tmp_path: Path) -> None:
    log = LogManager(tmp_path / "h.wal")
    diff = PageDiff(3, (ByteRange(0, b"ab", b"cd"), ByteRange(100, b"\x00" * 32, b"\x01" * 32)))
    change = RowChange(9, 4, 17, b"row-bytes")
    clr = Compensation(555, ClrKind.LOGICAL)
    lsns = [
        log.append(1, NO_LSN, RecordType.PAGE_DIFF, encode_page_diff(diff)),
        log.append(1, NO_LSN, RecordType.INSERT, encode_row_change(change)),
        log.append(1, NO_LSN, RecordType.CLR, encode_compensation(clr)),
    ]
    assert log.read(lsns[0]).page_diff() == diff
    assert log.read(lsns[1]).row_change() == change
    assert log.read(lsns[2]).compensation() == clr
    log.close()


def test_diff_pages() -> None:
    before = bytes(4096)
    after = bytearray(before)
    after[5] = 1
    after[40:70] = b"x" * 30
    after[4095] = 9
    ranges = diff_pages(before, after)
    assert [(offset, len(new)) for offset, _, new in ranges] == [(0, 96), (4064, 32)]
    rebuilt = bytearray(before)
    for offset, _, new in ranges:
        rebuilt[offset : offset + len(new)] = new
    assert rebuilt == after
    assert diff_pages(before, bytearray(before)) == []
