"""Random transactions with crashes, against a model of committed and in-flight state.

Rules mix BEGIN, INSERT, UPDATE, DELETE, COMMIT, ROLLBACK, checkpoints and crashes
(``simulate_crash`` loses the unflushed log tail and unwritten dirty pages). The model keeps
the committed rows and, during a transaction, the transaction's own view. After a crash
the reopened database must equal the committed rows exactly, with every index matching
its heap.
"""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pydb.catalog.integrity import check_table
from pydb.database import Database
from pydb.errors import UniqueViolationError

_ids = st.integers(0, 40)


class CrashModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "model.db"
        with Database(self.path) as db:
            db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT, pad TEXT)")
            db.execute("CREATE INDEX t_v ON t (v)")
        self.db = Database(self.path, pool_size=4)
        self.committed: dict[int, tuple[int, int]] = {}  # id -> (v, pad length)
        self.view: dict[int, tuple[int, int]] | None = None  # the open transaction's view

    @property
    def current(self) -> dict[int, tuple[int, int]]:
        return self.committed if self.view is None else self.view

    def _apply(self, change: dict[int, tuple[int, int]]) -> None:
        if self.view is None:
            self.committed = change
        else:
            self.view = change

    @precondition(lambda self: self.view is None)
    @rule()
    def begin(self) -> None:
        self.db.execute("BEGIN")
        self.view = dict(self.committed)

    @rule(ids=st.lists(_ids, min_size=1, max_size=5), v=st.integers(0, 3), pad=st.integers(0, 700))
    def insert(self, ids: list[int], v: int, pad: int) -> None:
        values = ", ".join(f"({i}, {v}, '{'x' * pad}')" for i in ids)
        clash = len(set(ids)) < len(ids) or any(i in self.current for i in ids)
        try:
            self.db.execute(f"INSERT INTO t VALUES {values}")
        except UniqueViolationError:
            assert clash
            return
        assert not clash
        self._apply({**self.current, **{i: (v, pad) for i in ids}})

    @rule(v=st.integers(0, 3), grow=st.integers(0, 300))
    def update(self, v: int, grow: int) -> None:
        targets = [
            i for i, (old_v, pad) in self.current.items() if old_v == v and pad + grow <= 1024
        ]
        if len(targets) != sum(1 for old_v, _ in self.current.values() if old_v == v):
            return  # some row would grow past the TEXT limit; covered elsewhere
        sql = f"UPDATE t SET v = v + 1, pad = pad || '{'y' * grow}' WHERE v = {v}"
        assert self.db.execute(sql).rowcount == len(targets)
        change = dict(self.current)
        for i in targets:
            change[i] = (v + 1, change[i][1] + grow)
        self._apply(change)

    @rule(low=_ids)
    def delete(self, low: int) -> None:
        count = self.db.execute(f"DELETE FROM t WHERE id >= {low}").rowcount
        assert count == sum(1 for i in self.current if i >= low)
        self._apply({i: row for i, row in self.current.items() if i < low})

    @precondition(lambda self: self.view is not None)
    @rule()
    def commit(self) -> None:
        self.db.execute("COMMIT")
        assert self.view is not None
        self.committed, self.view = self.view, None

    @precondition(lambda self: self.view is not None)
    @rule()
    def rollback(self) -> None:
        self.db.execute("ROLLBACK")
        self.view = None

    @rule()
    def checkpoint(self) -> None:
        self.db.checkpoint()

    @rule(flush=st.booleans())
    def crash(self, flush: bool) -> None:
        if flush:
            self.db.log.flush()  # even durable log records of an open transaction must vanish
        self.db.simulate_crash()
        self.db = Database(self.path, pool_size=4)
        self.view = None

    @invariant()
    def matches_model(self) -> None:
        got = {
            int(str(r[0])): (int(str(r[1])), len(str(r[2])))
            for r in self.db.execute("SELECT id, v, pad FROM t").rows
        }
        assert got == self.current
        assert check_table(self.db.catalog.get_table("t")) == len(got)

    def teardown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


CrashModel.TestCase.settings = settings(stateful_step_count=30, deadline=None)
TestCrashModel = CrashModel.TestCase
