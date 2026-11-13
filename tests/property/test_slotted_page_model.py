"""Random slotted-page operations against a dict of slot id -> bytes."""

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pydb.config import PAGE_SIZE
from pydb.storage import slotted_page
from pydb.storage.slotted_page import SLOT_ARRAY_OFFSET, SLOT_SIZE, SlottedPage

_payloads = st.binary(max_size=600)


class SlottedPageModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.sp = SlottedPage(slotted_page.format_page(1))
        self.rows: dict[int, bytes] = {}
        self.next_slot = 0

    @rule(data=_payloads)
    def insert(self, data: bytes) -> None:
        fits = self.sp.reclaimable_space() >= len(data) + SLOT_SIZE
        slot = self.sp.insert_tuple(data)
        if fits:
            assert slot == self.next_slot
            self.rows[self.next_slot] = data
            self.next_slot += 1
        else:
            assert slot is None

    @precondition(lambda self: self.rows)
    @rule(data=st.data(), new=_payloads)
    def update(self, data: st.DataObject, new: bytes) -> None:
        slot = data.draw(st.sampled_from(sorted(self.rows)))
        fits = self.sp.reclaimable_space() + len(self.rows[slot]) >= len(new)
        assert self.sp.update_tuple(slot, new) == fits
        if fits:
            self.rows[slot] = new

    @precondition(lambda self: self.rows)
    @rule(data=st.data())
    def delete(self, data: st.DataObject) -> None:
        slot = data.draw(st.sampled_from(sorted(self.rows)))
        self.sp.delete_tuple(slot)
        del self.rows[slot]

    @rule()
    def compact(self) -> None:
        self.sp.compact()
        assert self.sp.free_space == self.sp.reclaimable_space()

    @invariant()
    def matches_model(self) -> None:
        assert list(self.sp.live_slots()) == sorted(self.rows)
        for slot, data in self.rows.items():
            assert self.sp.get_tuple(slot) == data
        assert self.sp.slot_count == self.next_slot
        assert SLOT_ARRAY_OFFSET + self.sp.slot_count * SLOT_SIZE <= self.sp.free_ptr
        assert self.sp.free_ptr <= PAGE_SIZE


SlottedPageModel.TestCase.settings = settings(max_examples=100, stateful_step_count=40)
TestSlottedPageModel = SlottedPageModel.TestCase
