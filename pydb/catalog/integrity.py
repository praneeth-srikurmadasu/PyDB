"""Consistency checks between a table's heap and its indexes."""

from pydb.catalog.schema import TableInfo
from pydb.errors import CorruptPageError


def check_table(table: TableInfo) -> int:
    """Verify every index of ``table`` against its heap and return the row count.

    Each index must pass its own structural checks and contain exactly one entry per heap
    row, with the row's key and RID; no entry may point at a missing row. Raises
    CorruptPageError on the first mismatch.
    """
    rows = {rid: table.layout.deserialize(data) for rid, data in table.heap.scan()}
    for index in table.indexes:
        count = index.tree.check_invariants()
        entries = set(index.tree.range_scan())
        expected = {(index.key_of(row), rid) for rid, row in rows.items()}
        if count != len(rows) or entries != expected:
            missing = expected - entries
            extra = entries - expected
            raise CorruptPageError(
                f'index "{index.name}" disagrees with table "{table.name}": '
                f"{len(missing)} entries missing, {len(extra)} unexpected"
            )
    return len(rows)
