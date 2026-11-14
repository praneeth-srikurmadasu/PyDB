"""A fixed-size cache of pages in memory, with pinning, dirty tracking and LRU eviction.

Locking rules:

* ``_latch`` (the pool latch) protects the page table, frame metadata (page id, pin count,
  dirty flag), the free-frame list, the replacer, the statistics and all disk I/O.
* ``Frame.latch`` (a reader-writer latch) protects a frame's bytes. Callers take it while the
  frame is pinned, usually through ``BufferPoolManager.page()``.
* Lock order is always frame latch first, then pool latch. Code holding the pool latch never
  waits for a frame latch: eviction only ever touches unpinned frames, which nobody can be
  holding a latch on.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from pydb.config import BUFFER_POOL_SIZE, INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import BufferPoolFullError, StorageError
from pydb.storage import page
from pydb.storage.disk_manager import DiskManager
from pydb.storage.latch import RWLatch
from pydb.storage.page import PageType
from pydb.storage.replacer import LRUReplacer

LogFlusher = Callable[[int], None]
"""Called with a page's LSN before that page is written; must make the log durable up to it."""

ByteRanges = list[tuple[int, bytes, bytes]]
"""Changed parts of a page: ``(offset, before, after)``."""

DIFF_CHUNK = 32


class PageLogger(Protocol):
    """What the buffer pool needs from the write-ahead log."""

    def log_page_change(self, page_id: int, ranges: ByteRanges) -> int:
        """Log a change to a page and return the record's LSN."""
        ...

    def current_lsn(self) -> int:
        """An LSN newer than every record written so far and older than every future one."""
        ...

    def flush_log(self) -> None: ...

    def defer_free(self, page_id: int) -> bool:
        """Return True if freeing the page must wait until the transaction commits."""
        ...


def diff_pages(before: bytes, after: bytes | bytearray) -> ByteRanges:
    """The byte ranges where ``after`` differs from ``before``, in 32-byte granules."""
    ranges: ByteRanges = []
    start = -1
    size = len(before)
    for offset in range(0, size, DIFF_CHUNK):
        end = min(offset + DIFF_CHUNK, size)
        if before[offset:end] != after[offset:end]:
            if start < 0:
                start = offset
        elif start >= 0:
            ranges.append((start, before[start:offset], bytes(after[start:offset])))
            start = -1
    if start >= 0:
        ranges.append((start, before[start:], bytes(after[start:])))
    return ranges


@dataclass(slots=True)
class Frame:
    frame_id: int
    data: bytearray = field(default_factory=lambda: bytearray(PAGE_SIZE))
    page_id: int = INVALID_PAGE_ID
    pin_count: int = 0
    dirty: bool = False
    latch: RWLatch = field(default_factory=RWLatch)

    @property
    def page_lsn(self) -> int:
        return page.read_header(self.data).page_lsn


