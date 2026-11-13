"""The common 24-byte page header and page checksums.

Every page starts with the same header::

    offset  size  field
    0       4     page_id    (i32)  the page's own id, to detect misdirected writes
    4       1     page_type  (u8)   a PageType value
    5       3     reserved
    8       8     page_lsn   (i64)  LSN of the last log record that changed the page
    16      4     checksum   (u32)  CRC32 of bytes [0:16] and [20:PAGE_SIZE]
    20      4     reserved

The checksum covers the whole page except its own four bytes.
"""

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

from pydb.config import PAGE_SIZE
from pydb.errors import CorruptPageError

HEADER_FORMAT = "<iB3xqI4x"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
CHECKSUM_OFFSET = 16
CHECKSUM_END = CHECKSUM_OFFSET + 4

_ID_TYPE_LSN = struct.Struct("<iB3xq")
_CHECKSUM = struct.Struct("<I")


class PageType(IntEnum):
    """What a page holds. Stored in byte 4 of the header."""

    EMPTY = 0
    """Allocated but not yet formatted by its owner."""
    HEADER = 1
    """Page 0: database file metadata."""
    FREE = 2
    """On the free list, waiting to be reused."""
    TABLE_HEAP = 3
    """A slotted page holding rows of one table."""


@dataclass(frozen=True, slots=True)
class PageHeader:
    page_id: int
    page_type: PageType
    page_lsn: int


def new_page(page_id: int, page_type: PageType) -> bytearray:
    """Return a zero-filled page buffer with its header fields set (checksum not yet sealed)."""
    buf = bytearray(PAGE_SIZE)
    write_header(buf, PageHeader(page_id, page_type, 0))
    return buf


def read_header(buf: bytes | bytearray) -> PageHeader:
    page_id, raw_type, page_lsn = _ID_TYPE_LSN.unpack_from(buf, 0)
    try:
        page_type = PageType(raw_type)
    except ValueError:
        raise CorruptPageError(f"page {page_id}: unknown page type {raw_type}") from None
    return PageHeader(page_id, page_type, page_lsn)


def write_header(buf: bytearray, header: PageHeader) -> None:
    """Write page_id, page_type and page_lsn. Leaves the checksum field untouched."""
    _ID_TYPE_LSN.pack_into(buf, 0, header.page_id, header.page_type, header.page_lsn)


def compute_checksum(buf: bytes | bytearray) -> int:
    view = memoryview(buf)
    crc = zlib.crc32(view[:CHECKSUM_OFFSET])
    return zlib.crc32(view[CHECKSUM_END:], crc)


def stored_checksum(buf: bytes | bytearray) -> int:
    value: int = _CHECKSUM.unpack_from(buf, CHECKSUM_OFFSET)[0]
    return value


def seal(buf: bytearray) -> None:
    """Compute the checksum and store it in the header. Call just before writing to disk."""
    _CHECKSUM.pack_into(buf, CHECKSUM_OFFSET, compute_checksum(buf))


def verify(buf: bytes | bytearray, expected_page_id: int) -> PageHeader:
    """Check size, checksum and page id. Raise CorruptPageError on any mismatch."""
    if len(buf) != PAGE_SIZE:
        raise CorruptPageError(
            f"page {expected_page_id}: expected {PAGE_SIZE} bytes, got {len(buf)}"
        )
    stored = stored_checksum(buf)
    actual = compute_checksum(buf)
    if stored != actual:
        raise CorruptPageError(
            f"page {expected_page_id}: checksum mismatch "
            f"(stored 0x{stored:08x}, computed 0x{actual:08x})"
        )
    header = read_header(buf)
    if header.page_id != expected_page_id:
        raise CorruptPageError(
            f"page {expected_page_id}: header says page {header.page_id} (misdirected write)"
        )
    return header
