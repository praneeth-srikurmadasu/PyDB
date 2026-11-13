"""The external sort gives the same order as Python's stable sort, whatever the memory budget."""

import functools
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from pydb.execution.operators.sort import Sort, TopN, compare_rows
from pydb.execution.operators.values import Values
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.storage.tuple import Row
from pydb.types import DataType

_rows = st.lists(
    st.tuples(
        st.none() | st.integers(-5, 5),
        st.none() | st.sampled_from(["a", "b", "c", ""]),
        st.integers(0, 10**6),  # an identity column, to observe stability
    ),
    max_size=120,
)
_keys = st.lists(st.tuples(st.integers(0, 1), st.booleans()), min_size=1, max_size=3)


@settings(deadline=None)
@given(rows=_rows, keys=_keys, memory=st.sampled_from([64, 512, 4096, 1 << 20]))
def test_external_sort_matches_stable_sort(
    rows: list[Row], keys: list[tuple[int, bool]], memory: int
) -> None:
    expected = sorted(rows, key=functools.cmp_to_key(functools.partial(compare_rows, keys)))
    with tempfile.TemporaryDirectory() as tmp:
        disk = DiskManager(Path(tmp) / "sort.db")
        try:
            pool = BufferPoolManager(disk, 4)
            types = [DataType.INT, DataType.TEXT, DataType.INT]
            op = Sort(Values(rows), keys, types, pool, memory)
            assert list(op.rows()) == expected
            assert pool.pinned_pages() == {}
        finally:
            disk.close()


@given(rows=_rows, keys=_keys, count=st.integers(0, 30))
def test_top_n_is_a_prefix_of_the_sort(
    rows: list[Row], keys: list[tuple[int, bool]], count: int
) -> None:
    expected = sorted(rows, key=functools.cmp_to_key(functools.partial(compare_rows, keys)))
    assert list(TopN(Values(rows), keys, count).rows()) == expected[:count]
