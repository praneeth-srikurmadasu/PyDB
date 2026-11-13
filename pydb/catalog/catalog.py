"""The catalog: every table, column and index, stored as rows of three system tables.

::

    __tables__  (table_id INT, name TEXT, first_page INT)
    __columns__ (table_id INT, position INT, name TEXT, type INT, length INT,
                 not_null BOOL, default_sql TEXT)
    __indexes__ (index_id INT, name TEXT, table_id INT, header_page INT,
                 is_unique BOOL, is_primary BOOL, columns TEXT)
    __stats__   (table_id INT, position INT, row_count INT, page_count INT,
                 distinct_count INT, null_count INT, min_sql TEXT, max_sql TEXT)

``__stats__`` holds what ``ANALYZE`` measured: one row per column, plus a row with
position -1 for the table's row and page counts. A database created before ``__stats__``
existed gets it added when opened.

The system tables describe themselves too (table ids 1-3). The database header page's
``catalog_root`` is the first page of ``__tables__``; scanning it finds the other two. The
whole catalog is loaded into memory when the database opens and kept in step with the
system tables on every change.
"""

from collections.abc import Callable, Sequence

from pydb.catalog.schema import Column, IndexInfo, TableInfo
from pydb.config import INVALID_PAGE_ID
from pydb.errors import (
    CatalogError,
    CorruptPageError,
    DuplicateKeyError,
    DuplicateObjectError,
    UniqueViolationError,
    UnknownTableError,
)
from pydb.index.bplus_tree import BPlusTree
from pydb.planner.stats import ColumnStats, TableStats
from pydb.sql.ast import Literal
from pydb.sql.parser import parse_expression
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.table_heap import TableHeap
from pydb.storage.tuple import Row
from pydb.types import DataType, Value

INT, TEXT, BOOL = DataType.INT, DataType.TEXT, DataType.BOOL

TABLES_TABLE = "__tables__"
COLUMNS_TABLE = "__columns__"
INDEXES_TABLE = "__indexes__"
STATS_TABLE = "__stats__"

_SYSTEM_SCHEMAS: dict[str, tuple[int, tuple[Column, ...]]] = {
    TABLES_TABLE: (
        1,
        (Column("table_id", INT, not_null=True), Column("name", TEXT, not_null=True),
         Column("first_page", INT, not_null=True)),
    ),
    COLUMNS_TABLE: (
        2,
        (Column("table_id", INT, not_null=True), Column("position", INT, not_null=True),
         Column("name", TEXT, not_null=True), Column("type", INT, not_null=True),
         Column("length", INT), Column("not_null", BOOL, not_null=True),
         Column("default_sql", TEXT)),
    ),
    INDEXES_TABLE: (
        3,
        (Column("index_id", INT, not_null=True), Column("name", TEXT, not_null=True),
         Column("table_id", INT, not_null=True), Column("header_page", INT, not_null=True),
         Column("is_unique", BOOL, not_null=True), Column("is_primary", BOOL, not_null=True),
         Column("columns", TEXT, not_null=True)),
    ),
    STATS_TABLE: (
        4,
        (Column("table_id", INT, not_null=True), Column("position", INT, not_null=True),
         Column("row_count", INT), Column("page_count", INT), Column("distinct_count", INT),
         Column("null_count", INT), Column("min_sql", TEXT), Column("max_sql", TEXT)),
    ),
}  # fmt: skip


