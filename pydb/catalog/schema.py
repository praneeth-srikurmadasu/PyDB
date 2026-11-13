"""In-memory descriptions of tables, columns and indexes."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydb.errors import NotNullViolationError, TypeMismatchError, ValueTooLargeError
from pydb.index.bplus_tree import BPlusTree
from pydb.storage.table_heap import TableHeap
from pydb.storage.tuple import Row, RowLayout
from pydb.types import DataType, Value, check_value

if TYPE_CHECKING:
    from pydb.planner.stats import TableStats


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: DataType
    length: int | None = None  # VARCHAR(n): most characters allowed
    not_null: bool = False
    default: Value = None

    def type_sql(self) -> str:
        return f"VARCHAR({self.length})" if self.length is not None else self.type.name

    def validate(self, value: Value) -> Value:
        """Return the stored form of ``value`` or raise a descriptive error."""
        if value is None:
            if self.not_null:
                raise NotNullViolationError(
                    f'null value in column "{self.name}" violates not-null constraint'
                )
            return None
        try:
            stored = check_value(self.type, value)
        except TypeMismatchError as exc:
            raise TypeMismatchError(f'column "{self.name}": {exc}') from None
        except ValueTooLargeError as exc:
            raise ValueTooLargeError(f'column "{self.name}": {exc}') from None
        if self.length is not None and isinstance(stored, str) and len(stored) > self.length:
            raise ValueTooLargeError(
                f'value too long for column "{self.name}" {self.type_sql()} '
                f"({len(stored)} characters)"
            )
        return stored


@dataclass(slots=True)
class IndexInfo:
    index_id: int
    name: str
    table_id: int
    columns: tuple[int, ...]  # column positions in the table
    unique: bool
    primary: bool
    tree: BPlusTree

    def key_of(self, row: Row) -> tuple[Value, ...]:
        return tuple(row[i] for i in self.columns)


@dataclass(slots=True)
class TableInfo:
    table_id: int
    name: str
    columns: tuple[Column, ...]
    heap: TableHeap
    indexes: list[IndexInfo] = field(default_factory=list)
    stats: "TableStats | None" = None
    layout: RowLayout = field(init=False)

    def __post_init__(self) -> None:
        self.layout = RowLayout([c.type for c in self.columns])

    @property
    def is_system(self) -> bool:
        return self.name.startswith("__")

    def column_position(self, name: str) -> int | None:
        for i, column in enumerate(self.columns):
            if column.name == name:
                return i
        return None

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column_types(self) -> list[DataType]:
        return [c.type for c in self.columns]

    def index_on(self, positions: Sequence[int]) -> IndexInfo | None:
        for index in self.indexes:
            if index.columns == tuple(positions):
                return index
        return None
