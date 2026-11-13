"""Row writes that keep constraints and indexes correct, with statement-level undo.

Every insert, update and delete goes through a ``TableWriter``. For each row it:

1. validates every column (type, VARCHAR length, NOT NULL);
2. checks every unique index for a conflicting key, and that every index can encode the key;
3. only then changes the heap and **every** index.

Each change is recorded in an undo list. If any row of a statement fails, ``rollback()``
reverses the statement's earlier changes in reverse order, so a statement either applies
completely or not at all.
"""

from collections.abc import Callable, Sequence

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.errors import UniqueViolationError
from pydb.index import key_codec
from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row
from pydb.types import Value


class TableWriter:
    def __init__(self, table: TableInfo) -> None:
        self.table = table
        self._undo: list[Callable[[], None]] = []

    def prepare(self, values: Sequence[Value]) -> Row:
        """Validate a full row of values and return it in stored form."""
        return tuple(
            column.validate(value) for column, value in zip(self.table.columns, values, strict=True)
        )

    def insert(self, row: Row) -> RID:
        self._check_indexes(row, ignore=None)
        rid = self.table.heap.insert(self.table.layout.serialize(row))
        self._index_insert(row, rid)
        self._undo.append(lambda: self._raw_delete(rid, row))
        return rid

    def delete(self, rid: RID, row: Row) -> None:
        self._raw_delete(rid, row)
        self._undo.append(lambda: self._raw_restore(row))

    def update(self, rid: RID, old: Row, new: Row) -> RID:
        self._check_indexes(new, ignore=rid)
        self._index_delete(old, rid)
        new_rid = self.table.heap.update(rid, self.table.layout.serialize(new))
        self._index_insert(new, new_rid)
        self._undo.append(lambda: self._raw_revert(new_rid, new, old))
        return new_rid

    def rollback(self) -> None:
        """Undo every change made through this writer, newest first."""
        while self._undo:
            self._undo.pop()()

    # ----------------------------------------------------------------- internals

    def _check_indexes(self, row: Row, ignore: RID | None) -> None:
        for index in self.table.indexes:
            key = index.key_of(row)
            types = [self.table.columns[i].type for i in index.columns]
            key_codec.encode_key(types, key)  # raises if the key cannot be indexed
            if (
                index.unique
                and None not in key
                and any(found != ignore for found in index.tree.search(key))
            ):
                raise UniqueViolationError(_duplicate_message(self.table, index, key))

    def _index_insert(self, row: Row, rid: RID) -> None:
        for index in self.table.indexes:
            index.tree.insert(index.key_of(row), rid)

    def _index_delete(self, row: Row, rid: RID) -> None:
        for index in self.table.indexes:
            index.tree.delete(index.key_of(row), rid)

    def _raw_delete(self, rid: RID, row: Row) -> None:
        self._index_delete(row, rid)
        self.table.heap.delete(rid)

    def _raw_restore(self, row: Row) -> None:
        rid = self.table.heap.insert(self.table.layout.serialize(row))
        self._index_insert(row, rid)

    def _raw_revert(self, rid: RID, current: Row, previous: Row) -> None:
        self._index_delete(current, rid)
        restored = self.table.heap.update(rid, self.table.layout.serialize(previous))
        self._index_insert(previous, restored)


def _duplicate_message(table: TableInfo, index: IndexInfo, key: tuple[Value, ...]) -> str:
    columns = ", ".join(table.columns[i].name for i in index.columns)
    values = ", ".join(repr(v) for v in key)
    return (
        f'duplicate key value violates unique constraint "{index.name}": '
        f"({columns})=({values}) already exists"
    )
