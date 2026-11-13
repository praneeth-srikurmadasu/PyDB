"""Interleaved inserts, deletes, range scans and reopens against a sorted-list reference."""

import tempfile
from bisect import bisect_left, insort
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pydb.errors import DuplicateKeyError
from pydb.index.bplus_tree import BPlusTree
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.storage.table_heap import RID
from pydb.types import DataType

_keys = st.integers(-50, 50)
_rids = st.builds(RID, st.integers(1, 5), st.integers(0, 3))


class _TreeModel(RuleBasedStateMachine):
    unique = True

    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "tree.db"
        self.disk = DiskManager(self.path)
        self.pool = BufferPoolManager(self.disk, 8)
        self.tree = BPlusTree.create(
            self.pool, [DataType.INT], unique=self.unique, leaf_max=4, internal_max=3
        )
        self.entries: list[tuple[int, RID]] = []  # sorted (key, rid): the reference

    def _has_key(self, key: int) -> bool:
        i = bisect_left(self.entries, (key, RID(-1, -1)))
        return i < len(self.entries) and self.entries[i][0] == key

    @rule(key=_keys, rid=_rids)
    def insert(self, key: int, rid: RID) -> None:
        duplicate = (key, rid) in self.entries or (self.unique and self._has_key(key))
        try:
            self.tree.insert((key,), rid)
        except DuplicateKeyError:
            assert duplicate
            return
        assert not duplicate
        insort(self.entries, (key, rid))

    @precondition(lambda self: self.entries)
    @rule(choice=st.data())
    def delete_existing(self, choice: st.DataObject) -> None:
        key, rid = choice.draw(st.sampled_from(self.entries))
        assert self.tree.delete((key,), rid)
        self.entries.remove((key, rid))

    @rule(key=_keys, rid=_rids)
    def delete_any(self, key: int, rid: RID) -> None:
        present = (key, rid) in self.entries
        assert self.tree.delete((key,), rid) == present
        if present:
            self.entries.remove((key, rid))

    @rule(
        low=st.none() | _keys,
        high=st.none() | _keys,
        low_inclusive=st.booleans(),
        high_inclusive=st.booleans(),
    )
    def range_scan(
        self, low: int | None, high: int | None, low_inclusive: bool, high_inclusive: bool
    ) -> None:
        got = [
            (k[0], r)
            for k, r in self.tree.range_scan(
                None if low is None else (low,),
                None if high is None else (high,),
                low_inclusive=low_inclusive,
                high_inclusive=high_inclusive,
            )
        ]
        expected = [
            (k, r)
            for k, r in self.entries
            if (low is None or k > low or (low_inclusive and k == low))
            and (high is None or k < high or (high_inclusive and k == high))
        ]
        assert got == expected

    @rule(key=_keys)
    def search(self, key: int) -> None:
        assert self.tree.search((key,)) == [r for k, r in self.entries if k == key]

    @rule()
    def reopen(self) -> None:
        header = self.tree.header_page_id
        self.pool.flush_all()
        self.disk.close()
        self.disk = DiskManager(self.path)
        self.pool = BufferPoolManager(self.disk, 8)
        self.tree = BPlusTree(self.pool, header)

    @invariant()
    def structure_holds(self) -> None:
        assert self.tree.check_invariants() == len(self.entries)
        assert self.pool.pinned_pages() == {}

    def teardown(self) -> None:
        self.disk.close()
        self._tmp.cleanup()


class UniqueTreeModel(_TreeModel):
    unique = True


class NonUniqueTreeModel(_TreeModel):
    unique = False


_config = settings(stateful_step_count=60, deadline=None)
UniqueTreeModel.TestCase.settings = _config
NonUniqueTreeModel.TestCase.settings = _config
TestUniqueTreeModel = UniqueTreeModel.TestCase
TestNonUniqueTreeModel = NonUniqueTreeModel.TestCase
