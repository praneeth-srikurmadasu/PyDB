import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.config import PAGE_SIZE
from pydb.errors import BufferPoolFullError, CorruptPageError, StorageError
from pydb.storage import page
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.storage.page import PageHeader, PageType

BODY = page.HEADER_SIZE


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[DiskManager]:
    dm = DiskManager(tmp_path / "pool.db")
    yield dm
    dm.close()


def make_pages(disk: DiskManager, count: int) -> list[int]:
    """Allocate pages directly on disk, each with its id written at the start of the body."""
    ids = []
    for _ in range(count):
        pid = disk.allocate_page()
        buf = page.new_page(pid, PageType.EMPTY)
        buf[BODY : BODY + 4] = pid.to_bytes(4, "little")
        disk.write_page(pid, buf)
        ids.append(pid)
    return ids


def body_id(data: bytearray) -> int:
    return int.from_bytes(data[BODY : BODY + 4], "little")


# ------------------------------------------------------------------ hits, misses, pins


def test_first_fetch_misses_then_hits(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 3)
    reads = disk.num_reads
    with pool.page(pid) as frame:
        assert body_id(frame.data) == pid
    with pool.page(pid):
        pass
    assert disk.num_reads == reads + 1
    stats = pool.stats
    assert (stats.hits, stats.misses, stats.evictions) == (1, 1, 0)
    assert stats.hit_rate == 0.5


def test_pin_counts_stack(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 2)
    pool.fetch_page(pid)
    pool.fetch_page(pid)
    assert pool.pinned_pages() == {pid: 2}
    pool.unpin_page(pid, dirty=False)
    assert pool.pinned_pages() == {pid: 1}
    pool.unpin_page(pid, dirty=False)
    assert pool.pinned_pages() == {}


def test_unpin_misuse_is_an_error(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 2)
    with pytest.raises(StorageError, match="not in the buffer pool"):
        pool.unpin_page(pid, dirty=False)
    with pool.page(pid):
        pass
    with pytest.raises(StorageError, match="not pinned"):
        pool.unpin_page(pid, dirty=False)


def test_context_manager_unpins_on_exception(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 2)
    with pytest.raises(ZeroDivisionError), pool.page(pid, write=True):
        _ = 1 / 0
    assert pool.pinned_pages() == {}
    assert pool.is_dirty(pid)


def test_pool_size_must_be_positive(disk: DiskManager) -> None:
    with pytest.raises(ValueError):
        BufferPoolManager(disk, 0)


# ------------------------------------------------------------------ eviction


def test_lru_page_is_evicted_first(disk: DiskManager) -> None:
    a, b, c, d = make_pages(disk, 4)
    pool = BufferPoolManager(disk, 3)
    for pid in (a, b, c):
        with pool.page(pid):
            pass
    with pool.page(a):  # a becomes most recently used; b is now the LRU page
        pass
    with pool.page(d):
        pass
    assert not pool.is_resident(b)
    assert all(pool.is_resident(pid) for pid in (a, c, d))
    assert pool.stats.evictions == 1


def test_pinned_pages_are_never_evicted(disk: DiskManager) -> None:
    a, b, c = make_pages(disk, 3)
    pool = BufferPoolManager(disk, 2)
    pool.fetch_page(a)  # stays pinned
    with pool.page(b):
        pass
    with pool.page(c):
        pass
    assert pool.is_resident(a)
    assert not pool.is_resident(b)
    pool.unpin_page(a, dirty=False)


def test_full_pool_of_pinned_pages_raises(disk: DiskManager) -> None:
    a, b, c = make_pages(disk, 3)
    pool = BufferPoolManager(disk, 2)
    pool.fetch_page(a)
    pool.fetch_page(b)
    with pytest.raises(BufferPoolFullError):
        pool.fetch_page(c)
    with pytest.raises(BufferPoolFullError):
        pool.new_page()
    pages_before = disk.num_pages
    pool.unpin_page(a, dirty=False)
    with pool.page(c) as frame:
        assert body_id(frame.data) == c
    assert disk.num_pages == pages_before  # the failed new_page() allocated nothing
    pool.unpin_page(b, dirty=False)


