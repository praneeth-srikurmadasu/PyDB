"""A query suite checked against the standard library's sqlite3 as a reference.

The same deterministic data (with NULLs) is loaded into PyDB and into an in-memory SQLite
database. Every query runs on PyDB under each join algorithm and with a sort memory budget
small enough to force external sorting, before and after ANALYZE, and with index use and
join reordering switched off, and must give SQLite's answer. sqlite3 is used only
here, as an oracle; the engine never imports it.

Queries avoid the few places where the two engines intentionally differ (LIKE is
case-insensitive in SQLite; booleans are integers there).
"""

import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.database import Database
from pydb.planner.physical_plan import PlannerSettings
from pydb.storage.tuple import Row

SCHEMA = [
    "CREATE TABLE customers (id INT PRIMARY KEY, name TEXT, city TEXT, tier INT)",
    "CREATE TABLE products (id INT PRIMARY KEY, title TEXT, price FLOAT, category TEXT)",
    "CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT, product_id INT, qty INT, note TEXT)",
]
INDEXES = ["CREATE INDEX orders_customer ON orders (customer_id)"]


def dataset() -> dict[str, list[Row]]:
    rng = random.Random(42)
    cities = ["Pune", "Delhi", "Oslo", None]
    customers: list[Row] = [
        (i, f"cust{i:03d}", rng.choice(cities), rng.choice([1, 2, 3, None])) for i in range(1, 61)
    ]
    categories = ["books", "games", "tools", None]
    products: list[Row] = [
        (
            i,
            f"prod{i:02d}",
            round(rng.uniform(1, 100), 2) if i % 9 else None,
            rng.choice(categories),
        )
        for i in range(1, 26)
    ]
    orders: list[Row] = [
        (
            i,
            rng.choice([*range(1, 70), None]),  # some customers do not exist; some are NULL
            rng.randint(1, 27),
            rng.choice([1, 2, 3, 5, 8, None]),
            rng.choice(["gift", "rush", None, "x" * rng.randint(0, 300)]),
        )
        for i in range(1, 401)
    ]
    return {"customers": customers, "products": products, "orders": orders}


QUERIES = [
    # filters and expressions
    "SELECT id, name FROM customers WHERE tier >= 2 AND city IS NOT NULL ORDER BY id",
    "SELECT id FROM customers WHERE city IN ('Pune', 'Oslo') OR tier IS NULL ORDER BY id",
    "SELECT id, qty * 2 + 1, qty / 2, qty % 3 FROM orders WHERE qty BETWEEN 2 AND 5 ORDER BY id",
    "SELECT id, COALESCE(city, 'unknown'), UPPER(name), LENGTH(name) FROM customers ORDER BY id",
    "SELECT id FROM orders WHERE NOT (qty > 2) ORDER BY id",
    "SELECT id FROM products WHERE price > 50 OR price IS NULL ORDER BY id",
    # joins
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id ORDER BY o.id",
    "SELECT o.id, c.name FROM orders o LEFT JOIN customers c ON o.customer_id = c.id ORDER BY o.id",
    "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
    "ORDER BY c.id, o.id",
    "SELECT o.id, p.title, c.city FROM orders o JOIN products p ON p.id = o.product_id "
    "JOIN customers c ON c.id = o.customer_id WHERE p.category = 'books' ORDER BY o.id",
    "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id AND c.tier = 1 "
    "ORDER BY o.id",
    "SELECT o.id, c.id FROM orders o LEFT JOIN customers c "
    "ON o.customer_id = c.id AND c.tier = 3 ORDER BY o.id",
    "SELECT c.id, p.id FROM customers c JOIN products p ON c.tier < p.id / 10 "
    "WHERE c.id < 6 ORDER BY c.id, p.id",
    "SELECT a.id, b.id FROM customers a JOIN customers b ON a.city = b.city AND a.id < b.id "
    "WHERE a.id < 15 ORDER BY a.id, b.id",
    # aggregation
    "SELECT COUNT(*), COUNT(city), COUNT(tier), SUM(tier), MIN(name), MAX(city) FROM customers",
    "SELECT city, COUNT(*), SUM(tier), AVG(tier) FROM customers GROUP BY city ORDER BY city",
    "SELECT customer_id, COUNT(*) AS n, SUM(qty) FROM orders GROUP BY customer_id "
    "HAVING COUNT(*) >= 8 ORDER BY n DESC, customer_id",
    "SELECT c.city, p.category, COUNT(*), SUM(o.qty), MAX(p.price) FROM orders o "
    "JOIN customers c ON c.id = o.customer_id JOIN products p ON p.id = o.product_id "
    "GROUP BY c.city, p.category ORDER BY c.city, p.category",
    "SELECT c.id, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
    "GROUP BY c.id ORDER BY COUNT(o.id) DESC, c.id",
    "SELECT tier, COUNT(*) FROM customers WHERE id > 1000 GROUP BY tier",
    "SELECT COUNT(*), SUM(qty), AVG(qty), MIN(qty) FROM orders WHERE id > 1000",
    "SELECT qty, COUNT(*) FROM orders GROUP BY qty ORDER BY qty",
    "SELECT tier * 10, COUNT(*) FROM customers GROUP BY tier * 10 ORDER BY 1 DESC",
    # distinct, order, limit
    "SELECT DISTINCT city FROM customers ORDER BY city",
    "SELECT DISTINCT city, tier FROM customers ORDER BY city DESC, tier",
    "SELECT id, note FROM orders ORDER BY note, id",
    "SELECT id, qty FROM orders ORDER BY qty DESC, id DESC",
    "SELECT name FROM customers ORDER BY tier, city DESC, id LIMIT 7",
    "SELECT id FROM orders ORDER BY customer_id, id LIMIT 5 OFFSET 10",
    "SELECT id, price FROM products ORDER BY price DESC, id LIMIT 3",
    "SELECT name FROM customers ORDER BY id LIMIT 0",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
    "ORDER BY c.name, o.id LIMIT 12",
    "SELECT id FROM orders ORDER BY LENGTH(note), id",
    # predicates the optimizer can answer with an index
    "SELECT id, qty FROM orders WHERE customer_id = 7 ORDER BY id",
    "SELECT id FROM orders WHERE customer_id IN (3, 9, 9, 40) ORDER BY id",
    "SELECT id FROM customers WHERE id BETWEEN 10 AND 14 OR id = 50 ORDER BY id",
    "SELECT id FROM orders WHERE id > 390 AND id <= 395 AND qty IS NOT NULL ORDER BY id",
    "SELECT id FROM orders WHERE id >= 2 * 199 ORDER BY id",
    "SELECT c.name, o.id FROM customers c JOIN orders o ON o.customer_id = c.id "
    "WHERE c.id = 12 ORDER BY o.id",
    "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id "
    "JOIN products p ON p.id = o.product_id WHERE p.price < 20 AND c.tier = 2",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id "
    "WHERE c.id IS NULL ORDER BY o.id",
    "SELECT o.id, c.city FROM orders o LEFT JOIN customers c ON o.customer_id = c.id "
    "AND c.city = 'Oslo' WHERE o.id < 30 ORDER BY o.id",
]


