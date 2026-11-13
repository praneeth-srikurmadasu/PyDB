import math

import pytest

from pydb.errors import CorruptPageError, TypeMismatchError, ValueTooLargeError
from pydb.index.key_codec import decode_key, encode_key, max_key_size
from pydb.types import DataType

INT, FLOAT, BOOL, TEXT = DataType.INT, DataType.FLOAT, DataType.BOOL, DataType.TEXT


def test_int_encoding_flips_the_sign_bit() -> None:
    assert encode_key([INT], [0]) == b"\x01\x80\x00\x00\x00\x00\x00\x00\x00"
    assert encode_key([INT], [-1]) == b"\x01\x7f\xff\xff\xff\xff\xff\xff\xff"
    assert encode_key([INT], [-1]) < encode_key([INT], [0]) < encode_key([INT], [1])


def test_null_sorts_first_for_every_type() -> None:
    for data_type, low in ((INT, -(2**63)), (FLOAT, -math.inf), (BOOL, False), (TEXT, "")):
        assert encode_key([data_type], [None]) == b"\x00"
        assert encode_key([data_type], [None]) < encode_key([data_type], [low])


def test_float_order_including_infinities_and_nan() -> None:
    values = [-math.inf, -1e300, -1.5, -1e-300, 0.0, 1e-300, 2.0, 1e300, math.inf, math.nan]
    encoded = [encode_key([FLOAT], [v]) for v in values]
    assert encoded == sorted(encoded)
    assert len(set(encoded)) == len(encoded)


def test_negative_zero_equals_zero() -> None:
    assert encode_key([FLOAT], [-0.0]) == encode_key([FLOAT], [0.0])


def test_bool_order() -> None:
    assert encode_key([BOOL], [False]) < encode_key([BOOL], [True])


def test_text_escapes_zero_bytes_and_terminates() -> None:
    assert encode_key([TEXT], ["ab"]) == b"\x01ab\x00"
    assert encode_key([TEXT], ["a\x00b"]) == b"\x01a\x00\xffb\x00"
    ordered = ["", "a", "ab", "ab\x00", "ab\x00\x00", "abc", "b"]
    encoded = [encode_key([TEXT], [s]) for s in ordered]
    assert encoded == sorted(encoded)


def test_composite_keys_compare_column_by_column() -> None:
    rows = [("a", 5), ("a", 10), ("ab", -3), ("b", None), ("b", 0)]
    encoded = [encode_key([TEXT, INT], list(r)) for r in rows]
    assert encoded == sorted(encoded)


def test_prefix_key_is_a_byte_prefix() -> None:
    full = encode_key([INT, TEXT], [7, "x"])
    assert full.startswith(encode_key([INT, TEXT], [7]))


def test_decode_round_trip_ignores_trailing_bytes() -> None:
    types = [INT, FLOAT, BOOL, TEXT, TEXT]
    values = (-42, 2.5, True, "h\x00i", None)
    assert decode_key(types, encode_key(types, values) + b"rid!!!") == values


def test_long_text_is_rejected_in_keys() -> None:
    assert encode_key([TEXT], ["x" * 64])
    with pytest.raises(ValueTooLargeError, match="at most 64"):
        encode_key([TEXT], ["x" * 65])


def test_too_many_values_and_wrong_types() -> None:
    with pytest.raises(TypeMismatchError):
        encode_key([INT], [1, 2])
    with pytest.raises(TypeMismatchError):
        encode_key([INT], ["1"])


def test_corrupt_keys_are_detected() -> None:
    with pytest.raises(CorruptPageError, match="marker"):
        decode_key([INT], b"\x07" + bytes(8))
    with pytest.raises(CorruptPageError, match="unterminated"):
        decode_key([TEXT], b"\x01abc")
    with pytest.raises(CorruptPageError, match="ends before"):
        decode_key([INT, INT], encode_key([INT], [1]))


def test_max_key_size_bounds_every_encoding() -> None:
    types = [INT, FLOAT, BOOL, TEXT]
    worst = encode_key(types, [1, 1.0, True, "\x00" * 64])
    assert len(worst) == max_key_size(types)
