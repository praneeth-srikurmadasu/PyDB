import math

import pytest

from pydb.errors import CorruptPageError, TypeMismatchError, ValueTooLargeError
from pydb.storage.tuple import RowLayout
from pydb.types import DataType

INT, FLOAT, BOOL, TEXT = DataType.INT, DataType.FLOAT, DataType.BOOL, DataType.TEXT


def test_byte_layout_of_a_small_row() -> None:
    layout = RowLayout([INT, TEXT, BOOL])
    data = layout.serialize((7, "hi", None))
    assert data == bytes(
        [
            0b100,  # null bitmap: column 2 is NULL
            7, 0, 0, 0, 0, 0, 0, 0,  # INT 7
            0,  # BOOL slot, NULL
            12, 0,  # TEXT offset
            2, 0, ord("h"), ord("i"),  # [length][bytes]
        ]
    )  # fmt: skip
    assert layout.deserialize(data) == (7, "hi", None)


def test_fixed_columns_come_before_text_regardless_of_schema_order() -> None:
    layout = RowLayout([TEXT, INT])
    data = layout.serialize(("x", 1))
    assert data[1:9] == (1).to_bytes(8, "little")
    assert layout.min_size == 1 + 8 + 2


def test_all_null_row_has_minimum_size() -> None:
    layout = RowLayout([INT, FLOAT, BOOL, TEXT, TEXT])
    data = layout.serialize((None,) * 5)
    assert len(data) == layout.min_size
    assert data[0] == 0b11111
    assert layout.deserialize(data) == (None,) * 5


def test_null_bitmap_spans_multiple_bytes() -> None:
    layout = RowLayout([BOOL] * 10)
    row = tuple(None if i in (0, 8, 9) else (i % 2 == 0) for i in range(10))
    data = layout.serialize(row)
    assert layout.bitmap_size == 2
    assert data[0] == 0b00000001
    assert data[1] == 0b00000011
    assert layout.deserialize(data) == row


def test_empty_string_is_not_null() -> None:
    layout = RowLayout([TEXT])
    assert layout.deserialize(layout.serialize(("",))) == ("",)


def test_float_values_round_trip_including_specials() -> None:
    layout = RowLayout([FLOAT, FLOAT, FLOAT])
    out = layout.deserialize(layout.serialize((-0.0, math.inf, math.nan)))
    first, second, third = out
    assert isinstance(first, float) and math.copysign(1.0, first) == -1.0
    assert second == math.inf
    assert isinstance(third, float) and math.isnan(third)


def test_int_is_stored_as_float_in_a_float_column() -> None:
    layout = RowLayout([FLOAT])
    assert layout.deserialize(layout.serialize((2,))) == (2.0,)


def test_wrong_arity_is_rejected() -> None:
    with pytest.raises(TypeMismatchError, match="expected 2 values"):
        RowLayout([INT, INT]).serialize((1,))


def test_type_errors_propagate() -> None:
    layout = RowLayout([INT, TEXT])
    with pytest.raises(TypeMismatchError):
        layout.serialize(("1", "a"))
    with pytest.raises(ValueTooLargeError):
        layout.serialize((1, "x" * 2000))


def test_schema_needs_a_column() -> None:
    with pytest.raises(TypeMismatchError):
        RowLayout([])


def test_truncated_row_is_corrupt() -> None:
    layout = RowLayout([INT, TEXT])
    data = layout.serialize((1, "hello"))
    with pytest.raises(CorruptPageError):
        layout.deserialize(data[:5])
    with pytest.raises(CorruptPageError, match="past the end"):
        layout.deserialize(data[:-1])


def test_bad_text_pointer_is_corrupt() -> None:
    layout = RowLayout([TEXT])
    data = bytearray(layout.serialize(("hello",)))
    data[1:3] = (500).to_bytes(2, "little")
    with pytest.raises(CorruptPageError, match="outside the row"):
        layout.deserialize(bytes(data))


def test_invalid_utf8_is_corrupt() -> None:
    layout = RowLayout([TEXT])
    data = bytearray(layout.serialize(("ab",)))
    data[-1] = 0xFF
    with pytest.raises(CorruptPageError, match="UTF-8"):
        layout.deserialize(bytes(data))
