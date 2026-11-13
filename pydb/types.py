"""SQL data types and value validation."""

import struct
from enum import IntEnum

from pydb.config import MAX_TEXT_BYTES
from pydb.errors import TypeMismatchError, ValueTooLargeError

Value = int | float | bool | str | None
"""A single SQL value as a Python object. ``None`` is SQL NULL."""

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1


class DataType(IntEnum):
    INT = 1
    FLOAT = 2
    BOOL = 3
    TEXT = 4


FIXED_FORMATS: dict[DataType, struct.Struct] = {
    DataType.INT: struct.Struct("<q"),
    DataType.FLOAT: struct.Struct("<d"),
    DataType.BOOL: struct.Struct("<?"),
}
"""Encoding of each fixed-width type. TEXT is variable-length and has no entry."""


def is_fixed_width(data_type: DataType) -> bool:
    return data_type in FIXED_FORMATS


def check_value(data_type: DataType, value: Value) -> Value:
    """Validate ``value`` for a column of ``data_type`` and return its stored form.

    NULL passes for every type (NOT NULL is enforced by the catalog layer). An int is
    accepted for FLOAT and returned as a float. ``bool`` is not accepted for INT even though
    Python treats it as an int subclass.
    """
    if value is None:
        return None
    match data_type:
        case DataType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise _mismatch(data_type, value)
            if not INT_MIN <= value <= INT_MAX:
                raise ValueTooLargeError(f"INT value {value} is outside the 64-bit range")
            return value
        case DataType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise _mismatch(data_type, value)
            return float(value)
        case DataType.BOOL:
            if not isinstance(value, bool):
                raise _mismatch(data_type, value)
            return value
        case DataType.TEXT:
            if not isinstance(value, str):
                raise _mismatch(data_type, value)
            size = len(encode_text(value))
            if size > MAX_TEXT_BYTES:
                raise ValueTooLargeError(
                    f"TEXT value is {size} bytes in UTF-8; the limit is {MAX_TEXT_BYTES}"
                )
            return value


def encode_text(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TypeMismatchError(f"TEXT value is not valid Unicode: {exc.reason}") from None


def _mismatch(data_type: DataType, value: object) -> TypeMismatchError:
    return TypeMismatchError(f"expected {data_type.name}, got {type(value).__name__} {value!r}")
