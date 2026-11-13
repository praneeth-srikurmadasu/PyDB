"""Byte order of encoded keys equals value order (NULL first), and decoding inverts encoding."""

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pydb.index.key_codec import decode_key, encode_key
from pydb.types import INT_MAX, INT_MIN, DataType, Value

_VALUES: dict[DataType, st.SearchStrategy[Value]] = {
    DataType.INT: st.integers(INT_MIN, INT_MAX),
    DataType.FLOAT: st.floats(allow_nan=False).map(lambda f: f + 0.0),  # normalise -0.0
    DataType.BOOL: st.booleans(),
    DataType.TEXT: st.text(max_size=16).filter(lambda s: len(s.encode()) <= 64),
}

# st.text() builds Hypothesis's Unicode table on first use (about 1 s on a cold cache).
_settings = settings(suppress_health_check=[HealthCheck.too_slow])


def sort_key(values: tuple[Value, ...]) -> tuple[tuple[int, Value], ...]:
    """Python ordering with NULL below every value."""
    return tuple((0, 0) if v is None else (1, v) for v in values)


@st.composite
def key_pair(draw: st.DrawFn) -> tuple[list[DataType], tuple[Value, ...], tuple[Value, ...]]:
    types = draw(st.lists(st.sampled_from(list(DataType)), min_size=1, max_size=4))
    a = tuple(draw(st.none() | _VALUES[t]) for t in types)
    b = tuple(draw(st.none() | _VALUES[t]) for t in types)
    return types, a, b


def _cmp(x: Any, y: Any) -> int:
    return int(x > y) - int(x < y)


@_settings
@given(key_pair())
def test_encoded_order_matches_value_order(
    case: tuple[list[DataType], tuple[Value, ...], tuple[Value, ...]],
) -> None:
    types, a, b = case
    assert _cmp(encode_key(types, a), encode_key(types, b)) == _cmp(sort_key(a), sort_key(b))


@_settings
@given(key_pair())
def test_decode_inverts_encode(
    case: tuple[list[DataType], tuple[Value, ...], tuple[Value, ...]],
) -> None:
    types, a, _ = case
    assert decode_key(types, encode_key(types, a)) == a
