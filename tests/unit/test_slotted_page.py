import pytest

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import CorruptPageError, RecordNotFoundError, RowTooLargeError
from pydb.storage import page, slotted_page
from pydb.storage.page import PageType
from pydb.storage.slotted_page import MAX_TUPLE_SIZE, SLOT_ARRAY_OFFSET, SlottedPage


def fresh(page_id: int = 1) -> SlottedPage:
    return SlottedPage(slotted_page.format_page(page_id))


def test_empty_page() -> None:
    sp = fresh()
    assert sp.slot_count == 0
    assert sp.free_ptr == PAGE_SIZE
    assert sp.next_page_id == INVALID_PAGE_ID
    assert sp.prev_page_id == INVALID_PAGE_ID
    assert sp.free_space == PAGE_SIZE - SLOT_ARRAY_OFFSET
    assert list(sp.live_slots()) == []


def test_rejects_other_page_types() -> None:
    with pytest.raises(CorruptPageError, match="not TABLE_HEAP"):
        SlottedPage(page.new_page(1, PageType.EMPTY))


def test_insert_and_get() -> None:
    sp = fresh()
    assert sp.insert_tuple(b"alpha") == 0
    assert sp.insert_tuple(b"beta") == 1
    assert sp.get_tuple(0) == b"alpha"
    assert sp.get_tuple(1) == b"beta"
    assert sp.free_ptr == PAGE_SIZE - 9
    assert sp.buf[PAGE_SIZE - 5 :] == b"alpha"  # first tuple at the very end


def test_link_fields() -> None:
    sp = fresh()
    sp.next_page_id = 9
    sp.prev_page_id = 4
    assert (sp.next_page_id, sp.prev_page_id) == (9, 4)


def test_delete_leaves_a_tombstone() -> None:
    sp = fresh()
    sp.insert_tuple(b"a")
    sp.insert_tuple(b"b")
    sp.delete_tuple(0)
    assert not sp.is_live(0)
    assert list(sp.live_slots()) == [1]
    with pytest.raises(RecordNotFoundError, match="deleted"):
        sp.get_tuple(0)
    with pytest.raises(RecordNotFoundError):
        sp.delete_tuple(0)
    assert sp.insert_tuple(b"c") == 2  # slot ids are not reused


def test_missing_slot() -> None:
    with pytest.raises(RecordNotFoundError, match="no slot"):
        fresh().get_tuple(0)


def test_update_in_place_when_smaller_or_equal() -> None:
    sp = fresh()
    sp.insert_tuple(b"hello world")
    free_ptr = sp.free_ptr
    assert sp.update_tuple(0, b"hi")
    assert sp.get_tuple(0) == b"hi"
    assert sp.free_ptr == free_ptr


def test_update_grows_within_the_page() -> None:
    sp = fresh()
    sp.insert_tuple(b"x")
    sp.insert_tuple(b"y")
    assert sp.update_tuple(0, b"x" * 100)
    assert sp.get_tuple(0) == b"x" * 100
    assert sp.get_tuple(1) == b"y"


def test_update_returns_false_when_it_cannot_fit() -> None:
    sp = fresh()
    sp.insert_tuple(b"a" * 2000)
    sp.insert_tuple(b"b" * 2000)
    assert not sp.update_tuple(0, b"c" * 2100)
    assert sp.get_tuple(0) == b"a" * 2000  # unchanged


def test_full_page_returns_none() -> None:
    sp = fresh()
    assert sp.insert_tuple(b"a" * MAX_TUPLE_SIZE) == 0
    assert sp.free_space == 0
    assert sp.insert_tuple(b"") is None


def test_too_large_tuple_raises() -> None:
    sp = fresh()
    with pytest.raises(RowTooLargeError):
        sp.insert_tuple(b"a" * (MAX_TUPLE_SIZE + 1))
    sp.insert_tuple(b"a")
    with pytest.raises(RowTooLargeError):
        sp.update_tuple(0, b"a" * (MAX_TUPLE_SIZE + 1))


def test_compact_keeps_slot_ids_and_reclaims_space() -> None:
    sp = fresh()
    for i in range(5):
        sp.insert_tuple(bytes([i]) * 100)
    sp.delete_tuple(1)
    sp.delete_tuple(3)
    before = sp.free_space
    sp.compact()
    assert sp.free_space == before + 200
    assert sp.free_space == sp.reclaimable_space()
    assert list(sp.live_slots()) == [0, 2, 4]
    for i in (0, 2, 4):
        assert sp.get_tuple(i) == bytes([i]) * 100


def test_insert_compacts_a_fragmented_page() -> None:
    sp = fresh()
    first = sp.insert_tuple(b"a" * 2000)
    sp.insert_tuple(b"b" * 1900)
    assert first is not None
    sp.delete_tuple(first)
    assert sp.free_space < 1500  # not contiguous until compaction
    slot = sp.insert_tuple(b"c" * 1500)
    assert slot == 2
    assert sp.get_tuple(1) == b"b" * 1900
    assert sp.get_tuple(2) == b"c" * 1500


def test_page_survives_serialization_via_buffer() -> None:
    sp = fresh(3)
    sp.insert_tuple(b"keep")
    page.seal(sp.buf)
    reopened = SlottedPage(bytearray(sp.buf))
    page.verify(reopened.buf, 3)
    assert reopened.get_tuple(0) == b"keep"
