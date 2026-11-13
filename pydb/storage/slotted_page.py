"""Slotted pages: variable-length tuples addressed by a stable slot id.

Layout after the 24-byte common page header::

    offset  size  field
    24      2     slot_count     (u16)
    26      2     free_ptr       (u16)  start of the tuple area; PAGE_SIZE when empty
    28      4     next_page_id   (i32)
    32      4     prev_page_id   (i32)
    36      4*n   slot array: (offset u16, length u16) per slot
    ...           free space
    free_ptr      tuple data, growing backward from the end of the page

A slot with offset 0 is a tombstone: the tuple was deleted. Slot ids are never reused, and
``compact()`` moves tuple bytes without changing slot ids, so a RID stays valid for the life
of its row.
"""

import struct
from collections.abc import Iterator

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import CorruptPageError, RecordNotFoundError, RowTooLargeError
from pydb.storage import page
from pydb.storage.page import PageType

_PAGE_HEADER = struct.Struct("<HHii")
_SLOT = struct.Struct("<HH")

SLOT_ARRAY_OFFSET = page.HEADER_SIZE + _PAGE_HEADER.size
SLOT_SIZE = _SLOT.size
MAX_TUPLE_SIZE = PAGE_SIZE - SLOT_ARRAY_OFFSET - SLOT_SIZE
"""Largest tuple that fits on an empty page together with its slot."""

_TOMBSTONE = 0


def format_page(page_id: int, prev_page_id: int = INVALID_PAGE_ID) -> bytearray:
    """Return a new, empty TABLE_HEAP page buffer."""
    buf = page.new_page(page_id, PageType.TABLE_HEAP)
    _PAGE_HEADER.pack_into(buf, page.HEADER_SIZE, 0, PAGE_SIZE, INVALID_PAGE_ID, prev_page_id)
    return buf


