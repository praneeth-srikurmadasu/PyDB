"""Which table locks a statement needs.

Locks are taken before a statement runs, so this works on the parsed statement: tables a
query reads are locked SHARED; the table a write or schema change targets is locked
EXCLUSIVE. Statements that only read the catalog (``SHOW TABLES``, ``DESCRIBE``) and
transaction control take no table locks.
"""

from collections.abc import Callable

from pydb.sql import ast
from pydb.txn.lock_manager import LockMode

Resource = tuple[str, str]
"""A lockable resource: ``("table", name)``."""


def table(name: str) -> Resource:
    return ("table", name)


def required_locks(
    statement: ast.Statement, index_table: Callable[[str], str | None]
) -> dict[Resource, LockMode]:
    """The locks ``statement`` needs. ``index_table`` maps an index name to its table."""
    locks: dict[Resource, LockMode] = {}

    def need(name: str, mode: LockMode) -> None:
        held = locks.get(table(name))
        if held is None or not held.covers(mode):
            locks[table(name)] = mode

    match statement:
        case ast.Select():
            for name in tables_read(statement):
                need(name, LockMode.SHARED)
        case ast.Explain(query=query):
            for name in tables_read(query):
                need(name, LockMode.SHARED)
        case ast.Insert(table=name) | ast.Update(table=name) | ast.Delete(table=name):
            need(name, LockMode.EXCLUSIVE)
        case ast.CreateTable(name=name) | ast.DropTable(name=name):
            need(name, LockMode.EXCLUSIVE)
        case ast.CreateIndex(table=name):
            need(name, LockMode.EXCLUSIVE)
        case ast.DropIndex(name=index):
            owner = index_table(index)
            if owner is not None:
                need(owner, LockMode.EXCLUSIVE)
        case ast.Analyze(table=name):
            need(name, LockMode.SHARED)
        case _:
            pass
    return locks


def tables_read(select: ast.Select) -> list[str]:
    names = [] if select.from_table is None else [select.from_table.name]
    names.extend(join.table.name for join in select.joins)
    return names