@pytest.fixture(scope="module")
def reference() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    for sql in SCHEMA + INDEXES:
        connection.execute(sql)
    for table, rows in dataset().items():
        marks = ", ".join("?" * len(rows[0]))
        connection.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    yield connection
    connection.close()


def load(db: Database) -> None:
    for sql in SCHEMA + INDEXES:
        db.execute(sql)
    for table, rows in dataset().items():
        values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
        db.execute(f"INSERT INTO {table} VALUES {values}")


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def normalise(rows: list[Row] | list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return [tuple(round(v, 9) if isinstance(v, float) else v for v in row) for row in rows]


SETTINGS = {
    "auto": PlannerSettings(),
    "nested_loop": PlannerSettings(join_strategy="nested_loop"),
    "hash": PlannerSettings(join_strategy="hash"),
    "index_nested_loop": PlannerSettings(join_strategy="index_nested_loop"),
    "tiny_sort_memory": PlannerSettings(sort_memory_bytes=2048),
    "analyzed": PlannerSettings(),
    "no_indexes_no_reordering": PlannerSettings(use_indexes=False, reorder_joins=False),
}


@pytest.fixture(scope="module", params=list(SETTINGS))
def pydb(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Database]:
    path: Path = tmp_path_factory.mktemp("suite") / "suite.db"
    with Database(path, pool_size=16, settings=SETTINGS[request.param]) as db:
        load(db)
        if request.param == "analyzed":
            for table in ("customers", "products", "orders"):
                db.execute(f"ANALYZE {table}")
        yield db


@pytest.mark.parametrize("sql", QUERIES)
def test_matches_sqlite(pydb: Database, reference: sqlite3.Connection, sql: str) -> None:
    expected = normalise(reference.execute(sql).fetchall())
    got = normalise(pydb.execute(sql).rows)
    if "ORDER BY" in sql:
        assert got == expected
    else:
        assert sorted(got, key=repr) == sorted(expected, key=repr)


def test_suite_leaves_no_pins_or_temporary_pages(pydb: Database) -> None:
    free_before = pydb.disk.free_page_count
    pages_before = pydb.disk.num_pages
    for sql in QUERIES:
        pydb.execute(sql)
    assert pydb.pool.pinned_pages() == {}
    # sort runs are freed after each query, so any growth is matched by free pages
    assert pydb.disk.num_pages - pages_before <= pydb.disk.free_page_count - free_before + 1
