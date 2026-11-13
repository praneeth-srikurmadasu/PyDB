"""A table's rows, stored as a doubly linked list of slotted pages.

Rows are opaque bytes here; ``pydb.storage.tuple.RowLayout`` turns them into values.
A row is addressed by its RID, ``(page_id, slot_id)``. All page access goes through the
buffer pool, holding each page's latch only for the duration of one operation on it.
"""

from collections.abc import Iterator
from typing import NamedTuple

from pydb.config import INVALID_PAGE_ID
from pydb.errors import RowTooLargeError
from pydb.storage import slotted_page
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.slotted_page import MAX_TUPLE_SIZE, SlottedPage


class RID(NamedTuple):
    """Record id: where a row lives."""

    page_id: int
    slot_id: int


class TableHeap:
    """Insert, read, update, delete and scan the rows of one table."""

    def __init__(self, pool: BufferPoolManager, first_page_id: int) -> None:
        self.pool = pool
        self.first_page_id = first_page_id
        with pool.page(first_page_id) as frame:
            SlottedPage(frame.data)  # fail fast if this is not a table heap page
        self._insert_hint = first_page_id

    @classmethod
    def create(cls, pool: BufferPoolManager) -> "TableHeap":
        """Allocate the first page of a new, empty table."""
        with pool.create_page() as frame:
            frame.data[:] = slotted_page.format_page(frame.page_id)
            page_id = frame.page_id
        return cls(pool, page_id)

    def insert(self, data: bytes) -> RID:
        if len(data) > MAX_TUPLE_SIZE:
            raise RowTooLargeError(
                f"row is {len(data)} bytes; the largest row that fits on a page is "
                f"{MAX_TUPLE_SIZE} bytes"
            )
        page_id = self._insert_hint
        while True:
            with self.pool.page(page_id, write=True) as frame:
                sp = SlottedPage(frame.data)
                slot_id = sp.insert_tuple(data)
                if slot_id is not None:
                    self._insert_hint = page_id
                    return RID(page_id, slot_id)
                next_page_id = sp.next_page_id
                if next_page_id == INVALID_PAGE_ID:
                    next_page_id = self._append_page(sp)
            page_id = next_page_id

    def get(self, rid: RID) -> bytes:
        with self.pool.page(rid.page_id) as frame:
            return SlottedPage(frame.data).get_tuple(rid.slot_id)

    def update(self, rid: RID, data: bytes) -> RID:
        """Replace a row. Return its RID, which differs from ``rid`` if the row had to move."""
        with self.pool.page(rid.page_id, write=True) as frame:
            if SlottedPage(frame.data).update_tuple(rid.slot_id, data):
                return rid
        new_rid = self.insert(data)
        self.delete(rid)
        return new_rid

    def delete(self, rid: RID) -> None:
        with self.pool.page(rid.page_id, write=True) as frame:
            SlottedPage(frame.data).delete_tuple(rid.slot_id)

    def scan(self) -> Iterator[tuple[RID, bytes]]:
        """Yield every live row, page by page in chain order, slot by slot.

        Each page's rows are copied out under its latch, so no pin is held while the caller
        consumes them.
        """
        page_id = self.first_page_id
        while page_id != INVALID_PAGE_ID:
            with self.pool.page(page_id) as frame:
                sp = SlottedPage(frame.data)
                rows = [(RID(page_id, slot), sp.get_tuple(slot)) for slot in sp.live_slots()]
                page_id = sp.next_page_id
            yield from rows

    def page_ids(self) -> Iterator[int]:
        page_id = self.first_page_id
        while page_id != INVALID_PAGE_ID:
            yield page_id
            with self.pool.page(page_id) as frame:
                page_id = SlottedPage(frame.data).next_page_id

    # ----------------------------------------------------------------- internals

    def _append_page(self, tail: SlottedPage) -> int:
        """Link a new page after ``tail``, whose frame the caller holds write-latched."""
        with self.pool.create_page() as frame:
            frame.data[:] = slotted_page.format_page(frame.page_id, tail.page_id)
            page_id = frame.page_id
        tail.next_page_id = page_id
        return page_id
