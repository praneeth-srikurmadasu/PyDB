"""Page-granular access to the database file, with a header page and a free list.

The file is an array of PAGE_SIZE pages; page ``n`` lives at offset ``n * PAGE_SIZE``.
Page 0 is the header page. Its body, after the common page header, is::

    offset  size  field
    24      8     magic           b"PYDB0001"
    32      4     format_version  (u32)
    36      4     page_size       (u32)
    40      4     catalog_root    (i32)  INVALID_PAGE_ID until a catalog exists
    44      4     next_page_id    (i32)  one past the highest page ever allocated
    48      4     free_list_head  (i32)  INVALID_PAGE_ID when the free list is empty
    52      4     free_page_count (u32)

Freed pages form a stack threaded through the pages themselves: a FREE page stores the id
of the next free page at offset 24. Allocation pops from the stack before growing the file.
"""

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from pydb.config import HEADER_PAGE_ID, INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import CorruptPageError, StorageError
from pydb.storage import page
from pydb.storage.page import PageType

MAGIC = b"PYDB0001"
FORMAT_VERSION = 1

_HEADER_BODY = struct.Struct("<8sIIiiiI")
_FREE_NEXT = struct.Struct("<i")


@dataclass(slots=True)
class _FileHeader:
    catalog_root: int
    next_page_id: int
    free_list_head: int
    free_page_count: int