def test_dirty_page_is_written_on_eviction(disk: DiskManager) -> None:
    a, b = make_pages(disk, 2)
    pool = BufferPoolManager(disk, 1)
    with pool.page(a, write=True) as frame:
        frame.data[100:105] = b"dirty"
    writes = disk.num_writes
    with pool.page(b):
        pass
    assert disk.num_writes == writes + 1
    assert disk.read_page(a)[100:105] == b"dirty"


def test_clean_page_is_not_written_on_eviction(disk: DiskManager) -> None:
    a, b = make_pages(disk, 2)
    pool = BufferPoolManager(disk, 1)
    with pool.page(a):
        pass
    writes = disk.num_writes
    with pool.page(b):
        pass
    assert disk.num_writes == writes
    assert pool.stats.writes == 0


def test_evicted_page_reads_back_its_changes(disk: DiskManager) -> None:
    ids = make_pages(disk, 10)
    pool = BufferPoolManager(disk, 3)
    for pid in ids:
        with pool.page(pid, write=True) as frame:
            frame.data[200] = pid
    for pid in ids:
        with pool.page(pid) as frame:
            assert frame.data[200] == pid
            assert body_id(frame.data) == pid
    assert pool.stats.evictions >= 7


def test_corrupt_page_does_not_leak_a_frame(disk: DiskManager, tmp_path: Path) -> None:
    a, b = make_pages(disk, 2)
    disk.close()
    with open(tmp_path / "pool.db", "r+b") as f:
        f.seek(a * PAGE_SIZE + 300)
        f.write(b"\xff")
    disk2 = DiskManager(tmp_path / "pool.db")
    pool = BufferPoolManager(disk2, 1)
    with pytest.raises(CorruptPageError):
        pool.fetch_page(a)
    assert not pool.is_resident(a)
    with pool.page(b) as frame:  # the only frame is still usable
        assert body_id(frame.data) == b
    disk2.close()


# ------------------------------------------------------------------ new, delete, flush


def test_new_page_is_pinned_empty_and_allocated(disk: DiskManager) -> None:
    pool = BufferPoolManager(disk, 2)
    with pool.create_page() as frame:
        pid = frame.page_id
        assert page.read_header(frame.data) == PageHeader(pid, PageType.EMPTY, 0)
        assert pool.pinned_pages() == {pid: 1}
        frame.data[BODY] = 7
    assert pool.pinned_pages() == {}
    pool.flush_page(pid)
    assert disk.read_page(pid)[BODY] == 7


def test_flush_page_writes_only_when_dirty(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 2)
    with pool.page(pid, write=True) as frame:
        frame.data[BODY + 10] = 9
    assert pool.is_dirty(pid)
    pool.flush_page(pid)
    assert not pool.is_dirty(pid)
    writes = disk.num_writes
    pool.flush_page(pid)
    pool.flush_page(9999)  # not resident: nothing to do
    assert disk.num_writes == writes
    assert disk.read_page(pid)[BODY + 10] == 9


def test_flush_all_writes_every_dirty_page(disk: DiskManager) -> None:
    ids = make_pages(disk, 3)
    pool = BufferPoolManager(disk, 3)
    for pid in ids:
        with pool.page(pid, write=True) as frame:
            frame.data[BODY + 20] = pid
    pool.flush_all()
    assert pool.stats.writes == 3
    for pid in ids:
        assert disk.read_page(pid)[BODY + 20] == pid


def test_delete_page_frees_frame_and_disk_page(disk: DiskManager) -> None:
    a, b = make_pages(disk, 2)
    pool = BufferPoolManager(disk, 1)
    with pool.page(a, write=True):
        pass
    pool.delete_page(a)
    assert not pool.is_resident(a)
    assert disk.free_page_count == 1
    with pool.page(b):  # the freed frame is reused without an eviction
        pass
    assert pool.stats.evictions == 0
    with pool.create_page() as frame:
        assert frame.page_id == a  # disk free list hands the page back


