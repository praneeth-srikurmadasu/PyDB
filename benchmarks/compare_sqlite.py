"""Run the same workloads on PyDB and SQLite and print a timing table.

Usage::

    python benchmarks/compare_sqlite.py [--rows 20000] [--lookups 2000] [--repeat 3]

Both engines use a database file in a temporary directory and run identical SQL. Writes
run inside one transaction per batch on both. Each workload reports the best of
``--repeat`` runs in milliseconds. SQLite is a C library tuned for decades, so the point is
the *shape* of the numbers (which operations an index helps, how joins scale), not a race.
"""

import argparse
import random
import sqlite3
import tempfile
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Protocol

from pydb import __version__
from pydb.database import Database

Row = tuple[object, ...]


class Engine(Protocol):
    name: str

    def run(self, sql: str, params: Sequence[object] = ()) -> list[Row]: ...

    def close(self) -> None: ...


class PyDBEngine:
    name = f"PyDB {__version__}"

    def __init__(self, path: Path) -> None:
        self.db = Database(path)

    def run(self, sql: str, params: Sequence[object] = ()) -> list[Row]:
        return list(self.db.execute(sql, list(params)).rows)

    def close(self) -> None:
        self.db.close()


class SQLiteEngine:
    name = f"SQLite {sqlite3.sqlite_version}"

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT

    def run(self, sql: str, params: Sequence[object] = ()) -> list[Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        self.conn.close()


def load(engine: Engine, rows: int, batch: int = 500) -> None:
    engine.run("CREATE TABLE users (id INT PRIMARY KEY, city INT, age INT, name TEXT)")
    engine.run("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total FLOAT)")
    rng = random.Random(42)

    def user(i: int) -> str:
        return f"({i}, {rng.randrange(50)}, {rng.randrange(18, 90)}, 'user{i}')"

    def order(i: int) -> str:
        return f"({i}, {rng.randrange(rows)}, {rng.randrange(1, 10000) / 100})"

    makers: tuple[tuple[str, Callable[[int], str]], ...] = (("users", user), ("orders", order))
    for table, make in makers:
        for start in range(0, rows, batch):
            values = ", ".join(make(i) for i in range(start, min(start + batch, rows)))
            engine.run("BEGIN")
            engine.run(f"INSERT INTO {table} VALUES {values}")
            engine.run("COMMIT")
    engine.run("CREATE INDEX orders_user ON orders (user_id)")
    engine.run("CREATE INDEX users_city ON users (city)")
    engine.run("ANALYZE users")
    engine.run("ANALYZE orders")


def workloads(rows: int, lookups: int) -> list[tuple[str, Callable[[Engine], object]]]:
    rng = random.Random(7)
    keys = [rng.randrange(rows) for _ in range(lookups)]

    def point_lookups(engine: Engine) -> object:
        for key in keys:
            engine.run("SELECT name FROM users WHERE id = ?", (key,))
        return None

    def update_batch(engine: Engine) -> object:
        engine.run("BEGIN")
        for key in keys[:500]:
            engine.run("UPDATE users SET age = age + 1 WHERE id = ?", (key,))
        engine.run("COMMIT")
        return None

    return [
        (f"{lookups} primary-key lookups", point_lookups),
        (
            "range scan on PK (1% of rows)",
            lambda e: e.run(
                "SELECT COUNT(*) FROM users WHERE id BETWEEN ? AND ?", (0, rows // 100)
            ),
        ),
        ("index lookup on city (2%)", lambda e: e.run("SELECT id FROM users WHERE city = 7")),
        ("full scan + filter", lambda e: e.run("SELECT COUNT(*) FROM users WHERE age > 60")),
        (
            "GROUP BY 50 groups",
            lambda e: e.run("SELECT city, COUNT(*), AVG(age) FROM users GROUP BY city"),
        ),
        (
            "ORDER BY + LIMIT 10",
            lambda e: e.run("SELECT id, total FROM orders ORDER BY total DESC LIMIT 10"),
        ),
        (
            "join users x orders (all rows)",
            lambda e: e.run(
                "SELECT COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id WHERE u.age < 30"
            ),
        ),
        ("500 updates in one transaction", update_batch),
    ]


def best_ms(action: Callable[[], object], repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        action()
        best = min(best, time.perf_counter() - started)
    return best * 1000


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Compare PyDB with SQLite")
    parser.add_argument("--rows", type=int, default=20000, help="rows per table")
    parser.add_argument("--lookups", type=int, default=2000, help="point lookups to run")
    parser.add_argument("--repeat", type=int, default=3, help="runs per workload (best wins)")
    args = parser.parse_args(argv)

    results: dict[str, dict[str, float]] = {}
    names: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        for engine_class in (PyDBEngine, SQLiteEngine):
            path = Path(directory) / f"{engine_class.__name__}.db"
            engine: Engine = engine_class(path)
            names.append(engine.name)
            try:
                timings = {
                    f"load {args.rows:,} rows x 2 tables": best_ms(
                        partial(load, engine, args.rows), 1
                    )
                }
                for label, work in workloads(args.rows, args.lookups):
                    timings[label] = best_ms(partial(work, engine), args.repeat)
                results[engine.name] = timings
            finally:
                engine.close()

    pydb_name, sqlite_name = names
    labels = list(results[pydb_name])
    width = max(len(label) for label in labels)
    print(f"| {'workload'.ljust(width)} | {pydb_name} ms | {sqlite_name} ms | ratio |")
    print(f"|{'-' * (width + 2)}|---:|---:|---:|")
    for label in labels:
        ours, theirs = results[pydb_name][label], results[sqlite_name][label]
        ratio = ours / theirs if theirs else float("inf")
        print(f"| {label.ljust(width)} | {ours:,.1f} | {theirs:,.1f} | {ratio:,.0f}x |")


if __name__ == "__main__":
    main()
