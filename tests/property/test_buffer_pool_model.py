"""Random buffer-pool workloads under memory pressure, checked against a dict of page bytes."""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pydb.storage import page
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager

POOL_SIZE = 3
BODY = page.HEADER_SIZE


class BufferPoolModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "pool.db"
        self.disk = DiskManager(self.path)
        self.pool = BufferPoolManager(self.disk, POOL_SIZE)
        self.contents: dict[int, bytes] = {}  # page id -> first 16 body bytes
        self.held: list[int] = []  # pins held across steps

    @rule()
    def new_page(self) -> None:
        with self.pool.create_page() as frame:
            self.contents[frame.page_id] = bytes(16)

    @precondition(lambda self: self.contents)
    @rule(choice=st.data(), payload=st.binary(min_size=16, max_size=16))
    def write(self, choice: st.DataObject, payload: bytes) -> None:
        pid = choice.draw(st.sampled_from(sorted(self.contents)))
        with self.pool.page(pid, write=True) as frame:
            frame.data[BODY : BODY + 16] = payload
        self.contents[pid] = payload

    @precondition(lambda self: self.contents and len(self.held) < POOL_SIZE - 1)
    @rule(choice=st.data())
    def hold_pin(self, choice: st.DataObject) -> None:
        pid = choice.draw(st.sampled_from(sorted(self.contents)))
        self.pool.fetch_page(pid)
        self.held.append(pid)

    @precondition(lambda self: self.held)
    @rule()
    def release_pin(self) -> None:
        self.pool.unpin_page(self.held.pop(), dirty=False)

    @precondition(lambda self: self.contents)
    @rule(choice=st.data())
    def flush(self, choice: st.DataObject) -> None:
        pid = choice.draw(st.sampled_from(sorted(self.contents)))
        self.pool.flush_page(pid)

    @rule()
    def restart(self) -> None:
        for pid in self.held:
            self.pool.unpin_page(pid, dirty=False)
        self.held.clear()
        self.pool.flush_all()
        self.disk.close()
        self.disk = DiskManager(self.path)
        self.pool = BufferPoolManager(self.disk, POOL_SIZE)

    @invariant()
    def pages_match_and_pins_are_accounted_for(self) -> None:
        expected_pins: dict[int, int] = {}
        for pid in self.held:
            expected_pins[pid] = expected_pins.get(pid, 0) + 1
        assert self.pool.pinned_pages() == expected_pins
        for pid in self.held:  # held pages must still be resident
            assert self.pool.is_resident(pid)
        for pid, payload in self.contents.items():
            with self.pool.page(pid) as frame:
                assert bytes(frame.data[BODY : BODY + 16]) == payload

    def teardown(self) -> None:
        self.disk.close()
        self._tmp.cleanup()


BufferPoolModel.TestCase.settings = settings(max_examples=60, stateful_step_count=40, deadline=None)
TestBufferPoolModel = BufferPoolModel.TestCase
