"""Interactive SQL shell.

Usage::

    python -m pydb.cli.repl data.db

Statements may span several lines and run when a line ends the statement with ``;``.
Meta commands start with a dot: ``.tables``, ``.schema [table]``, ``.timer on|off``,
``.help`` and ``.quit``.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from typing import TextIO

from pydb.database import Database
from pydb.errors import PyDBError, SqlSyntaxError
from pydb.execution.executor import Result
from pydb.sql import ast
from pydb.sql.lexer import tokenize
from pydb.sql.parser import parse_script
from pydb.sql.tokens import TokenType
from pydb.types import Value

PROMPT = "pydb> "
PROMPT_IN_TRANSACTION = "pydb*> "
CONTINUATION = "  ...> "
HELP = """\
.tables              list tables
.schema [table]      show CREATE statements
.timer on|off        show how long each statement takes
.help                show this help
.quit                exit (also .exit, or end of input)
SQL statements end with ';' and may span several lines."""


def format_value(value: Value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_table(columns: Sequence[str], rows: Sequence[Sequence[Value]]) -> str:
    """Render rows as an aligned ASCII table followed by a row count."""
    cells = [[format_value(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        widths = [max(w, len(v)) for w, v in zip(widths, row, strict=True)]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(values: Sequence[str]) -> str:
        return "| " + " | ".join(v.ljust(w) for v, w in zip(values, widths, strict=True)) + " |"

    lines = [rule, line(columns), rule, *(line(r) for r in cells), rule]
    count = len(rows)
    lines.append(f"({count} row{'' if count == 1 else 's'})")
    return "\n".join(lines)


def statement_is_complete(text: str) -> bool:
    """True when ``text`` ends with a ``;`` outside any string, identifier or comment."""
    try:
        tokens = tokenize(text)
    except SqlSyntaxError as exc:
        return "unterminated" not in exc.detail  # let other errors surface now
    return len(tokens) >= 2 and tokens[-2].type is TokenType.SEMICOLON


def is_blank(text: str) -> bool:
    try:
        return tokenize(text)[0].type is TokenType.EOF
    except SqlSyntaxError:
        return False


class Repl:
    def __init__(self, db: Database, stdin: TextIO, stdout: TextIO, *, interactive: bool) -> None:
        self.db = db
        self.stdin = stdin
        self.stdout = stdout
        self.interactive = interactive
        self.timer = False

    def run(self) -> int:
        if self.interactive:
            self._print('PyDB shell. Type ".help" for help.')
        buffer = ""
        while True:
            if self.interactive:
                prompt = PROMPT_IN_TRANSACTION if self.db.in_transaction else PROMPT
                self.stdout.write(CONTINUATION if buffer else prompt)
                self.stdout.flush()
            try:
                line = self.stdin.readline()
            except KeyboardInterrupt:
                self._print("")
                buffer = ""
                continue
            if not line:  # end of input
                if buffer and not is_blank(buffer):
                    self.run_sql(buffer)
                return 0
            if not buffer and line.strip().startswith("."):
                if not self.meta(line.strip()):
                    return 0
                continue
            buffer += line
            if is_blank(buffer):
                buffer = ""
            elif statement_is_complete(buffer):
                self.run_sql(buffer)
                buffer = ""

    def run_sql(self, text: str) -> None:
        try:
            statements = parse_script(text)
        except PyDBError as exc:
            self._print(f"Error: {exc}")
            return
        for statement in statements:
            started = time.perf_counter()
            try:
                result = self.db.execute_statement(statement)
            except PyDBError as exc:
                self._print(f"Error: {exc}")
                return
            self._show(result)
            if self.timer:
                self._print(f"Time: {(time.perf_counter() - started) * 1000:.3f} ms")

    def meta(self, command: str) -> bool:
        """Run a dot command. Return False to exit."""
        name, _, argument = command.partition(" ")
        argument = argument.strip()
        if name in (".quit", ".exit"):
            return False
        if name == ".help":
            self._print(HELP)
        elif name == ".tables":
            names = [t.name for t in self.db.catalog.tables()]
            self._print("\n".join(names) if names else "(no tables)")
        elif name == ".schema":
            self._schema(argument)
        elif name == ".timer" and argument in ("on", "off"):
            self.timer = argument == "on"
        else:
            self._print(f'Error: unknown command "{command}". Type ".help" for help.')
        return True

    # ----------------------------------------------------------------- output

    def _show(self, result: Result) -> None:
        if result.columns:
            self._print(format_table(result.columns, result.rows))
        else:
            self._print(result.message)

    def _schema(self, table_name: str) -> None:
        try:
            tables = (
                [self.db.catalog.get_table(table_name)] if table_name else self.db.catalog.tables()
            )
        except PyDBError as exc:
            self._print(f"Error: {exc}")
            return
        for table in tables:
            pk = next((i for i in table.indexes if i.primary), None)
            constraint_names = {f"{table.name}_pkey"}
            columns = []
            for position, column in enumerate(table.columns):
                single_pk = pk is not None and pk.columns == (position,)
                unique = any(
                    i.name == f"{table.name}_{column.name}_key" and i.columns == (position,)
                    for i in table.indexes
                )
                if unique:
                    constraint_names.add(f"{table.name}_{column.name}_key")
                columns.append(
                    ast.ColumnDef(
                        column.name,
                        ast.TypeName(column.type, column.length),
                        not_null=column.not_null and not single_pk,
                        primary_key=single_pk,
                        unique=unique,
                        default=None if column.default is None else ast.Literal(column.default),
                    )
                )
            table_pk = (
                tuple(table.columns[p].name for p in pk.columns)
                if pk and len(pk.columns) > 1
                else ()
            )
            self._print(ast.CreateTable(table.name, tuple(columns), table_pk).to_sql() + ";")
            for index in table.indexes:
                if index.name not in constraint_names:
                    cols = tuple(table.columns[p].name for p in index.columns)
                    self._print(
                        ast.CreateIndex(index.name, table.name, cols, index.unique).to_sql() + ";"
                    )

    def _print(self, text: str) -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pydb.cli.repl", description="PyDB SQL shell")
    parser.add_argument(
        "database", nargs="?", help="path of the database file (created if missing)"
    )
    parser.add_argument("--connect", metavar="HOST:PORT", help="connect to a PyDB server")
    args = parser.parse_args(argv)
    if args.connect:
        print("error: --connect needs the PyDB server, which is not available yet", file=sys.stderr)
        return 2
    if not args.database:
        parser.error("a database path is required")
    try:
        db = Database(args.database)
    except PyDBError as exc:
        print(f"error: cannot open {args.database}: {exc}", file=sys.stderr)
        return 1
    with db:
        return Repl(db, sys.stdin, sys.stdout, interactive=sys.stdin.isatty()).run()


if __name__ == "__main__":
    sys.exit(main())
