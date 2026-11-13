import math

import pytest

from pydb.config import MAX_TEXT_BYTES
from pydb.errors import TypeMismatchError, ValueTooLargeError
from pydb.types import INT_MAX, INT_MIN, DataType, check_value


@pytest.mark.parametrize("data_type", list(DataType))
def test_null_is_valid_for_every_type(data_type: DataType) -> None:
    assert check_value(data_type, None) is None


def test_int_accepts_the_full_64_bit_range() -> None:
    assert check_value(DataType.INT, INT_MIN) == INT_MIN
    assert check_value(DataType.INT, INT_MAX) == INT_MAX


@pytest.mark.parametrize("value", [INT_MAX + 1, INT_MIN - 1])
def test_int_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueTooLargeError):
        check_value(DataType.INT, value)


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_int_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeMismatchError, match="expected INT"):
        check_value(DataType.INT, value)  # type: ignore[arg-type]


def test_float_accepts_int_and_returns_float() -> None:
    result = check_value(DataType.FLOAT, 3)
    assert result == 3.0
    assert isinstance(result, float)


def test_float_keeps_special_values() -> None:
    assert check_value(DataType.FLOAT, math.inf) == math.inf
    result = check_value(DataType.FLOAT, math.nan)
    assert isinstance(result, float)
    assert math.isnan(result)


@pytest.mark.parametrize("value", [False, "1.0"])
def test_float_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeMismatchError, match="expected FLOAT"):
        check_value(DataType.FLOAT, value)  # type: ignore[arg-type]


def test_bool_accepts_only_bool() -> None:
    assert check_value(DataType.BOOL, True) is True
    with pytest.raises(TypeMismatchError, match="expected BOOL"):
        check_value(DataType.BOOL, 1)


def test_text_limit_counts_utf8_bytes_not_characters() -> None:
    assert check_value(DataType.TEXT, "a" * MAX_TEXT_BYTES) == "a" * MAX_TEXT_BYTES
    three_byte_char = "€"  # euro sign, 3 bytes in UTF-8
    fits = three_byte_char * (MAX_TEXT_BYTES // 3)
    assert check_value(DataType.TEXT, fits) == fits
    with pytest.raises(ValueTooLargeError, match="bytes in UTF-8"):
        check_value(DataType.TEXT, fits + three_byte_char)


def test_text_rejects_lone_surrogates_and_non_strings() -> None:
    with pytest.raises(TypeMismatchError, match="not valid Unicode"):
        check_value(DataType.TEXT, "\ud800")
    with pytest.raises(TypeMismatchError, match="expected TEXT"):
        check_value(DataType.TEXT, 5)