class DiskManager:
    """Reads, writes, allocates and frees pages in a single database file.

    Not thread-safe; the buffer pool serialises access to it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.num_reads = 0
        self.num_writes = 0
        self._file: BinaryIO | None = None

        is_new = not self.path.exists() or self.path.stat().st_size == 0
        file = open(self.path, "w+b" if is_new else "r+b", buffering=0)  # noqa: SIM115
        self._file = file
        try:
            if is_new:
                self._header = _FileHeader(INVALID_PAGE_ID, HEADER_PAGE_ID + 1, INVALID_PAGE_ID, 0)
                self._write_header_page()
                self.sync()
            else:
                self._header = self._load_header()
        except BaseException:
            self._file = None
            file.close()
            raise

    # ----------------------------------------------------------------- lifecycle

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._file is None

    def sync(self) -> None:
        """Force every written page to stable storage."""
        file = self._require_open()
        file.flush()
        os.fsync(file.fileno())

    def close(self) -> None:
        """Sync and close the file. Calling it again does nothing."""
        if self._file is None:
            return
        try:
            self.sync()
        finally:
            self._file.close()
            self._file = None

    # ----------------------------------------------------------------- header fields

    @property
    def num_pages(self) -> int:
        """Number of page ids ever handed out, including page 0 and freed pages."""
        return self._header.next_page_id

    @property
    def free_page_count(self) -> int:
        return self._header.free_page_count

    @property
    def catalog_root(self) -> int:
        return self._header.catalog_root

    def set_catalog_root(self, page_id: int) -> None:
        if page_id != INVALID_PAGE_ID:
            self._check_data_page_id(page_id)
        self._header.catalog_root = page_id
        self._write_header_page()

    # ----------------------------------------------------------------- page I/O

    def read_page(self, page_id: int) -> bytearray:
        """Return a verified copy of the page. Raise CorruptPageError if it fails its checks."""
        if not 0 <= page_id < self._header.next_page_id:
            raise StorageError(f"read of page {page_id}: no such page")
        buf = self._read_raw(page_id)
        page.verify(buf, page_id)
        return buf

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        """Seal the page's checksum and write it. Page 0 belongs to the DiskManager."""
        self._check_data_page_id(page_id)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"write of page {page_id}: expected {PAGE_SIZE} bytes")
        buf = bytearray(data)
        header = page.read_header(buf)
        if header.page_id != page_id:
            raise StorageError(f"write of page {page_id}: buffer header says {header.page_id}")
        if header.page_type in (PageType.HEADER, PageType.FREE):
            raise StorageError(
                f"write of page {page_id}: {header.page_type.name} pages are managed "
                "by the DiskManager"
            )
        self._write_raw(page_id, buf)

    # ----------------------------------------------------------------- allocation

    def allocate_page(self) -> int:
        """Return the id of a fresh EMPTY page, reusing a freed page when one exists."""
        head = self._header.free_list_head
        if head != INVALID_PAGE_ID:
            freed = self.read_page(head)
            if page.read_header(freed).page_type is not PageType.FREE:
                raise CorruptPageError(f"free list head {head} is not a FREE page")
            (next_free,) = _FREE_NEXT.unpack_from(freed, page.HEADER_SIZE)
            page_id = head
            self._header.free_list_head = next_free
            self._header.free_page_count -= 1
        else:
            page_id = self._header.next_page_id
            self._header.next_page_id += 1
        self._write_raw(page_id, page.new_page(page_id, PageType.EMPTY))
        self._write_header_page()
        return page_id

    def deallocate_page(self, page_id: int) -> None:
        """Put a page on the free list. Freeing a page twice raises StorageError."""
        self._check_data_page_id(page_id)
        current = self.read_page(page_id)
        if page.read_header(current).page_type is PageType.FREE:
            raise StorageError(f"page {page_id} is already free")
        buf = page.new_page(page_id, PageType.FREE)
        _FREE_NEXT.pack_into(buf, page.HEADER_SIZE, self._header.free_list_head)
        self._write_raw(page_id, buf)
        self._header.free_list_head = page_id
        self._header.free_page_count += 1
        self._write_header_page()

    # ----------------------------------------------------------------- internals

    def _require_open(self) -> BinaryIO:
        if self._file is None:
            raise StorageError(f"{self.path}: disk manager is closed")
        return self._file

    def _check_data_page_id(self, page_id: int) -> None:
        if page_id == HEADER_PAGE_ID:
            raise StorageError("page 0 is the header page and is managed by the DiskManager")
        if not 0 < page_id < self._header.next_page_id:
            raise StorageError(f"page {page_id}: no such page")

    def _read_raw(self, page_id: int) -> bytearray:
        file = self._require_open()
        file.seek(page_id * PAGE_SIZE)
        data = file.read(PAGE_SIZE)
        self.num_reads += 1
        if data is None or len(data) != PAGE_SIZE:
            got = 0 if data is None else len(data)
            raise CorruptPageError(f"page {page_id}: short read ({got} of {PAGE_SIZE} bytes)")
        return bytearray(data)

    def _write_raw(self, page_id: int, buf: bytearray) -> None:
        file = self._require_open()
        page.seal(buf)
        file.seek(page_id * PAGE_SIZE)
        written = file.write(buf)
        if written != PAGE_SIZE:
            raise StorageError(f"page {page_id}: short write ({written} of {PAGE_SIZE} bytes)")
        self.num_writes += 1

    def _write_header_page(self) -> None:
        buf = page.new_page(HEADER_PAGE_ID, PageType.HEADER)
        h = self._header
        _HEADER_BODY.pack_into(
            buf,
            page.HEADER_SIZE,
            MAGIC,
            FORMAT_VERSION,
            PAGE_SIZE,
            h.catalog_root,
            h.next_page_id,
            h.free_list_head,
            h.free_page_count,
        )
        self._write_raw(HEADER_PAGE_ID, buf)

    def _load_header(self) -> _FileHeader:
        buf = self._read_raw(HEADER_PAGE_ID)
        header = page.verify(buf, HEADER_PAGE_ID)
        if header.page_type is not PageType.HEADER:
            raise CorruptPageError(f"{self.path}: page 0 is not a header page")
        magic, version, page_size, catalog_root, next_page_id, free_head, free_count = (
            _HEADER_BODY.unpack_from(buf, page.HEADER_SIZE)
        )
        if magic != MAGIC:
            raise CorruptPageError(f"{self.path}: not a PyDB file (magic {magic!r})")
        if version != FORMAT_VERSION:
            raise StorageError(f"{self.path}: unsupported format version {version}")
        if page_size != PAGE_SIZE:
            raise StorageError(f"{self.path}: page size {page_size}, expected {PAGE_SIZE}")
        size = self.path.stat().st_size
        if size < next_page_id * PAGE_SIZE:
            raise CorruptPageError(
                f"{self.path}: file has {size} bytes but the header records {next_page_id} pages"
            )
        return _FileHeader(catalog_root, next_page_id, free_head, free_count)