@dataclass(frozen=True, slots=True)
class BufferPoolStats:
    hits: int
    misses: int
    evictions: int
    writes: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class BufferPoolManager:
    def __init__(
        self,
        disk: DiskManager,
        pool_size: int = BUFFER_POOL_SIZE,
        log_flusher: LogFlusher | None = None,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        self.disk = disk
        self.pool_size = pool_size
        self._log_flusher = log_flusher
        self._frames = [Frame(i) for i in range(pool_size)]
        self._page_table: dict[int, int] = {}
        self._free_frames = list(range(pool_size - 1, -1, -1))
        self._replacer = LRUReplacer()
        self._latch = threading.Lock()
        self._hits = self._misses = self._evictions = self._writes = 0
        self.logger: PageLogger | None = None
        self._logged_since_checkpoint: set[int] = set()
        self._unlogged_pages: set[int] = set()
        self._local = threading.local()

    # ----------------------------------------------------------------- pinning

    def fetch_page(self, page_id: int) -> Frame:
        """Pin ``page_id`` in a frame, reading it from disk on a miss."""
        with self._latch:
            frame_id = self._page_table.get(page_id)
            if frame_id is not None:
                frame = self._frames[frame_id]
                self._pin(frame)
                self._hits += 1
                return frame
            self._misses += 1
            frame = self._take_frame()
            try:
                frame.data[:] = self.disk.read_page(page_id)
            except BaseException:
                self._free_frames.append(frame.frame_id)
                raise
            self._install(frame, page_id)
            return frame

    def new_page(self) -> Frame:
        """Allocate a page on disk and pin it in a frame as a zeroed EMPTY page."""
        with self._latch:
            frame = self._take_frame()
            lsn = self.logger.current_lsn() if self.logger is not None else 0
            try:
                page_id = self.disk.allocate_page(lsn)
            except BaseException:
                self._free_frames.append(frame.frame_id)
                raise
            self._drop_stale(page_id)
            frame.data[:] = page.new_page(page_id, PageType.EMPTY)
            page.write_header(frame.data, page.PageHeader(page_id, PageType.EMPTY, lsn))
            self._install(frame, page_id)
            if getattr(self._local, "unlogged", False):
                self._unlogged_pages.add(page_id)
            return frame

    def unpin_page(self, page_id: int, dirty: bool) -> None:
        """Release one pin. ``dirty`` records that the caller changed the page."""
        with self._latch:
            frame = self._resident(page_id)
            if frame.pin_count <= 0:
                raise StorageError(f"unpin of page {page_id}, which is not pinned")
            frame.dirty = frame.dirty or dirty
            frame.pin_count -= 1
            if frame.pin_count == 0:
                self._replacer.unpin(frame.frame_id)

    @contextmanager
    def page(self, page_id: int, *, write: bool = False) -> Iterator[Frame]:
        """Pin and latch a page for the duration of a ``with`` block.

        On exit the latch is released and the page unpinned, marked dirty if ``write``.
        """
        frame = self.fetch_page(page_id)
        try:
            if not write:
                with frame.latch.read():
                    yield frame
            else:
                with frame.latch.write(), self._logged(frame):
                    yield frame
        finally:
            self.unpin_page(page_id, dirty=write)

    @contextmanager
    def create_page(self) -> Iterator[Frame]:
        """Allocate, pin and write-latch a new page for the duration of a ``with`` block."""
        frame = self.new_page()
        page_id = frame.page_id
        try:
            with frame.latch.write(), self._logged(frame):
                yield frame
        finally:
            self.unpin_page(page_id, dirty=True)

    @contextmanager
    def _logged(self, frame: Frame) -> Iterator[None]:
        """Log whatever the enclosed block changes in ``frame`` (the caller holds its latch)."""
        if self.logger is None or frame.page_id in self._unlogged_pages:
            yield
            return
        before = bytes(frame.data)
        try:
            yield
        finally:
            ranges = diff_pages(before, frame.data)
            if ranges:
                if frame.page_id not in self._logged_since_checkpoint:
                    # the first change after a checkpoint logs the whole page, so that a
                    # page torn by a crash during its next write can be rebuilt by redo
                    ranges = [(0, before, bytes(frame.data))]
                    self._logged_since_checkpoint.add(frame.page_id)
                lsn = self.logger.log_page_change(frame.page_id, ranges)
                header = page.read_header(frame.data)
                page.write_header(
                    frame.data, page.PageHeader(header.page_id, header.page_type, lsn)
                )

    @contextmanager
    def unlogged(self) -> Iterator[None]:
        """Pages created inside this block are temporary: never logged, freed immediately."""
        previous = getattr(self._local, "unlogged", False)
        self._local.unlogged = True
        try:
            yield
        finally:
            self._local.unlogged = previous

    def install_page(self, page_id: int, data: bytes) -> Frame:
        """Pin ``page_id`` with ``data`` instead of reading it (redo of a torn page)."""
        with self._latch:
            frame_id = self._page_table.get(page_id)
            if frame_id is not None:
                frame = self._frames[frame_id]
                self._pin(frame)
            else:
                frame = self._take_frame()
                self._install(frame, page_id)
            frame.data[:] = data
            return frame

    def start_checkpoint(self) -> None:
        """Called once every dirty page is flushed: the next change to each page is logged
        as a full image again."""
        with self._latch:
            self._logged_since_checkpoint.clear()

    # ----------------------------------------------------------------- writing back

    def flush_page(self, page_id: int) -> None:
        """Write the page to disk if it is resident and dirty."""
        with self._latch:
            frame_id = self._page_table.get(page_id)
            if frame_id is None:
                return
            frame = self._frames[frame_id]
            self._pin(frame)
        try:
            with frame.latch.read(), self._latch:  # frame latch first, then pool latch
                if frame.dirty:
                    self._write_back(frame)
        finally:
            self.unpin_page(page_id, dirty=False)

    def flush_all(self) -> None:
        with self._latch:
            resident = list(self._page_table)
        for page_id in resident:
            self.flush_page(page_id)

    def delete_page(self, page_id: int, *, deferred: bool = False) -> None:
        """Drop the page from the pool and return it to the disk free list.

        With a write-ahead log, a page freed by a transaction that may still be undone
        physically is only freed when it commits (``deferred`` is then set by the commit).
        Otherwise the log is flushed first, so no durable page refers to a freed page.
        """
        logger = self.logger
        lsn = 0
        if logger is not None and page_id not in self._unlogged_pages:
            if not deferred and logger.defer_free(page_id):
                return
            logger.flush_log()
            lsn = logger.current_lsn()
        with self._latch:
            self._unlogged_pages.discard(page_id)
            self._logged_since_checkpoint.discard(page_id)
            frame_id = self._page_table.get(page_id)
            if frame_id is not None:
                frame = self._frames[frame_id]
                if frame.pin_count > 0:
                    raise StorageError(f"cannot delete page {page_id}: it is pinned")
                self._replacer.pin(frame_id)
                del self._page_table[page_id]
                self._reset(frame)
                self._free_frames.append(frame_id)
            self.disk.deallocate_page(page_id, lsn, known_live=frame_id is not None)

    # ----------------------------------------------------------------- introspection

    @property
    def stats(self) -> BufferPoolStats:
        with self._latch:
            return BufferPoolStats(self._hits, self._misses, self._evictions, self._writes)

    def reset_stats(self) -> None:
        with self._latch:
            self._hits = self._misses = self._evictions = self._writes = 0

    def pinned_pages(self) -> dict[int, int]:
        """Page id -> pin count for every pinned page. Empty when no pins have leaked."""
        with self._latch:
            return {f.page_id: f.pin_count for f in self._frames if f.pin_count > 0}

    def check_invariants(self) -> None:
        """Every cached page is in exactly one frame, and the page table agrees with it."""
        with self._latch:
            seen: dict[int, int] = {}
            for frame in self._frames:
                if frame.page_id == INVALID_PAGE_ID:
                    continue
                if frame.page_id in seen:
                    first = seen[frame.page_id]
                    raise StorageError(
                        f"page {frame.page_id} is in frames {first} and {frame.frame_id}"
                    )
                seen[frame.page_id] = frame.frame_id
            if seen != self._page_table:
                raise StorageError("the page table does not match the frames")

    def is_resident(self, page_id: int) -> bool:
        with self._latch:
            return page_id in self._page_table

    def is_dirty(self, page_id: int) -> bool:
        with self._latch:
            return self._resident(page_id).dirty

    # ----------------------------------------------------------------- internals (pool latch held)

    def _pin(self, frame: Frame) -> None:
        if frame.pin_count == 0:
            self._replacer.pin(frame.frame_id)
        frame.pin_count += 1

    def _drop_stale(self, page_id: int) -> None:
        """Forget a cached copy of a page that the disk has just handed out as new.

        This happens after recovery: redo reads pages named in old log records, including
        pages freed later on, and such a freed page can then be allocated again.
        """
        frame_id = self._page_table.get(page_id)
        if frame_id is None:
            return
        frame = self._frames[frame_id]
        if frame.pin_count > 0:
            raise StorageError(f"page {page_id} was allocated while pinned")
        self._replacer.pin(frame_id)
        del self._page_table[page_id]
        self._reset(frame)
        self._free_frames.append(frame_id)

    def _install(self, frame: Frame, page_id: int) -> None:
        frame.page_id = page_id
        frame.pin_count = 1
        frame.dirty = False
        self._page_table[page_id] = frame.frame_id

    def _take_frame(self) -> Frame:
        """Return an empty, unmapped frame: a free one, or an evicted victim."""
        if self._free_frames:
            return self._frames[self._free_frames.pop()]
        frame_id = self._replacer.victim()
        if frame_id is None:
            raise BufferPoolFullError(f"all {self.pool_size} buffer pool frames are pinned")
        frame = self._frames[frame_id]
        if frame.dirty:
            try:
                self._write_back(frame)
            except BaseException:
                self._replacer.unpin(frame_id)
                raise
        del self._page_table[frame.page_id]
        self._reset(frame)
        self._evictions += 1
        return frame

    def _write_back(self, frame: Frame) -> None:
        if self._log_flusher is not None:
            self._log_flusher(frame.page_lsn)
        self.disk.write_page(frame.page_id, frame.data)
        frame.dirty = False
        self._writes += 1

    def _resident(self, page_id: int) -> Frame:
        frame_id = self._page_table.get(page_id)
        if frame_id is None:
            raise StorageError(f"page {page_id} is not in the buffer pool")
        return self._frames[frame_id]

    @staticmethod
    def _reset(frame: Frame) -> None:
        frame.page_id = INVALID_PAGE_ID
        frame.pin_count = 0
        frame.dirty = False
