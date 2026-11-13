"""Runs parsed statements: binds them, builds operator trees and collects results."""

import time
from dataclasses import dataclass, field

from pydb.catalog.catalog import Catalog
from pydb.catalog.schema import Column
from pydb.errors import CatalogError, NotSupportedError, TypeMismatchError, UnknownColumnError
from pydb.execution.expressions import BoundExpr, compile_expr
from pydb.execution.operators.base import Operator
from pydb.execution.operators.delete import Delete
from pydb.execution.operators.insert import Insert
from pydb.execution.operators.update import Update
from pydb.planner.binder import Binder
from pydb.planner.explain import explain_lines, instrument
from pydb.planner.optimizer import Optimizer
from pydb.planner.physical_plan import PlannerSettings
from pydb.planner.stats import collect
from pydb.sql import ast
from pydb.storage.tuple import Row
from pydb.types import Value


@dataclass(slots=True)
class Result:
    """What a statement returns: rows for queries, a count and message for everything else."""

    columns: tuple[str, ...] = ()
    rows: list[Row] = field(default_factory=list)
    rowcount: int = -1
    message: str = ""


class Executor:
    def __init__(self, catalog: Catalog, settings: PlannerSettings | None = None) -> None:
        self.catalog = catalog
        self.binder = Binder(catalog)
        self.settings = settings or PlannerSettings()
        self.optimizer = Optimizer(catalog.pool, self.settings)

    def execute(self, statement: ast.Statement) -> Result:
        match statement:
            case ast.Select():
                plan = self.plan_select(statement)
                rows = list(plan.rows())
                return Result(plan.columns, rows, len(rows), f"SELECT {len(rows)}")
            case ast.Insert():
                bound = self.binder.bind_insert(statement)
                values = [
                    self._insert_values(bound.table.columns, bound.positions, row)
                    for row in bound.rows
                ]
                return self._run_write(Insert(bound.table, values), "INSERT")
            case ast.Update():
                upd = self.binder.bind_update(statement)
                source = self.optimizer.access_path(upd.table, upd.where)
                assignments = [(p, compile_expr(e)) for p, e in upd.assignments]
                return self._run_write(Update(upd.table, source, assignments), "UPDATE")
            case ast.Delete():
                dele = self.binder.bind_delete(statement)
                return self._run_write(
                    Delete(dele.table, self.optimizer.access_path(dele.table, dele.where)), "DELETE"
                )
            case ast.CreateTable():
                return self._create_table(statement)
            case ast.DropTable():
                if statement.if_exists and not self.catalog.has_table(statement.name):
                    return Result(message="DROP TABLE (did not exist)")
                self.catalog.drop_table(statement.name)
                return Result(message="DROP TABLE")
            case ast.CreateIndex():
                table = self.catalog.get_table(statement.table)
                positions = [
                    self._position(table.columns, c, table.name) for c in statement.columns
                ]
                self.catalog.create_index(statement.name, table.name, positions, statement.unique)
                return Result(message="CREATE INDEX")
            case ast.DropIndex():
                if statement.if_exists and self.catalog.find_index(statement.name) is None:
                    return Result(message="DROP INDEX (did not exist)")
                self.catalog.drop_index(statement.name)
                return Result(message="DROP INDEX")
            case ast.ShowTables():
                names: list[Row] = [(t.name,) for t in self.catalog.tables()]
                return Result(("table",), names, len(names), f"{len(names)} tables")
            case ast.Describe():
                return self._describe(statement.table)
            case ast.Explain():
                return self._explain(statement)
            case ast.Analyze():
                table = self.catalog.get_table(statement.table)
                self.catalog.set_stats(table, collect(table))
                return Result(message="ANALYZE")
            case ast.Begin() | ast.Commit() | ast.Rollback():
                raise NotSupportedError(
                    "explicit transactions are not supported yet; each statement commits on its own"
                )
        raise NotSupportedError(f"{type(statement).__name__} is not supported")

    # ----------------------------------------------------------------- planning

    def plan_select(self, select: ast.Select) -> Operator:
        return self.optimizer.plan_select(self.binder.bind_select(select))

    def _explain(self, statement: ast.Explain) -> Result:
        plan = self.plan_select(statement.query)
        if not statement.analyze:
            lines = explain_lines(plan)
        else:
            pool = self.catalog.pool
            collected = instrument(plan, pool)
            before = pool.stats
            started = time.perf_counter()
            count = sum(1 for _ in plan.rows())
            elapsed = time.perf_counter() - started
            after = pool.stats
            lines = explain_lines(plan, collected)
            lines.append(
                f"Execution: {count} rows in {elapsed * 1000:.3f} ms, "
                f"buffer hits={after.hits - before.hits} misses={after.misses - before.misses}"
            )
        return Result(("QUERY PLAN",), [(line,) for line in lines], len(lines), "EXPLAIN")

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _run_write(operator: Operator, verb: str) -> Result:
        (row,) = list(operator.rows())
        count = row[0]
        assert isinstance(count, int)
        return Result(rowcount=count, message=f"{verb} {count}")

    @staticmethod
    def _insert_values(
        columns: tuple[Column, ...], positions: tuple[int, ...], exprs: tuple[BoundExpr, ...]
    ) -> list[Value]:
        values: list[Value] = [c.default for c in columns]
        for position, expr in zip(positions, exprs, strict=True):
            values[position] = compile_expr(expr)(())
        return values

    def _create_table(self, stmt: ast.CreateTable) -> Result:
        if stmt.if_not_exists and self.catalog.has_table(stmt.name):
            return Result(message="CREATE TABLE (already exists)")
        column_pk = [i for i, c in enumerate(stmt.columns) if c.primary_key]
        if (stmt.primary_key and column_pk) or len(column_pk) > 1:
            raise CatalogError(f'multiple primary keys for table "{stmt.name}" are not allowed')
        names = [c.name for c in stmt.columns]
        pk = [self._position_by_name(names, n, stmt.name) for n in stmt.primary_key] or column_pk
        columns: list[Column] = []
        for i, c in enumerate(stmt.columns):
            column = Column(c.name, c.type.base, c.type.length, c.not_null or i in pk)
            if c.default is not None:
                try:
                    default = column.validate(c.default.value)
                except TypeMismatchError as exc:
                    raise TypeMismatchError(f"invalid DEFAULT: {exc}") from None
                column = Column(column.name, column.type, column.length, column.not_null, default)
            columns.append(column)
        unique = [i for i, c in enumerate(stmt.columns) if c.unique and [i] != pk]
        self.catalog.create_table(stmt.name, columns, pk, unique)
        return Result(message="CREATE TABLE")

    def _describe(self, name: str) -> Result:
        table = self.catalog.get_table(name)
        rows: list[Row] = []
        for i, column in enumerate(table.columns):
            keys = [
                "PRI" if index.primary else "UNI" if index.unique else "IDX"
                for index in table.indexes
                if i in index.columns
            ]
            default = None if column.default is None else ast.Literal(column.default).to_sql()
            rows.append(
                (
                    column.name,
                    column.type_sql(),
                    not column.not_null,
                    default,
                    ",".join(keys) or None,
                )
            )
        return Result(("column", "type", "nullable", "default", "key"), rows, len(rows), name)

    @staticmethod
    def _position(columns: tuple[Column, ...], name: str, table: str) -> int:
        return Executor._position_by_name([c.name for c in columns], name, table)

    @staticmethod
    def _position_by_name(names: list[str], name: str, table: str) -> int:
        if name not in names:
            raise UnknownColumnError(f'column "{name}" of table "{table}" does not exist')
        return names.index(name)
