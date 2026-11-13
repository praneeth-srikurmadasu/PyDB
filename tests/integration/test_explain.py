import io
import re
from pathlib import Path

from pydb.cli.repl import Repl
from pydb.database import Database


def explain(db: Database, sql: str) -> list[str]:
    return [row[0] for row in db.execute(sql).rows]  # type: ignore[misc]


def setup(db: Database) -> None:
    db.execute_script(
        "CREATE TABLE users (id INT PRIMARY KEY, city TEXT, age INT);"
        "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total FLOAT);"
    )
    db.execute(
        "INSERT INTO users VALUES "
        + ", ".join(f"({i}, 'c{i % 5}', {20 + i % 40})" for i in range(400))
    )
    db.execute(
        "INSERT INTO orders VALUES "
        + ", ".join(f"({i}, {i % 400}, {i * 1.5})" for i in range(1200))
    )


def test_explain_shape_and_estimates(tmp_path: Path) -> None:
    with Database(tmp_path / "e.db") as db:
        setup(db)
        lines = explain(db, "EXPLAIN SELECT city FROM users WHERE age > 50 ORDER BY city")
        assert db.execute("EXPLAIN SELECT 1").columns == ("QUERY PLAN",)
        assert lines[0].startswith("Sort by city")
        assert lines[1].startswith("-> Projection [city]")
        assert lines[2].startswith("  -> Filter (age > 50)")
        assert lines[3].startswith("    -> SeqScan users")
        for line in lines:
            assert re.search(r"\(rows=\d+ cost=\d+\.\d\d\)$", line)


def test_plans_change_with_indexes_and_statistics(tmp_path: Path) -> None:
    with Database(tmp_path / "e.db") as db:
        setup(db)
        query = (
            "EXPLAIN SELECT o.total FROM orders o JOIN users u ON o.user_id = u.id "
            "WHERE u.city = 'c1'"
        )
        before = "\n".join(explain(db, query))
        assert "HashJoin" in before
        db.execute("CREATE INDEX orders_user ON orders (user_id)")
        db.execute("ANALYZE users")
        db.execute("ANALYZE orders")
        after = "\n".join(explain(db, query))
        assert after != before
        assert "orders_user" in after or "HashJoin" in after
        assert "-> SeqScan users" in "\n".join(
            explain(db, "EXPLAIN SELECT * FROM users WHERE age > 30")
        )
        assert (
            "IndexScan users using users_pkey"
            in explain(db, "EXPLAIN SELECT * FROM users WHERE id BETWEEN 5 AND 9")[1]
        )


def test_explain_analyze_reports_actual_rows(tmp_path: Path) -> None:
    with Database(tmp_path / "e.db") as db:
        setup(db)
        db.execute("ANALYZE users")
        lines = explain(db, "EXPLAIN ANALYZE SELECT id FROM users WHERE id < 10")
        assert re.search(r"actual rows=10 time=\d+\.\d{3} ms hits=\d+ misses=\d+", lines[0])
        assert lines[-1].startswith("Execution: 10 rows in ")
        loops = explain(
            db,
            "EXPLAIN ANALYZE SELECT u.id FROM users u JOIN users v ON u.id < v.id "
            "WHERE u.id < 3 AND v.id < 3",
        )
        assert any("loops=" in line for line in loops)
        assert loops[-1].startswith("Execution: 3 rows")


def test_explain_in_the_repl(tmp_path: Path) -> None:
    with Database(tmp_path / "e.db") as db:
        setup(db)
        out = io.StringIO()
        Repl(
            db, io.StringIO("EXPLAIN SELECT * FROM users WHERE id = 3;\n"), out, interactive=False
        ).run()
    assert "QUERY PLAN" in out.getvalue()
    assert "IndexScan users using users_pkey (= 3)" in out.getvalue()
