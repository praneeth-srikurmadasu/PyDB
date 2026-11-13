from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.config import INVALID_PAGE_ID
from pydb.errors import CorruptPageError, RecordNotFoundError, RowTooLargeError
from pydb.storage.disk_manager import DiskManager
from pydb.storage.slotted_page import MAX_TUPLE_SIZE, SlottedPage
from pydb.storage.table_heap import RID, TableHeap
from pydb.storage.tuple import Row, RowLayout
from pydb.types import DataType


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[DiskManager]:
    dm = DiskManager(tmp_path / "heap.db")
    yield dm
    dm.close()


def check_chain(heap: TableHeap) -> list[int]:
    """Assert next/prev links agree and return the page ids in order."""
    ids = list(heap.page_ids())
    prev = INVALID_PAGE_ID
    for page_id in ids:
        sp = SlottedPage(heap.disk.read_page(page_id))
        assert sp.prev_page_id == prev
        prev = page_id
    return ids


def test_insert_get_on_one_page(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    a = heap.insert(b"first")
    b = heap.insert(b"second")
    assert a == RID(heap.first_page_id, 0)
    assert b == RID(heap.first_page_id, 1)
    assert heap.get(a) == b"first"
    assert heap.get(b) == b"second"


def test_rows_spill_onto_new_linked_pages(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    rids = [heap.insert(bytes([i % 256]) * 500) for i in range(40)]
    pages = check_chain(heap)
    assert len(pages) > 1
    assert {rid.page_id for rid in rids} == set(pages)
    for i, rid in enumerate(rids):
        assert heap.get(rid) == bytes([i % 256]) * 500


def test_scan_returns_every_live_row_in_chain_order(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    rids = [heap.insert(f"row-{i}".encode() * 50) for i in range(30)]
    heap.delete(rids[3])
    heap.delete(rids[17])
    scanned = list(heap.scan())
    expected = [(rid, f"row-{i}".encode() * 50) for i, rid in enumerate(rids) if i not in (3, 17)]
    assert scanned == expected


def test_update_in_place_keeps_the_rid(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    rid = heap.insert(b"short")
    assert heap.update(rid, b"a bit longer") == rid
    assert heap.get(rid) == b"a bit longer"


def test_update_that_does_not_fit_moves_the_row(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    rid = heap.insert(b"a" * 2000)
    heap.insert(b"b" * 2000)
    new_rid = heap.update(rid, b"c" * 3000)
    assert new_rid != rid
    assert heap.get(new_rid) == b"c" * 3000
    with pytest.raises(RecordNotFoundError):
        heap.get(rid)
    assert [data[:1] for _, data in heap.scan()] == [b"b", b"c"]


def test_delete_then_get_raises(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    rid = heap.insert(b"gone")
    heap.delete(rid)
    with pytest.raises(RecordNotFoundError):
        heap.get(rid)
    assert list(heap.scan()) == []


def test_too_large_row_is_rejected(disk: DiskManager) -> None:
    heap = TableHeap.create(disk)
    with pytest.raises(RowTooLargeError):
        heap.insert(b"x" * (MAX_TUPLE_SIZE + 1))
    assert heap.get(heap.insert(b"x" * MAX_TUPLE_SIZE)) == b"x" * MAX_TUPLE_SIZE


def test_opening_a_non_heap_page_fails(disk: DiskManager) -> None:
    page_id = disk.allocate_page()
    with pytest.raises(CorruptPageError):
        TableHeap(disk, page_id)


def test_two_heaps_share_a_file_independently(disk: DiskManager) -> None:
    left = TableHeap.create(disk)
    right = TableHeap.create(disk)
    for i in range(20):
        left.insert(b"L" * 400)
        right.insert(f"R{i}".encode() * 100)
    assert all(data == b"L" * 400 for _, data in left.scan())
    assert [data[:3] for _, data in right.scan()][:2] == [b"R0R", b"R1R"]
    assert set(check_chain(left)).isdisjoint(check_chain(right))


def test_typed_rows_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "typed.db"
    layout = RowLayout([DataType.INT, DataType.TEXT, DataType.FLOAT, DataType.BOOL])
    rows: list[Row] = [(i, f"name {i}", i / 3, i % 2 == 0) for i in range(500)]
    rows.append((None,) * 4)
    with DiskManager(path) as disk:
        heap = TableHeap.create(disk)
        rids = [heap.insert(layout.serialize(row)) for row in rows]
        heap.delete(rids[10])
        rids[20] = heap.update(rids[20], layout.serialize((20, "renamed", 0.5, None)))
        first = heap.first_page_id
    expected = {rid: row for rid, row in zip(rids, rows, strict=True)}
    del expected[rids[10]]
    expected[rids[20]] = (20, "renamed", 0.5, None)
    with DiskManager(path) as disk:
        heap = TableHeap(disk, first)
        assert {rid: layout.deserialize(data) for rid, data in heap.scan()} == expected
        check_chain(heap)


def test_inserts_after_reopen_reuse_free_space(tmp_path: Path) -> None:
    path = tmp_path / "reuse.db"
    with DiskManager(path) as disk:
        heap = TableHeap.create(disk)
        first = heap.first_page_id
        rids = [heap.insert(b"z" * 1000) for _ in range(3)]
        heap.delete(rids[0])
        pages = disk.num_pages
    with DiskManager(path) as disk:
        heap = TableHeap(disk, first)
        rid = heap.insert(b"y" * 1000)
        assert rid.page_id == first
        assert disk.num_pages == pages
