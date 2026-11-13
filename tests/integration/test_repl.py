import io
import subprocess
import sys
from pathlib import Path

from pydb.cli.repl import Repl, format_table, statement_is_complete
from pydb.database import Database


def run_repl(path: Path, script: str) -> str:
    out = io.StringIO()
    with Database(path) as db:
        assert Repl(db, io.StringIO(script), out, interactive=False).run() == 0
    return out.getvalue()


def test_format_table() -> None:
    text = format_table(("id", "name"), [(1, "ann"), (22, None), (3, True)])
    assert text.splitlines() == [
        "+----+------+",
        "| id | name |",
        "+----+------+",
        "| 1  | ann  |",
        "| 22 | NULL |",
        "| 3  | true |",
        "+----+------+",
        "(3 rows)",
    ]
    assert format_table(("x",), []).endswith("(0 rows)")


def test_statement_completion() -> None:
    assert statement_is_complete("SELECT 1;")
    assert statement_is_complete("SELECT 1; -- trailing comment\n")
    assert not statement_is_complete("SELECT 1")
    assert not statement_is_complete("SELECT ';")
    assert not statement_is_complete("SELECT 1 /* ; */")
    assert statement_is_complete("SELECT 1 ? 2;")  # a lexical error is reported, not awaited


def test_multiline_statements_meta_commands_and_errors(tmp_path: Path) -> None:
    out = run_repl(
        tmp_path / "r.db",
        "CREATE TABLE t (\n"
        "  id INT PRIMARY KEY,\n"
        "  note TEXT DEFAULT 'a;b'  -- a ; in a string and in a comment\n"
        ");\n"
        "INSERT INTO t VALUES (1, 'x'), (2, NULL);\n"
        "SELECT * FROM t WHERE id > 1;\n"
        "SELECT missing FROM t;\n"
        ".tables\n"
        ".schema t\n"
        ".timer on\n"
        "SELECT 1;\n"
        ".nonsense\n"
        "SELECT 2; SELECT 3;\n",
    )
    assert "CREATE TABLE\nINSERT 2\n" in out
    assert "| 2  | NULL |" in out
    assert 'Error: column "missing" does not exist' in out
    assert "\nt\n" in out
    assert "CREATE TABLE t (id INT PRIMARY KEY, note TEXT DEFAULT 'a;b');" in out
    assert "Time: " in out
    assert 'unknown command ".nonsense"' in out
    assert out.count("(1 row)") == 4  # WHERE id > 1, SELECT 1, SELECT 2, SELECT 3


def test_quit_and_unterminated_final_statement(tmp_path: Path) -> None:
    out = run_repl(tmp_path / "q.db", "SELECT 1\n")  # no semicolon: runs at end of input
    assert "(1 row)" in out
    assert run_repl(tmp_path / "q.db", ".quit\nSELECT 1;\n") == ""


def test_schema_shows_composite_keys_and_extra_indexes(tmp_path: Path) -> None:
    out = run_repl(
        tmp_path / "s.db",
        "CREATE TABLE p (a INT, b VARCHAR(5) UNIQUE, c FLOAT NOT NULL, PRIMARY KEY (a, b));\n"
        "CREATE UNIQUE INDEX p_c ON p (c);\n.schema\n",
    )
    assert (
        "CREATE TABLE p (a INT NOT NULL, b VARCHAR(5) NOT NULL UNIQUE, c FLOAT NOT NULL, "
        "PRIMARY KEY (a, b));" in out
    )
    assert "CREATE UNIQUE INDEX p_c ON p (c);" in out


def test_command_line_entry_point_persists_between_runs(tmp_path: Path) -> None:
    db_file = str(tmp_path / "cli.db")

    def cli(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pydb.cli.repl", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
        )

    first = cli("CREATE TABLE t (x INT);\nINSERT INTO t VALUES (41), (42);\n", db_file)
    assert first.returncode == 0, first.stderr
    second = cli("SELECT x FROM t WHERE x > 41;\n", db_file)
    assert "| 42 |" in second.stdout
    assert cli("", "--connect", "localhost:5555").returncode == 2