class SlottedPage:
    """A view over a TABLE_HEAP page buffer. Changes are made in place on ``buf``."""

    def __init__(self, buf: bytearray) -> None:
        header = page.read_header(buf)
        if header.page_type is not PageType.TABLE_HEAP:
            raise CorruptPageError(
                f"page {header.page_id} is a {header.page_type.name} page, not TABLE_HEAP"
            )
        self.buf = buf
        self.page_id = header.page_id

    # ----------------------------------------------------------------- header fields

    @property
    def slot_count(self) -> int:
        value: int = _PAGE_HEADER.unpack_from(self.buf, page.HEADER_SIZE)[0]
        return value

    @property
    def free_ptr(self) -> int:
        value: int = _PAGE_HEADER.unpack_from(self.buf, page.HEADER_SIZE)[1]
        return value

    @property
    def next_page_id(self) -> int:
        value: int = _PAGE_HEADER.unpack_from(self.buf, page.HEADER_SIZE)[2]
        return value

    @next_page_id.setter
    def next_page_id(self, page_id: int) -> None:
        self._set_header(next_page_id=page_id)

    @property
    def prev_page_id(self) -> int:
        value: int = _PAGE_HEADER.unpack_from(self.buf, page.HEADER_SIZE)[3]
        return value

    @prev_page_id.setter
    def prev_page_id(self, page_id: int) -> None:
        self._set_header(prev_page_id=page_id)

    # ----------------------------------------------------------------- space accounting

    @property
    def free_space(self) -> int:
        """Contiguous bytes between the end of the slot array and the tuple area."""
        return self.free_ptr - self._slot_array_end()

    def reclaimable_space(self) -> int:
        """Bytes that would be free after ``compact()``."""
        live = sum(length for offset, length in self._slots() if offset != _TOMBSTONE)
        return PAGE_SIZE - self._slot_array_end() - live

    # ----------------------------------------------------------------- tuple operations

    def insert_tuple(self, data: bytes) -> int | None:
        """Store ``data`` and return its slot id, or None if the page has no room."""
        _check_size(data)
        needed = len(data) + SLOT_SIZE
        if self.free_space < needed:
            if self.reclaimable_space() < needed:
                return None
            self.compact()
        slot_id = self.slot_count
        offset = self.free_ptr - len(data)
        self.buf[offset : offset + len(data)] = data
        _SLOT.pack_into(self.buf, self._slot_offset(slot_id), offset, len(data))
        self._set_header(slot_count=slot_id + 1, free_ptr=offset)
        return slot_id

    def get_tuple(self, slot_id: int) -> bytes:
        offset, length = self._live_slot(slot_id)
        return bytes(self.buf[offset : offset + length])

    def update_tuple(self, slot_id: int, data: bytes) -> bool:
        """Replace the tuple in place. Return False if the new version does not fit here."""
        _check_size(data)
        offset, length = self._live_slot(slot_id)
        if len(data) <= length:
            self.buf[offset : offset + len(data)] = data
            self._set_slot(slot_id, offset, len(data))
            return True
        if self.reclaimable_space() + length < len(data):
            return False
        self._set_slot(slot_id, _TOMBSTONE, 0)
        if self.free_space < len(data):
            self.compact()
        new_offset = self.free_ptr - len(data)
        self.buf[new_offset : new_offset + len(data)] = data
        self._set_slot(slot_id, new_offset, len(data))
        self._set_header(free_ptr=new_offset)
        return True

    def delete_tuple(self, slot_id: int) -> None:
        self._live_slot(slot_id)
        self._set_slot(slot_id, _TOMBSTONE, 0)

    def is_live(self, slot_id: int) -> bool:
        return 0 <= slot_id < self.slot_count and self._slot(slot_id)[0] != _TOMBSTONE

    def live_slots(self) -> Iterator[int]:
        for slot_id, (offset, _) in enumerate(self._slots()):
            if offset != _TOMBSTONE:
                yield slot_id

    def compact(self) -> None:
        """Pack live tuples against the end of the page. Slot ids do not change."""
        live = [
            (slot_id, bytes(self.buf[offset : offset + length]))
            for slot_id, (offset, length) in enumerate(self._slots())
            if offset != _TOMBSTONE
        ]
        end = self._slot_array_end()
        self.buf[end:PAGE_SIZE] = bytes(PAGE_SIZE - end)
        free_ptr = PAGE_SIZE
        for slot_id, data in live:
            free_ptr -= len(data)
            self.buf[free_ptr : free_ptr + len(data)] = data
            self._set_slot(slot_id, free_ptr, len(data))
        self._set_header(free_ptr=free_ptr)

    # ----------------------------------------------------------------- internals

    def _slot_offset(self, slot_id: int) -> int:
        return SLOT_ARRAY_OFFSET + slot_id * SLOT_SIZE

    def _slot_array_end(self) -> int:
        return self._slot_offset(self.slot_count)

    def _slot(self, slot_id: int) -> tuple[int, int]:
        offset, length = _SLOT.unpack_from(self.buf, self._slot_offset(slot_id))
        return offset, length

    def _slots(self) -> Iterator[tuple[int, int]]:
        for slot_id in range(self.slot_count):
            yield self._slot(slot_id)

    def _set_slot(self, slot_id: int, offset: int, length: int) -> None:
        _SLOT.pack_into(self.buf, self._slot_offset(slot_id), offset, length)

    def _live_slot(self, slot_id: int) -> tuple[int, int]:
        if not 0 <= slot_id < self.slot_count:
            raise RecordNotFoundError(f"page {self.page_id} has no slot {slot_id}")
        offset, length = self._slot(slot_id)
        if offset == _TOMBSTONE:
            raise RecordNotFoundError(f"slot {slot_id} on page {self.page_id} is deleted")
        return offset, length

    def _set_header(
        self,
        *,
        slot_count: int | None = None,
        free_ptr: int | None = None,
        next_page_id: int | None = None,
        prev_page_id: int | None = None,
    ) -> None:
        current = _PAGE_HEADER.unpack_from(self.buf, page.HEADER_SIZE)
        _PAGE_HEADER.pack_into(
            self.buf,
            page.HEADER_SIZE,
            current[0] if slot_count is None else slot_count,
            current[1] if free_ptr is None else free_ptr,
            current[2] if next_page_id is None else next_page_id,
            current[3] if prev_page_id is None else prev_page_id,
        )


def _check_size(data: bytes) -> None:
    if len(data) > MAX_TUPLE_SIZE:
        raise RowTooLargeError(
            f"row is {len(data)} bytes; the largest row that fits on a page is "
            f"{MAX_TUPLE_SIZE} bytes"
        )
