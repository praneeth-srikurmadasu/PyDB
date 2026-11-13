"""A table's rows, stored as a doubly linked list of slotted pages.

Rows are opaque bytes here; ``pydb.storage.tuple.RowLayout`` turns them into values.
A row is addressed by its RID, ``(page_id, slot_id)``.
"""

from collections.abc import Iterator
from typing import NamedTuple

from pydb.config import INVALID_PAGE_ID
from pydb.errors import RowTooLargeError
from pydb.storage import slotted_page
from pydb.storage.disk_manager import DiskManager
from pydb.storage.slotted_page import MAX_TUPLE_SIZE, SlottedPage


class RID(NamedTuple):
    """Record id: where a row lives."""

    page_id: int
    slot_id: int


class TableHeap:
    """Insert, read, update, delete and scan the rows of one table."""

    def __init__(self, disk: DiskManager, first_page_id: int) -> None:
        self.disk = disk
        self.first_page_id = first_page_id
        self._load(first_page_id)  # fail fast if this is not a table heap page
        self._insert_hint = first_page_id

    @classmethod
    def create(cls, disk: DiskManager) -> "TableHeap":
        """Allocate the first page of a new, empty table."""
        page_id = disk.allocate_page()
        disk.write_page(page_id, slotted_page.format_page(page_id))
        return cls(disk, page_id)

    def insert(self, data: bytes) -> RID:
        if len(data) > MAX_TUPLE_SIZE:
            raise RowTooLargeError(
                f"row is {len(data)} bytes; the largest row that fits on a page is "
                f"{MAX_TUPLE_SIZE} bytes"
            )
        page_id = self._insert_hint
        while True:
            sp = self._load(page_id)
            slot_id = sp.insert_tuple(data)
            if slot_id is not None:
                self._store(sp)
                self._insert_hint = page_id
                return RID(page_id, slot_id)
            if sp.next_page_id == INVALID_PAGE_ID:
                page_id = self._append_page(sp)
            else:
                page_id = sp.next_page_id

    def get(self, rid: RID) -> bytes:
        return self._load(rid.page_id).get_tuple(rid.slot_id)

    def update(self, rid: RID, data: bytes) -> RID:
        """Replace a row. Return its RID, which differs from ``rid`` if the row had to move."""
        sp = self._load(rid.page_id)
        if sp.update_tuple(rid.slot_id, data):
            self._store(sp)
            return rid
        new_rid = self.insert(data)
        self.delete(rid)
        return new_rid

    def delete(self, rid: RID) -> None:
        sp = self._load(rid.page_id)
        sp.delete_tuple(rid.slot_id)
        self._store(sp)

    def scan(self) -> Iterator[tuple[RID, bytes]]:
        """Yield every live row, page by page in chain order, slot by slot."""
        for page_id in self.page_ids():
            sp = self._load(page_id)
            for slot_id in sp.live_slots():
                yield RID(page_id, slot_id), sp.get_tuple(slot_id)

    def page_ids(self) -> Iterator[int]:
        page_id = self.first_page_id
        while page_id != INVALID_PAGE_ID:
            yield page_id
            page_id = self._load(page_id).next_page_id

    # ----------------------------------------------------------------- internals

    def _append_page(self, tail: SlottedPage) -> int:
        """Add a page after ``tail``. The new page is written before it is linked in."""
        page_id = self.disk.allocate_page()
        self.disk.write_page(page_id, slotted_page.format_page(page_id, tail.page_id))
        tail.next_page_id = page_id
        self._store(tail)
        return page_id

    def _load(self, page_id: int) -> SlottedPage:
        return SlottedPage(self.disk.read_page(page_id))

    def _store(self, sp: SlottedPage) -> None:
        self.disk.write_page(sp.page_id, sp.buf)
