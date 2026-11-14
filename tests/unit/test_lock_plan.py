import pytest

from pydb.sql.parser import parse
from pydb.txn.lock_manager import LockMode
from pydb.txn.lock_plan import required_locks, table

S = LockMode.SHARED
X = LockMode.EXCLUSIVE


def plan(sql: str) -> dict[tuple[str, str], LockMode]:
    return required_locks(parse(sql), {"t_name_idx": "t"}.get)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t", {table("t"): S}),
        ("SELECT 1", {}),
        (
            "SELECT * FROM t JOIN u ON t.id = u.id LEFT JOIN v ON v.id = u.id",
            {table("t"): S, table("u"): S, table("v"): S},
        ),
        ("SELECT * FROM t a JOIN t b ON a.id = b.id", {table("t"): S}),
        ("EXPLAIN ANALYZE SELECT * FROM t", {table("t"): S}),
        ("INSERT INTO t VALUES (1)", {table("t"): X}),
        ("UPDATE t SET a = 1 WHERE b = 2", {table("t"): X}),
        ("DELETE FROM t", {table("t"): X}),
        ("CREATE TABLE n (id INT)", {table("n"): X}),
        ("DROP TABLE IF EXISTS t", {table("t"): X}),
        ("CREATE INDEX i ON t (a)", {table("t"): X}),
        ("DROP INDEX t_name_idx", {table("t"): X}),
        ("DROP INDEX IF EXISTS missing", {}),
        ("ANALYZE t", {table("t"): S}),
        ("SHOW TABLES", {}),
        ("DESCRIBE t", {}),
        ("BEGIN", {}),
        ("COMMIT", {}),
    ],
)
def test_required_locks(sql: str, expected: dict[tuple[str, str], LockMode]) -> None:
    assert plan(sql) == expected
