from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.catalog import Catalog
from pydb.catalog.integrity import check_table
from pydb.catalog.schema import Column
from pydb.errors import CatalogError, DuplicateObjectError, UniqueViolationError, UnknownTableError
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.types import DataType

INT, TEXT = DataType.INT, DataType.TEXT


def open_catalog(path: Path) -> tuple[DiskManager, BufferPoolManager, Catalog]:
    disk = DiskManager(path)
    pool = BufferPoolManager(disk, 32)
    return disk, pool, Catalog(pool)


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    disk, pool, cat = open_catalog(tmp_path / "cat.db")
    yield cat
    assert pool.pinned_pages() == {}
    pool.flush_all()
    disk.close()


def test_bootstrap_creates_self_describing_system_tables(catalog: Catalog) -> None:
    names = [t.name for t in catalog.tables(include_system=True)]
    assert names == ["__columns__", "__indexes__", "__stats__", "__tables__"]
    assert catalog.tables() == []
    tables = catalog.get_table("__tables__")
    rows = [tables.layout.deserialize(d) for _, d in tables.heap.scan()]
    assert sorted(str(r[1]) for r in rows) == names
    assert catalog.pool.disk.catalog_root == tables.heap.first_page_id


def test_create_table_with_constraint_indexes(catalog: Catalog) -> None:
    cols = [Column("id", INT, not_null=True), Column("email", TEXT), Column("n", INT)]
    table = catalog.create_table("users", cols, primary_key=[0], unique=[1])
    assert [(i.name, i.columns, i.unique, i.primary) for i in table.indexes] == [
        ("users_pkey", (0,), True, True),
        ("users_email_key", (1,), True, False),
    ]
    assert catalog.get_table("users") is table


def test_catalog_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    disk, pool, cat = open_catalog(path)
    cols = [
        Column("id", INT, not_null=True),
        Column("name", TEXT, length=20, default="anon"),
        Column("score", DataType.FLOAT, default=-1.5),
    ]
    table = cat.create_table("people", cols, primary_key=[0])
    table.heap.insert(table.layout.serialize((1, "x", 0.0)))
    cat.create_index("people_name", "people", [1], unique=False)
    pool.flush_all()
    disk.close()

    disk, pool, cat = open_catalog(path)
    people = cat.get_table("people")
    assert people.columns == tuple(cols)
    assert sorted(i.name for i in people.indexes) == ["people_name", "people_pkey"]
    assert people.table_id == table.table_id
    assert len(list(people.heap.scan())) == 1
    disk.close()


def test_create_index_fills_from_existing_rows(catalog: Catalog) -> None:
    table = catalog.create_table("t", [Column("a", INT), Column("b", TEXT)])
    for i in range(50):
        table.heap.insert(table.layout.serialize((i % 7, f"v{i}")))
    index = catalog.create_index("t_a", "t", [0], unique=False)
    assert len(index.tree.search((3,))) == 7
    assert check_table(table) == 50


def test_unique_index_on_duplicates_fails_and_cleans_up(catalog: Catalog) -> None:
    table = catalog.create_table("t", [Column("a", INT)])
    for value in (1, 2, 2):
        table.heap.insert(table.layout.serialize((value,)))
    free_before = catalog.pool.disk.free_page_count
    with pytest.raises(UniqueViolationError, match="duplicate values"):
        catalog.create_index("t_a", "t", [0], unique=True)
    assert table.indexes == []
    assert catalog.find_index("t_a") is None
    assert catalog.pool.disk.free_page_count > free_before  # tree pages were returned


def test_drop_table_frees_pages_and_rows(catalog: Catalog) -> None:
    table = catalog.create_table("big", [Column("a", INT), Column("pad", TEXT)], primary_key=[0])
    writer_rows = [(i, "x" * 200) for i in range(200)]
    for row in writer_rows:
        rid = table.heap.insert(table.layout.serialize(row))
        table.indexes[0].tree.insert((row[0],), rid)
    free_before = catalog.pool.disk.free_page_count
    catalog.drop_table("big")
    assert not catalog.has_table("big")
    assert catalog.pool.disk.free_page_count > free_before + 10
    with pytest.raises(UnknownTableError):
        catalog.get_table("big")
    columns = catalog.get_table("__columns__")
    assert all(columns.layout.deserialize(d)[0] != table.table_id for _, d in columns.heap.scan())
    catalog.create_table("big", [Column("a", INT)])  # the name is free again


def test_name_rules(catalog: Catalog) -> None:
    catalog.create_table("t", [Column("a", INT)], primary_key=[0])
    with pytest.raises(DuplicateObjectError, match='table "t" already exists'):
        catalog.create_table("t", [Column("a", INT)])
    with pytest.raises(CatalogError, match="reserved for system tables"):
        catalog.create_table("__mine", [Column("a", INT)])
    with pytest.raises(DuplicateObjectError, match="specified more than once"):
        catalog.create_table("u", [Column("a", INT), Column("a", TEXT)])
    with pytest.raises(DuplicateObjectError, match='index "t_pkey" already exists'):
        catalog.create_index("t_pkey", "t", [0], unique=False)
    with pytest.raises(CatalogError, match="cannot drop system table"):
        catalog.drop_table("__tables__")
    with pytest.raises(CatalogError, match="primary key"):
        catalog.drop_index("t_pkey")
    with pytest.raises(CatalogError, match="does not exist"):
        catalog.drop_index("nope")
