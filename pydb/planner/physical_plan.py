"""Planner settings and the cost model for physical operators.

Costs are in units of one page read; each row processed adds a small CPU cost. With
``rows`` and ``pages`` the estimated sizes of the inputs:

=====================  =========================================================
operator               cost
=====================  =========================================================
SeqScan                pages + rows * CPU_ROW
IndexScan              height + matches * (1 + CPU_ROW)  (one page per matching row)
Filter, Projection     child + child_rows * CPU_ROW
NestedLoopJoin         outer + outer_rows * inner
HashJoin               outer + inner + (outer_rows + inner_rows) * HASH_ROW
IndexNestedLoopJoin    outer + outer_rows * (height + matches_per_lookup)
HashAggregate          child + child_rows * HASH_ROW
Sort                   child + rows * log2(rows) * CPU_ROW
TopN                   child + rows * log2(n) * CPU_ROW
=====================  =========================================================

The estimated output rows of a join are ``outer_rows * inner_rows * selectivity(ON)``; a
LEFT join returns at least ``outer_rows``.
"""

from dataclasses import dataclass

from pydb.config import SORT_MEMORY_BYTES

CPU_ROW = 0.01
HASH_ROW = 0.02
INDEX_SELECTIVITY_THRESHOLD = 0.15
"""Use an index scan only if it is expected to return less than this fraction of rows."""
MAX_REORDERED_TABLES = 4
"""Join orders are searched exhaustively for up to this many INNER-joined tables."""

JOIN_STRATEGIES = ("auto", "index_nested_loop", "hash", "nested_loop")


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    join_strategy: str = "auto"
    """``auto`` chooses by cost; any other value forces that algorithm where it applies."""
    sort_memory_bytes: int = SORT_MEMORY_BYTES
    reorder_joins: bool = True
    use_indexes: bool = True

    def __post_init__(self) -> None:
        if self.join_strategy not in JOIN_STRATEGIES:
            raise ValueError(f"join_strategy must be one of {JOIN_STRATEGIES}")
        if self.sort_memory_bytes < 1:
            raise ValueError("sort_memory_bytes must be positive")
