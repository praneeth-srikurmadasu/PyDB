"""Row serialization.

A row is stored as::

    [null bitmap][fixed-width columns][var-length offsets][var-length data]

* null bitmap: ceil(n / 8) bytes; bit ``i`` (LSB first) set means column ``i`` is NULL.
* fixed-width columns: every INT, FLOAT and BOOL column in schema order, at a position that
  depends only on the schema. A NULL column still occupies its slot (zero bytes), so column
  offsets never depend on the data.
* var-length offsets: one u16 per TEXT column, the offset of its data from the start of the
  row (0 when the value is NULL).
* var-length data: for each non-NULL TEXT column, ``[u16 length][UTF-8 bytes]``.

All integers are little-endian.
"""

import struct
from collections.abc import Sequence

from pydb.errors import CorruptPageError, TypeMismatchError
from pydb.types import FIXED_FORMATS, DataType, Value, check_value, encode_text

Row = tuple[Value, ...]

_U16 = struct.Struct("<H")


class RowLayout:
    """Serializes rows of one schema. Column offsets are computed once, here."""

    def __init__(self, types: Sequence[DataType]) -> None:
        if not types:
            raise TypeMismatchError("a row needs at least one column")
        self.types: tuple[DataType, ...] = tuple(types)
        self.bitmap_size = (len(self.types) + 7) // 8

        self._fixed: list[tuple[int, int, struct.Struct]] = []  # (column, offset, format)
        self._var: list[tuple[int, int]] = []  # (column, offset of its u16 pointer)
        offset = self.bitmap_size
        for column, data_type in enumerate(self.types):
            fmt = FIXED_FORMATS.get(data_type)
            if fmt is not None:
                self._fixed.append((column, offset, fmt))
                offset += fmt.size
        for column, data_type in enumerate(self.types):
            if data_type is DataType.TEXT:
                self._var.append((column, offset))
                offset += _U16.size
        self.min_size = offset
        """Size of a row whose TEXT columns are all NULL; the var-length data starts here."""

    def serialize(self, row: Sequence[Value]) -> bytes:
        if len(row) != len(self.types):
            raise TypeMismatchError(f"expected {len(self.types)} values, got {len(row)}")
        values = [check_value(t, v) for t, v in zip(self.types, row, strict=True)]

        buf = bytearray(self.min_size)
        for column, value in enumerate(values):
            if value is None:
                buf[column // 8] |= 1 << (column % 8)
        for column, offset, fmt in self._fixed:
            value = values[column]
            if value is not None:
                fmt.pack_into(buf, offset, value)
        for column, pointer in self._var:
            value = values[column]
            if value is None:
                continue
            assert isinstance(value, str)
            data = encode_text(value)
            _U16.pack_into(buf, pointer, len(buf))
            buf += _U16.pack(len(data))
            buf += data
        return bytes(buf)

    def deserialize(self, data: bytes | bytearray | memoryview) -> Row:
        if len(data) < self.min_size:
            raise CorruptPageError(f"row is {len(data)} bytes, shorter than {self.min_size}")
        values: list[Value] = [None] * len(self.types)
        for column, offset, fmt in self._fixed:
            if not self._is_null(data, column):
                values[column] = fmt.unpack_from(data, offset)[0]
        for column, pointer in self._var:
            if self._is_null(data, column):
                continue
            (start,) = _U16.unpack_from(data, pointer)
            if start + _U16.size > len(data):
                raise CorruptPageError(f"TEXT column {column} points outside the row")
            (length,) = _U16.unpack_from(data, start)
            end = start + _U16.size + length
            if end > len(data):
                raise CorruptPageError(f"TEXT column {column} runs past the end of the row")
            try:
                values[column] = bytes(data[start + _U16.size : end]).decode("utf-8")
            except UnicodeDecodeError:
                raise CorruptPageError(f"TEXT column {column} is not valid UTF-8") from None
        return tuple(values)

    @staticmethod
    def _is_null(data: bytes | bytearray | memoryview, column: int) -> bool:
        return bool(data[column // 8] & (1 << (column % 8)))
