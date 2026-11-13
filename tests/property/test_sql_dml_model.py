"""Random INSERT / UPDATE / DELETE statements against a dict model, with index checks.

The table has a primary key, a UNIQUE column and a secondary index, so every statement
exercises constraint checks and index maintenance. After every step the rows must match the
model and every index must match the heap exactly.
"""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from pydb.catalog.integrity import check_table
from pydb.config import MAX_TEXT_BYTES
from pydb.database import Database
from pydb.errors import UniqueViolationError, ValueTooLargeError

_ids = st.integers(0, 30)
_codes = st.none() | st.integers(0, 8)
_groups = st.integers(0, 4)


class DmlModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "dml.db"
        self.db = Database(self.path, pool_size=8)
        self.db.execute("CREATE TABLE t (id INT PRIMARY KEY, code INT UNIQUE, grp INT, pad TEXT)")
        self.db.execute("CREATE INDEX t_grp ON t (grp)")
        self.model: dict[int, tuple[int | None, int, str]] = {}

    def _code_taken(self, code: int | None, exclude: set[int]) -> bool:
        return code is not None and any(
            c == code for i, (c, _, _) in self.model.items() if i not in exclude
        )

    @rule(
        rows=st.lists(st.tuples(_ids, _codes, _groups, st.integers(0, 600)), min_size=1, max_size=4)
    )
    def insert(self, rows: list[tuple[int, int | None, int, int]]) -> None:
        values = ", ".join(
            f"({i}, {'NULL' if c is None else c}, {g}, '{'p' * n}')" for i, c, g, n in rows
        )
        ok = True
        staged: dict[int, tuple[int | None, int, str]] = {}
        for i, c, g, n in rows:
            taken_codes = {v[0] for v in staged.values()}
            if (
                i in self.model
                or i in staged
                or self._code_taken(c, set())
                or (c is not None and c in taken_codes)
            ):
                ok = False
            staged[i] = (c, g, "p" * n)
        try:
            self.db.execute(f"INSERT INTO t VALUES {values}")
        except UniqueViolationError:
            assert not ok
            return
        assert ok
        self.model.update(staged)

    @rule(group=_groups, delta=st.integers(-3, 3), grow=st.integers(0, 900))
    def update_group(self, group: int, delta: int, grow: int) -> None:
        targets = [i for i, (_, g, _) in self.model.items() if g == group]
        new_ids = {i + delta for i in targets}
        others = set(self.model) - set(targets)
        # rows are updated one at a time in heap order; any id collision may fail midway
        may_fail = bool(new_ids & others) or (delta != 0 and len(targets) > 1)
        too_long = any(len(self.model[i][2]) + grow > MAX_TEXT_BYTES for i in targets)
        sql = f"UPDATE t SET id = id + {delta}, pad = pad || '{'g' * grow}' WHERE grp = {group}"
        try:
            count = self.db.execute(sql).rowcount
        except UniqueViolationError:
            assert may_fail
            return  # the invariant then checks that the statement left no trace
        except ValueTooLargeError:
            assert too_long
            return
        assert count == len(targets)
        assert not new_ids & others
        assert not too_long
        moved = {
            i + delta: (c, g, p + "g" * grow) for i, (c, g, p) in self.model.items() if g == group
        }
        for i in targets:
            del self.model[i]
        self.model.update(moved)

    @rule(low=_ids)
    def delete(self, low: int) -> None:
        count = self.db.execute(f"DELETE FROM t WHERE id >= {low} AND code IS NULL").rowcount
        doomed = [i for i, (c, _, _) in self.model.items() if i >= low and c is None]
        assert count == len(doomed)
        for i in doomed:
            del self.model[i]

    @rule()
    def reopen(self) -> None:
        self.db.close()
        self.db = Database(self.path, pool_size=8)

    @invariant()
    def matches_model_and_indexes_match_heap(self) -> None:
        got = {r[0]: (r[1], r[2], r[3]) for r in self.db.execute("SELECT * FROM t").rows}
        assert got == self.model
        assert check_table(self.db.catalog.get_table("t")) == len(self.model)
        assert self.db.pool.pinned_pages() == {}

    def teardown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


DmlModel.TestCase.settings = settings(stateful_step_count=25, deadline=None)
TestDmlModel = DmlModel.TestCase
