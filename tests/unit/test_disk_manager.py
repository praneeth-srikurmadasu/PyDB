from pathlib import Path

import pytest

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import CorruptPageError, StorageError
from pydb.storage import page
from pydb.storage.disk_manager import DiskManager
from pydb.storage.page import PageType


def filled_page(page_id: int, payload: bytes) -> bytearray:
    buf = page.new_page(page_id, PageType.EMPTY)
    buf[page.HEADER_SIZE : page.HEADER_SIZE + len(payload)] = payload
    return buf


def flip_byte_on_disk(path: Path, offset: int) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        value = f.read(1)[0]
        f.seek(offset)
        f.write(bytes([value ^ 0xFF]))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


# ------------------------------------------------------------------ creation and reopen


def test_new_file_holds_only_the_header_page(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        assert dm.num_pages == 1
        assert dm.free_page_count == 0
        assert dm.catalog_root == INVALID_PAGE_ID
    assert db_path.stat().st_size == PAGE_SIZE
    with open(db_path, "rb") as f:
        assert f.read(PAGE_SIZE)[24:32] == b"PYDB0001"


def test_allocate_hands_out_sequential_ids(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        assert [dm.allocate_page() for _ in range(3)] == [1, 2, 3]
        assert dm.num_pages == 4
    assert db_path.stat().st_size == 4 * PAGE_SIZE


def test_new_page_reads_back_as_empty(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        assert page.read_header(dm.read_page(pid)).page_type is PageType.EMPTY


def test_write_then_read(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        dm.write_page(pid, filled_page(pid, b"hello pages"))
        data = dm.read_page(pid)
    assert data[page.HEADER_SIZE : page.HEADER_SIZE + 11] == b"hello pages"


def test_pages_survive_reopen(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        ids = [dm.allocate_page() for _ in range(5)]
        for pid in ids:
            dm.write_page(pid, filled_page(pid, f"page-{pid}".encode()))
        dm.set_catalog_root(ids[2])
    with DiskManager(db_path) as dm:
        assert dm.num_pages == 6
        assert dm.catalog_root == ids[2]
        for pid in ids:
            payload = f"page-{pid}".encode()
            data = dm.read_page(pid)
            assert data[page.HEADER_SIZE : page.HEADER_SIZE + len(payload)] == payload


def test_page_lsn_is_preserved(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        buf = page.new_page(pid, PageType.EMPTY)
        page.write_header(buf, page.PageHeader(pid, PageType.EMPTY, 12345))
        dm.write_page(pid, buf)
    with DiskManager(db_path) as dm:
        assert page.read_header(dm.read_page(pid)).page_lsn == 12345


# ------------------------------------------------------------------ free list


def test_freed_pages_are_reused_lifo(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        a, b, c = (dm.allocate_page() for _ in range(3))
        dm.deallocate_page(a)
        dm.deallocate_page(c)
        assert dm.free_page_count == 2
        assert dm.allocate_page() == c
        assert dm.allocate_page() == a
        assert dm.free_page_count == 0
        assert dm.allocate_page() == 4
        assert b == 2


def test_reuse_does_not_grow_the_file(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        ids = [dm.allocate_page() for _ in range(10)]
        for pid in ids:
            dm.deallocate_page(pid)
        dm.sync()
        size = db_path.stat().st_size
        for _ in ids:
            dm.allocate_page()
        dm.sync()
        assert db_path.stat().st_size == size
        assert dm.num_pages == 11


def test_reused_page_comes_back_empty(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        dm.write_page(pid, filled_page(pid, b"old data"))
        dm.deallocate_page(pid)
        assert dm.allocate_page() == pid
        data = dm.read_page(pid)
        assert page.read_header(data).page_type is PageType.EMPTY
        assert data[page.HEADER_SIZE :] == bytes(PAGE_SIZE - page.HEADER_SIZE)


def test_free_list_survives_reopen(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        ids = [dm.allocate_page() for _ in range(4)]
        dm.deallocate_page(ids[1])
        dm.deallocate_page(ids[3])
    with DiskManager(db_path) as dm:
        assert dm.free_page_count == 2
        assert dm.allocate_page() == ids[3]
        assert dm.allocate_page() == ids[1]
        assert dm.allocate_page() == 5


def test_double_free_is_rejected(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        dm.deallocate_page(pid)
        with pytest.raises(StorageError, match="already free"):
            dm.deallocate_page(pid)
        assert dm.free_page_count == 1


# ------------------------------------------------------------------ corruption


def test_corrupted_data_page_is_detected(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        dm.write_page(pid, filled_page(pid, b"precious"))
    flip_byte_on_disk(db_path, pid * PAGE_SIZE + 500)
    with DiskManager(db_path) as dm, pytest.raises(CorruptPageError, match="checksum"):
        dm.read_page(pid)


def test_corrupted_header_page_fails_open(db_path: Path) -> None:
    DiskManager(db_path).close()
    flip_byte_on_disk(db_path, 30)
    with pytest.raises(CorruptPageError):
        DiskManager(db_path)


def test_page_copied_to_wrong_offset_is_detected(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        a = dm.allocate_page()
        b = dm.allocate_page()
        dm.write_page(a, filled_page(a, b"AAAA"))
    with open(db_path, "r+b") as f:
        f.seek(a * PAGE_SIZE)
        data = f.read(PAGE_SIZE)
        f.seek(b * PAGE_SIZE)
        f.write(data)
    with DiskManager(db_path) as dm, pytest.raises(CorruptPageError, match="misdirected"):
        dm.read_page(b)


def test_non_pydb_file_is_rejected(db_path: Path) -> None:
    db_path.write_bytes(b"x" * PAGE_SIZE)
    with pytest.raises(CorruptPageError):
        DiskManager(db_path)


def test_truncated_file_is_rejected(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        for _ in range(3):
            dm.allocate_page()
    with open(db_path, "r+b") as f:
        f.truncate(2 * PAGE_SIZE)
    with pytest.raises(CorruptPageError, match="header records 4 pages"):
        DiskManager(db_path)


def test_failed_open_releases_the_file(db_path: Path) -> None:
    db_path.write_bytes(b"x" * PAGE_SIZE)
    with pytest.raises(CorruptPageError):
        DiskManager(db_path)
    db_path.unlink()  # fails on Windows if the handle leaked
    assert not db_path.exists()


# ------------------------------------------------------------------ misuse


def test_header_page_cannot_be_written_or_freed(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        with pytest.raises(StorageError, match="header page"):
            dm.write_page(0, page.new_page(0, PageType.EMPTY))
        with pytest.raises(StorageError, match="header page"):
            dm.deallocate_page(0)


def test_unallocated_pages_are_rejected(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        with pytest.raises(StorageError, match="no such page"):
            dm.read_page(1)
        with pytest.raises(StorageError, match="no such page"):
            dm.write_page(1, page.new_page(1, PageType.EMPTY))
        with pytest.raises(StorageError, match="no such page"):
            dm.read_page(-1)


def test_write_rejects_wrong_size_and_wrong_id(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        other = dm.allocate_page()
        with pytest.raises(StorageError, match="bytes"):
            dm.write_page(pid, bytes(10))
        with pytest.raises(StorageError, match="header says"):
            dm.write_page(pid, page.new_page(other, PageType.EMPTY))


def test_write_rejects_reserved_page_types(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        pid = dm.allocate_page()
        for page_type in (PageType.HEADER, PageType.FREE):
            with pytest.raises(StorageError, match="managed by the DiskManager"):
                dm.write_page(pid, page.new_page(pid, page_type))


def test_catalog_root_must_be_a_real_page(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        with pytest.raises(StorageError):
            dm.set_catalog_root(5)
        dm.set_catalog_root(INVALID_PAGE_ID)
        assert dm.catalog_root == INVALID_PAGE_ID


# ------------------------------------------------------------------ lifecycle and counters


def test_counters_track_physical_io(db_path: Path) -> None:
    with DiskManager(db_path) as dm:
        reads, writes = dm.num_reads, dm.num_writes
        pid = dm.allocate_page()  # writes the new page and the header page
        assert dm.num_writes == writes + 2
        dm.read_page(pid)
        dm.read_page(pid)
        assert dm.num_reads == reads + 2


def test_close_is_idempotent_and_blocks_further_use(db_path: Path) -> None:
    dm = DiskManager(db_path)
    pid = dm.allocate_page()
    dm.close()
    dm.close()
    assert dm.closed
    with pytest.raises(StorageError, match="closed"):
        dm.read_page(pid)
    db_path.unlink()  # the handle really is released


def test_empty_existing_file_is_initialised(db_path: Path) -> None:
    db_path.touch()
    with DiskManager(db_path) as dm:
        assert dm.num_pages == 1
