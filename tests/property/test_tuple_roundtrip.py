"""deserialize(serialize(row)) == row for random schemas and rows."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pydb.storage.tuple import RowLayout
from pydb.types import INT_MAX, INT_MIN, DataType, Value

_VALUES: dict[DataType, st.SearchStrategy[Value]] = {
    DataType.INT: st.integers(INT_MIN, INT_MAX),
    DataType.FLOAT: st.floats(allow_nan=False),
    DataType.BOOL: st.booleans(),
    DataType.TEXT: st.text(max_size=64),
}


@st.composite
def schema_and_row(draw: st.DrawFn) -> tuple[list[DataType], tuple[Value, ...]]:
    types = draw(st.lists(st.sampled_from(list(DataType)), min_size=1, max_size=12))
    row = tuple(draw(st.none() | _VALUES[t]) for t in types)
    return types, row


# The first st.text() draw in a process builds Hypothesis's Unicode table (about 1 s on a cold
# cache, as in CI). That one-off cost trips the too_slow health check; generation is otherwise fast.
_settings = settings(suppress_health_check=[HealthCheck.too_slow])


@_settings
@given(schema_and_row())
def test_round_trip(case: tuple[list[DataType], tuple[Value, ...]]) -> None:
    types, row = case
    layout = RowLayout(types)
    data = layout.serialize(row)
    assert layout.deserialize(data) == row


@_settings
@given(schema_and_row())
def test_size_is_min_size_plus_text_payloads(
    case: tuple[list[DataType], tuple[Value, ...]],
) -> None:
    types, row = case
    layout = RowLayout(types)
    text_bytes = sum(2 + len(v.encode()) for v in row if isinstance(v, str))
    assert len(layout.serialize(row)) == layout.min_size + text_bytes