def test_delete_pinned_page_is_refused(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 1)
    pool.fetch_page(pid)
    with pytest.raises(StorageError, match="pinned"):
        pool.delete_page(pid)
    pool.unpin_page(pid, dirty=False)


def test_reset_stats(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 1)
    with pool.page(pid):
        pass
    pool.reset_stats()
    assert pool.stats.hits == pool.stats.misses == 0
    assert pool.stats.hit_rate == 0.0


# ------------------------------------------------------------------ write-ahead rule


def test_log_is_flushed_up_to_page_lsn_before_the_page_is_written(disk: DiskManager) -> None:
    a, b = make_pages(disk, 2)
    calls: list[tuple[int, int]] = []

    def flush_log(lsn: int) -> None:
        calls.append((lsn, disk.num_writes))

    pool = BufferPoolManager(disk, 1, log_flusher=flush_log)
    with pool.page(a, write=True) as frame:
        page.write_header(frame.data, PageHeader(a, PageType.EMPTY, 77))
    writes_before = disk.num_writes
    with pool.page(b):  # evicts a
        pass
    assert calls == [(77, writes_before)]  # called with page_lsn, before the write
    assert disk.num_writes == writes_before + 1
    assert page.read_header(disk.read_page(a)).page_lsn == 77


def test_log_flush_failure_keeps_the_page_dirty_and_resident(disk: DiskManager) -> None:
    a, b = make_pages(disk, 2)

    def failing_flush(lsn: int) -> None:
        raise OSError("log device unavailable")

    pool = BufferPoolManager(disk, 1, log_flusher=failing_flush)
    with pool.page(a, write=True):
        pass
    with pytest.raises(OSError):
        pool.fetch_page(b)
    assert pool.is_resident(a)
    assert pool.is_dirty(a)
    assert pool.pinned_pages() == {}


# ------------------------------------------------------------------ concurrency


def test_concurrent_writers_on_shared_pages_lose_nothing(disk: DiskManager) -> None:
    threads_count = 4
    ids = make_pages(disk, 8)
    # One frame per thread (each holds at most one pin) but twice as many pages as frames,
    # so pages are evicted and reloaded while threads contend for them.
    pool = BufferPoolManager(disk, threads_count)
    rounds = 150

    def worker(offset: int) -> None:
        for i in range(rounds):
            pid = ids[(i + offset) % len(ids)]
            with pool.page(pid, write=True) as frame:
                pos = BODY + 8
                count = int.from_bytes(frame.data[pos : pos + 4], "little")
                frame.data[pos : pos + 4] = (count + 1).to_bytes(4, "little")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert pool.pinned_pages() == {}
    total = 0
    for pid in ids:
        with pool.page(pid) as frame:
            total += int.from_bytes(frame.data[BODY + 8 : BODY + 12], "little")
    assert total == threads_count * rounds
    assert pool.stats.evictions > 0


def test_flush_waits_for_an_active_writer(disk: DiskManager) -> None:
    (pid,) = make_pages(disk, 1)
    pool = BufferPoolManager(disk, 2)
    writing = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with pool.page(pid, write=True) as frame:
            frame.data[BODY] = 1
            writing.set()
            release.wait(timeout=5)
            frame.data[BODY + 1] = 2  # the second half of a two-byte change

    t = threading.Thread(target=writer)
    t.start()
    writing.wait(timeout=5)
    flusher = threading.Thread(target=pool.flush_page, args=(pid,))
    flusher.start()
    flusher.join(timeout=0.2)
    assert flusher.is_alive()  # blocked behind the writer's latch
    release.set()
    t.join(timeout=5)
    flusher.join(timeout=5)
    pool.flush_page(pid)
    data = disk.read_page(pid)
    assert (data[BODY], data[BODY + 1]) == (1, 2)  # never half-written