class Catalog:
    def __init__(self, pool: BufferPoolManager) -> None:
        self.pool = pool
        self._tables: dict[str, TableInfo] = {}
        if pool.disk.catalog_root == INVALID_PAGE_ID:
            self._bootstrap()
        else:
            self._load(pool.disk.catalog_root)

    # ----------------------------------------------------------------- lookups

    def get_table(self, name: str) -> TableInfo:
        table = self._tables.get(name)
        if table is None:
            raise UnknownTableError(f'table "{name}" does not exist')
        return table

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def tables(self, *, include_system: bool = False) -> list[TableInfo]:
        return sorted(
            (t for t in self._tables.values() if include_system or not t.is_system),
            key=lambda t: t.name,
        )

    def find_index(self, name: str) -> tuple[TableInfo, IndexInfo] | None:
        for table in self._tables.values():
            for index in table.indexes:
                if index.name == name:
                    return table, index
        return None

    # ----------------------------------------------------------------- DDL

    def create_table(
        self,
        name: str,
        columns: Sequence[Column],
        primary_key: Sequence[int] = (),
        unique: Sequence[int] = (),
    ) -> TableInfo:
        """Create a table, its heap and the indexes behind PRIMARY KEY and UNIQUE."""
        if name in self._tables:
            raise DuplicateObjectError(f'table "{name}" already exists')
        if name.startswith("__"):
            raise CatalogError("table names starting with '__' are reserved for system tables")
        names = [c.name for c in columns]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise DuplicateObjectError(f'column "{duplicates[0]}" specified more than once')
        planned = [f"{name}_pkey"] if primary_key else []
        planned += [f"{name}_{columns[p].name}_key" for p in unique]
        for index_name in planned:
            if self.find_index(index_name) is not None:
                raise DuplicateObjectError(f'index "{index_name}" already exists')

        table_id = 1 + max(t.table_id for t in self._tables.values())
        table = TableInfo(table_id, name, tuple(columns), TableHeap.create(self.pool))
        self._insert_system_row(TABLES_TABLE, (table_id, name, table.heap.first_page_id))
        for position, column in enumerate(columns):
            self._insert_system_row(COLUMNS_TABLE, _column_row(table_id, position, column))
        self._tables[name] = table
        try:
            if primary_key:
                self._build_index(table, f"{name}_pkey", tuple(primary_key), True, True)
            for position in unique:
                if table.index_on((position,)) is None:
                    self._build_index(
                        table, f"{name}_{columns[position].name}_key", (position,), True, False
                    )
        except BaseException:
            self.drop_table(name)
            raise
        return table

    def drop_table(self, name: str) -> None:
        table = self.get_table(name)
        if table.is_system:
            raise CatalogError(f'cannot drop system table "{name}"')
        for index in list(table.indexes):
            self._drop_index(table, index)
        self._delete_system_rows(COLUMNS_TABLE, lambda r: r[0] == table.table_id)
        self._delete_system_rows(STATS_TABLE, lambda r: r[0] == table.table_id)
        self._delete_system_rows(TABLES_TABLE, lambda r: r[0] == table.table_id)
        table.heap.drop()
        del self._tables[name]

    def create_index(
        self, name: str, table_name: str, positions: Sequence[int], unique: bool
    ) -> IndexInfo:
        table = self.get_table(table_name)
        if table.is_system:
            raise CatalogError(f'cannot create an index on system table "{table_name}"')
        if self.find_index(name) is not None:
            raise DuplicateObjectError(f'index "{name}" already exists')
        return self._build_index(table, name, tuple(positions), unique, False)

    def drop_index(self, name: str) -> None:
        found = self.find_index(name)
        if found is None:
            raise CatalogError(f'index "{name}" does not exist')
        table, index = found
        if table.is_system:
            raise CatalogError(f'cannot drop index "{name}" of a system table')
        if index.primary:
            raise CatalogError(
                f'cannot drop "{name}": it enforces the primary key of "{table.name}"'
            )
        self._drop_index(table, index)

    def set_stats(self, table: TableInfo, stats: TableStats) -> None:
        """Replace the stored statistics of ``table``."""
        self._delete_system_rows(STATS_TABLE, lambda r: r[0] == table.table_id)
        self._insert_system_row(
            STATS_TABLE,
            (table.table_id, -1, stats.row_count, stats.page_count, None, None, None, None),
        )
        for position, column in enumerate(stats.columns):
            self._insert_system_row(
                STATS_TABLE,
                (table.table_id, position, None, None, column.distinct, column.nulls,
                 _literal_sql(column.min), _literal_sql(column.max)),
            )  # fmt: skip
        table.stats = stats

    # ----------------------------------------------------------------- internals

    def _build_index(
        self, table: TableInfo, name: str, positions: tuple[int, ...], unique: bool, primary: bool
    ) -> IndexInfo:
        """Create the tree, fill it from the table's rows, then record it."""
        types = [table.columns[p].type for p in positions]
        tree = BPlusTree.create(self.pool, types, unique=unique)
        try:
            for rid, data in table.heap.scan():
                row = table.layout.deserialize(data)
                tree.insert(tuple(row[p] for p in positions), rid)
        except DuplicateKeyError:
            tree.drop()
            raise UniqueViolationError(
                f'could not create unique index "{name}": the table has duplicate values'
            ) from None
        except BaseException:
            tree.drop()
            raise
        index_id = 1 + max(
            (i.index_id for t in self._tables.values() for i in t.indexes), default=0
        )
        index = IndexInfo(index_id, name, table.table_id, positions, unique, primary, tree)
        self._insert_system_row(
            INDEXES_TABLE,
            (index_id, name, table.table_id, tree.header_page_id, unique, primary,
             ",".join(map(str, positions))),
        )  # fmt: skip
        table.indexes.append(index)
        return index

    def _drop_index(self, table: TableInfo, index: IndexInfo) -> None:
        self._delete_system_rows(INDEXES_TABLE, lambda r: r[0] == index.index_id)
        index.tree.drop()
        table.indexes.remove(index)

    def _insert_system_row(self, table_name: str, row: Row) -> None:
        table = self._tables[table_name]
        table.heap.insert(table.layout.serialize(row))

    def _delete_system_rows(self, table_name: str, predicate: Callable[[Row], bool]) -> None:
        table = self._tables[table_name]
        doomed = [
            rid for rid, data in table.heap.scan() if predicate(table.layout.deserialize(data))
        ]
        for rid in doomed:
            table.heap.delete(rid)

    def _bootstrap(self) -> None:
        for name, (table_id, columns) in _SYSTEM_SCHEMAS.items():
            self._tables[name] = TableInfo(table_id, name, columns, TableHeap.create(self.pool))
        for name, (table_id, columns) in _SYSTEM_SCHEMAS.items():
            first_page = self._tables[name].heap.first_page_id
            self._insert_system_row(TABLES_TABLE, (table_id, name, first_page))
            for position, column in enumerate(columns):
                self._insert_system_row(COLUMNS_TABLE, _column_row(table_id, position, column))
        self.pool.flush_all()  # the pages must be on disk before the header points at them
        self.pool.disk.set_catalog_root(self._tables[TABLES_TABLE].heap.first_page_id)
        self.pool.disk.sync()

    def _add_system_table(self, name: str) -> None:
        """Create a system table missing from a database made by an older version."""
        columns = _SYSTEM_SCHEMAS[name][1]
        table_id = 1 + max(t.table_id for t in self._tables.values())
        table = TableInfo(table_id, name, columns, TableHeap.create(self.pool))
        self._insert_system_row(TABLES_TABLE, (table_id, name, table.heap.first_page_id))
        for position, column in enumerate(columns):
            self._insert_system_row(COLUMNS_TABLE, _column_row(table_id, position, column))
        self._tables[name] = table

    def _load_stats(self, by_id: dict[int, TableInfo]) -> None:
        stats_table = self._tables[STATS_TABLE]
        table_level: dict[int, tuple[int, int]] = {}
        columns: dict[int, dict[int, ColumnStats]] = {}
        for _, data in stats_table.heap.scan():
            r = stats_table.layout.deserialize(data)
            table_id, position = _int(r[0]), _int(r[1])
            if position < 0:
                table_level[table_id] = (_int(r[2]), _int(r[3]))
            else:
                columns.setdefault(table_id, {})[position] = ColumnStats(
                    _int(r[4]), _int(r[5]), _literal_value(r[6]), _literal_value(r[7])
                )
        for table_id, (rows, pages) in table_level.items():
            table = by_id.get(table_id)
            per_column = columns.get(table_id, {})
            if table is not None and len(per_column) == len(table.columns):
                ordered = tuple(per_column[i] for i in range(len(table.columns)))
                table.stats = TableStats(rows, pages, ordered)

    def _load(self, root: int) -> None:
        tables_schema = _SYSTEM_SCHEMAS[TABLES_TABLE][1]
        bootstrap = TableInfo(1, TABLES_TABLE, tables_schema, TableHeap(self.pool, root))
        table_rows = [bootstrap.layout.deserialize(d) for _, d in bootstrap.heap.scan()]
        first_pages = {str(r[1]): int(_int(r[2])) for r in table_rows}
        for name in (COLUMNS_TABLE, INDEXES_TABLE):
            if name not in first_pages:
                raise CorruptPageError(f"catalog is missing system table {name}")
        heaps = {name: TableHeap(self.pool, page_id) for name, page_id in first_pages.items()}

        columns_layout = TableInfo(
            2, COLUMNS_TABLE, _SYSTEM_SCHEMAS[COLUMNS_TABLE][1], heaps[COLUMNS_TABLE]
        )
        by_table: dict[int, list[tuple[int, Column]]] = {}
        for _, data in heaps[COLUMNS_TABLE].scan():
            r = columns_layout.layout.deserialize(data)
            by_table.setdefault(_int(r[0]), []).append((_int(r[1]), _column_from_row(r)))
        for r in table_rows:
            table_id, name = _int(r[0]), str(r[1])
            columns = tuple(c for _, c in sorted(by_table.get(table_id, [])))
            self._tables[name] = TableInfo(table_id, name, columns, heaps[name])

        if STATS_TABLE not in self._tables:
            self._add_system_table(STATS_TABLE)
        by_id = {t.table_id: t for t in self._tables.values()}
        self._load_stats(by_id)
        indexes_table = self._tables[INDEXES_TABLE]
        for _, data in indexes_table.heap.scan():
            r = indexes_table.layout.deserialize(data)
            positions = tuple(int(p) for p in str(r[6]).split(","))
            index = IndexInfo(
                _int(r[0]), str(r[1]), _int(r[2]), positions, bool(r[4]), bool(r[5]),
                BPlusTree(self.pool, _int(r[3])),
            )  # fmt: skip
            by_id[index.table_id].indexes.append(index)


def _literal_sql(value: Value) -> str | None:
    return None if value is None else Literal(value).to_sql()


def _literal_value(sql: Value) -> Value:
    if sql is None:
        return None
    literal = parse_expression(str(sql))
    if not isinstance(literal, Literal):
        raise CorruptPageError(f"stored value {sql!r} is not a literal")
    return literal.value


def _column_row(table_id: int, position: int, column: Column) -> Row:
    default_sql = None if column.default is None else Literal(column.default).to_sql()
    return (table_id, position, column.name, int(column.type), column.length,
            column.not_null, default_sql)  # fmt: skip


def _column_from_row(r: Row) -> Column:
    default: Value = None
    if r[6] is not None:
        literal = parse_expression(str(r[6]))
        if not isinstance(literal, Literal):
            raise CorruptPageError(f"column default {r[6]!r} is not a literal")
        default = literal.value
    length = None if r[4] is None else _int(r[4])
    return Column(str(r[2]), DataType(_int(r[3])), length, bool(r[5]), default)


def _int(value: Value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CorruptPageError(f"catalog value {value!r} should be an integer")
    return value
