"""EXPLAIN and EXPLAIN ANALYZE output.

EXPLAIN prints the operator tree, one operator per line, indented under its parent, with the
planner's estimated rows and cost. EXPLAIN ANALYZE also runs the query and adds, per
operator, the rows it actually produced, the time spent in it and the buffer pool hits and
misses it caused. Time and buffer counts include the operator's children, as in PostgreSQL.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydb.execution.operators.base import Operator
from pydb.storage.buffer_pool import BufferPoolManager


@dataclass(slots=True)
class OperatorStats:
    rows: int = 0
    seconds: float = 0.0
    hits: int = 0
    misses: int = 0
    loops: int = 0  # how many times the operator was opened
    active: int = field(default=0)  # nesting depth, so self-calls are not counted twice


def explain_lines(plan: Operator, actual: dict[int, OperatorStats] | None = None) -> list[str]:
    lines: list[str] = []

    def visit(op: Operator, depth: int) -> None:
        text = f"{op.label()}  (rows={op.est_rows:.0f} cost={op.est_cost:.2f})"
        if actual is not None:
            stats = actual.get(id(op), OperatorStats())
            loops = f" loops={stats.loops}" if stats.loops > 1 else ""
            text += (
                f" (actual rows={stats.rows}{loops} time={stats.seconds * 1000:.3f} ms "
                f"hits={stats.hits} misses={stats.misses})"
            )
        lines.append(("  " * (depth - 1) + "-> " if depth else "") + text)
        for child in op.children():
            visit(child, depth + 1)

    visit(plan, 0)
    return lines


def instrument(plan: Operator, pool: BufferPoolManager) -> dict[int, OperatorStats]:
    """Wrap every operator's methods to record rows, time and buffer traffic."""
    collected: dict[int, OperatorStats] = {}

    def wrap(op: Operator) -> None:
        stats = collected.setdefault(id(op), OperatorStats())
        for name in ("open", "next", "next_with_rid", "close"):
            method = getattr(op, name, None)
            if method is not None:
                setattr(op, name, _timed(method, stats, pool, name))
        for child in op.children():
            wrap(child)

    wrap(plan)
    return collected


def _timed(
    method: Callable[..., Any], stats: OperatorStats, pool: BufferPoolManager, name: str
) -> Callable[..., Any]:
    counts_rows = name in ("next", "next_with_rid")

    def timed(*args: Any, **kwargs: Any) -> Any:
        if stats.active:  # e.g. next() implemented by calling next_with_rid() on itself
            return method(*args, **kwargs)
        stats.active += 1
        before = pool.stats
        started = time.perf_counter()
        try:
            result = method(*args, **kwargs)
        finally:
            stats.active -= 1
            stats.seconds += time.perf_counter() - started
            after = pool.stats
            stats.hits += after.hits - before.hits
            stats.misses += after.misses - before.misses
        if name == "open":
            stats.loops += 1
        if counts_rows and result is not None:
            stats.rows += 1
        return result

    return timed
