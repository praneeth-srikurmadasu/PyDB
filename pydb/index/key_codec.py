"""Order-preserving key encoding.

``encode_key(types, values)`` produces bytes such that comparing two encodings with plain
byte comparison gives the same result as comparing the value tuples, with NULL lowest. The
B+ tree therefore compares raw bytes and never needs to know column types.

Each column is a marker byte followed by its payload::

    NULL   00
    INT    01 + (value + 2**63) as 8 bytes big-endian       (flips the sign bit)
    FLOAT  01 + IEEE 754 big-endian; negative: all bits inverted, else sign bit flipped
    BOOL   01 + 00 or 01
    TEXT   01 + UTF-8 with each 00 byte written as 00 FF, then a 00 terminator

Every column encoding is self-delimiting, so composite keys are plain concatenations and a
key for the leading columns is a byte prefix of the full key.
"""

import math
import struct
from collections.abc import Sequence

from pydb.config import MAX_KEY_TEXT_BYTES
from pydb.errors import CorruptPageError, TypeMismatchError, ValueTooLargeError
from pydb.types import DataType, Value, check_value, encode_text

NULL_MARKER = 0x00
VALUE_MARKER = 0x01

_U64 = struct.Struct(">Q")
_F64 = struct.Struct(">d")
_SIGN = 1 << 63
_CANONICAL_NAN = float("nan")


def encoded_size_limit(data_type: DataType) -> int:
    """Largest number of bytes one column of this type can take in a key."""
    match data_type:
        case DataType.INT | DataType.FLOAT:
            return 1 + 8
        case DataType.BOOL:
            return 1 + 1
        case DataType.TEXT:
            return 1 + 2 * MAX_KEY_TEXT_BYTES + 1


def max_key_size(types: Sequence[DataType]) -> int:
    return sum(encoded_size_limit(t) for t in types)


def encode_key(types: Sequence[DataType], values: Sequence[Value]) -> bytes:
    """Encode a full key, or a prefix of one when ``values`` is shorter than ``types``."""
    if len(values) > len(types):
        raise TypeMismatchError(f"key has {len(values)} values but the index has {len(types)}")
    out = bytearray()
    for data_type, value in zip(types, values, strict=False):
        _encode_value(out, data_type, check_value(data_type, value))
    return bytes(out)


def decode_key(types: Sequence[DataType], data: bytes) -> tuple[Value, ...]:
    """Decode the columns of a key. Trailing bytes (such as a RID suffix) are ignored."""
    values: list[Value] = []
    pos = 0
    for data_type in types:
        if pos >= len(data):
            raise CorruptPageError("key ends before all of its columns")
        marker = data[pos]
        pos += 1
        if marker == NULL_MARKER:
            values.append(None)
            continue
        if marker != VALUE_MARKER:
            raise CorruptPageError(f"bad key marker byte 0x{marker:02x}")
        value, pos = _decode_value(data, pos, data_type)
        values.append(value)
    return tuple(values)


def _encode_value(out: bytearray, data_type: DataType, value: Value) -> None:
    if value is None:
        out.append(NULL_MARKER)
        return
    out.append(VALUE_MARKER)
    match data_type:
        case DataType.INT:
            assert isinstance(value, int)
            out += _U64.pack(value + _SIGN)
        case DataType.FLOAT:
            assert isinstance(value, float)
            if math.isnan(value):
                value = _CANONICAL_NAN
            elif value == 0.0:
                value = 0.0  # -0.0 and 0.0 are equal, so they must encode the same
            (bits,) = _U64.unpack(_F64.pack(value))
            bits = bits ^ 0xFFFF_FFFF_FFFF_FFFF if bits & _SIGN else bits | _SIGN
            out += _U64.pack(bits)
        case DataType.BOOL:
            out.append(1 if value else 0)
        case DataType.TEXT:
            assert isinstance(value, str)
            raw = encode_text(value)
            if len(raw) > MAX_KEY_TEXT_BYTES:
                raise ValueTooLargeError(
                    f"TEXT key value is {len(raw)} bytes; index keys hold at most "
                    f"{MAX_KEY_TEXT_BYTES} bytes"
                )
            out += raw.replace(b"\x00", b"\x00\xff")
            out.append(0x00)


def _decode_value(data: bytes, pos: int, data_type: DataType) -> tuple[Value, int]:
    match data_type:
        case DataType.INT:
            (raw,) = _U64.unpack_from(data, pos)
            return raw - _SIGN, pos + 8
        case DataType.FLOAT:
            (bits,) = _U64.unpack_from(data, pos)
            bits = bits ^ _SIGN if bits & _SIGN else bits ^ 0xFFFF_FFFF_FFFF_FFFF
            value: float = _F64.unpack(_U64.pack(bits))[0]
            return value, pos + 8
        case DataType.BOOL:
            return data[pos] == 1, pos + 1
        case DataType.TEXT:
            out = bytearray()
            while True:
                if pos >= len(data):
                    raise CorruptPageError("unterminated TEXT in key")
                byte = data[pos]
                if byte != 0x00:
                    out.append(byte)
                    pos += 1
                elif pos + 1 < len(data) and data[pos + 1] == 0xFF:
                    out.append(0x00)
                    pos += 2
                else:
                    return out.decode("utf-8"), pos + 1
