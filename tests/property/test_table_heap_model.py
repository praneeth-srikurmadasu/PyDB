"""Random TableHeap operations, with reopens, against a dict of RID -> bytes."""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pydb.errors import RecordNotFoundError
from pydb.storage.disk_manager import DiskManager
from pydb.storage.table_heap import RID, TableHeap

_payloads = st.binary(min_size=0, max_size=1500)


class TableHeapModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "heap.db"
        self.disk = DiskManager(self.path)
        self.heap = TableHeap.create(self.disk)
        self.rows: dict[RID, bytes] = {}
        self.dead: set[RID] = set()

    @rule(data=_payloads)
    def insert(self, data: bytes) -> None:
        rid = self.heap.insert(data)
        assert rid not in self.rows
        assert rid not in self.dead
        self.rows[rid] = data

    @precondition(lambda self: self.rows)
    @rule(choice=st.data(), data=_payloads)
    def update(self, choice: st.DataObject, data: bytes) -> None:
        rid = choice.draw(st.sampled_from(sorted(self.rows)))
        new_rid = self.heap.update(rid, data)
        del self.rows[rid]
        if new_rid != rid:
            self.dead.add(rid)
        self.rows[new_rid] = data

    @precondition(lambda self: self.rows)
    @rule(choice=st.data())
    def delete(self, choice: st.DataObject) -> None:
        rid = choice.draw(st.sampled_from(sorted(self.rows)))
        self.heap.delete(rid)
        del self.rows[rid]
        self.dead.add(rid)

    @rule()
    def reopen(self) -> None:
        first = self.heap.first_page_id
        self.disk.close()
        self.disk = DiskManager(self.path)
        self.heap = TableHeap(self.disk, first)

    @invariant()
    def matches_model(self) -> None:
        assert dict(self.heap.scan()) == self.rows
        for rid in list(self.dead)[:5]:
            try:
                self.heap.get(rid)
            except RecordNotFoundError:
                continue
            raise AssertionError(f"deleted row {rid} is still readable")

    def teardown(self) -> None:
        self.disk.close()
        self._tmp.cleanup()


TableHeapModel.TestCase.settings = settings(max_examples=40, stateful_step_count=40, deadline=None)
TestTableHeapModel = TableHeapModel.TestCase
