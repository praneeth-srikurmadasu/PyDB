"""Random sequences of DiskManager operations, checked against a plain-Python model."""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
)

from pydb.config import PAGE_SIZE
from pydb.storage import page
from pydb.storage.disk_manager import DiskManager
from pydb.storage.page import PageType


class DiskManagerModel(RuleBasedStateMachine):
    """The model: a dict of live page id -> payload, and a stack of freed ids."""

    live = Bundle("live")

    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "model.db"
        self.dm = DiskManager(self.path)
        self.pages: dict[int, bytes] = {}
        self.free: list[int] = []
        self.high_water = 1

    @rule(target=live)
    def allocate(self) -> int:
        pid = self.dm.allocate_page()
        if self.free:
            assert pid == self.free.pop()
        else:
            assert pid == self.high_water
            self.high_water += 1
        assert pid not in self.pages
        self.pages[pid] = b""
        return pid

    @rule(pid=live, payload=st.binary(max_size=64))
    def write(self, pid: int, payload: bytes) -> None:
        buf = page.new_page(pid, PageType.EMPTY)
        buf[page.HEADER_SIZE : page.HEADER_SIZE + len(payload)] = payload
        self.dm.write_page(pid, buf)
        self.pages[pid] = payload

    @rule(pid=consumes(live))
    def free_page(self, pid: int) -> None:
        self.dm.deallocate_page(pid)
        del self.pages[pid]
        self.free.append(pid)

    @rule()
    def reopen(self) -> None:
        self.dm.close()
        self.dm = DiskManager(self.path)

    @invariant()
    def matches_model(self) -> None:
        assert self.dm.num_pages == self.high_water
        assert self.dm.free_page_count == len(self.free)
        for pid, payload in self.pages.items():
            data = self.dm.read_page(pid)
            body = data[page.HEADER_SIZE :]
            assert body[: len(payload)] == payload
            assert body[len(payload) :] == bytes(PAGE_SIZE - page.HEADER_SIZE - len(payload))

    def teardown(self) -> None:
        self.dm.close()
        self._tmp.cleanup()


DiskManagerModel.TestCase.settings = settings(stateful_step_count=30)
TestDiskManagerModel = DiskManagerModel.TestCase
