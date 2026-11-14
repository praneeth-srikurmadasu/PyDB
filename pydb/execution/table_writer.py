"""Row writes that keep constraints and indexes correct.

Every insert, update and delete goes through a ``TableWriter``. For each row it:

1. validates every column (type, VARCHAR length, NOT NULL);
2. checks every unique index for a conflicting key, and that every index can encode the key;
3. only then changes the heap and **every** index;
4. records the completed operation in the journal (the write-ahead log), which is what
   makes it undoable.

An UPDATE is a delete of the old row followed by an insert of the new one, so its undo is
two simple steps. A delete *reserves* the row's slot rather than freeing it; the space is
freed when the transaction commits, and until then the delete can always be undone in
place. Statement and transaction rollback are done by the transaction manager from the
journal.
"""

from collections.abc import Sequence
from typing import Protocol

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.errors import UniqueViolationError
from pydb.index import key_codec
from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row
from pydb.types import Value


class Journal(Protocol):
    def log_insert(self, table: TableInfo, rid: RID, row: bytes) -> None: ...

    def log_delete(self, table: TableInfo, rid: RID, row: bytes) -> None: ...


class TableWriter:
    def __init__(self, table: TableInfo, journal: Journal | None = None) -> None:
        self.table = table
        self.journal = journal

    def prepare(self, values: Sequence[Value]) -> Row:
        """Validate a full row of values and return it in stored form."""
        return tuple(
            column.validate(value) for column, value in zip(self.table.columns, values, strict=True)
        )

    def insert(self, row: Row) -> RID:
        self._check_indexes(row, ignore=None)
        return self._insert(row)

    def delete(self, rid: RID, row: Row) -> None:
        for index in self.table.indexes:
            index.tree.delete(index.key_of(row), rid)
        self.table.heap.delete(rid, reserve=self.journal is not None)
        if self.journal is not None:
            self.journal.log_delete(self.table, rid, self.table.layout.serialize(row))

    def update(self, rid: RID, old: Row, new: Row) -> RID:
        self._check_indexes(new, ignore=rid)
        self.delete(rid, old)
        return self._insert(new)

    def _insert(self, row: Row) -> RID:
        data = self.table.layout.serialize(row)
        rid = self.table.heap.insert(data)
        for index in self.table.indexes:
            index.tree.insert(index.key_of(row), rid)
        if self.journal is not None:
            self.journal.log_insert(self.table, rid, data)
        return rid

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


def _duplicate_message(table: TableInfo, index: IndexInfo, key: tuple[Value, ...]) -> str:
    columns = ", ".join(table.columns[i].name for i in index.columns)
    values = ", ".join(repr(v) for v in key)
    return (
        f'duplicate key value violates unique constraint "{index.name}": '
        f"({columns})=({values}) already exists"
    )
