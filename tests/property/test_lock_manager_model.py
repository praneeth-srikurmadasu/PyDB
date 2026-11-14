"""Random lock requests and releases from many transactions, against a simple model.

``try_acquire`` never waits, so the run is single-threaded and deterministic. The model
grants a request exactly when it is compatible with every other holder (with no waiters,
FIFO order plays no part).
"""

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from pydb.txn.lock_manager import LockManager, LockMode

TXNS = st.integers(1, 5)
RESOURCES = st.sampled_from([("table", "a"), ("table", "b"), ("table", "c")])
MODES = st.sampled_from([LockMode.SHARED, LockMode.EXCLUSIVE])


class LockManagerModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.locks = LockManager()
        self.model: dict[tuple[str, str], dict[int, LockMode]] = {}

    @rule(txn=TXNS, resource=RESOURCES, mode=MODES)
    def request(self, txn: int, resource: tuple[str, str], mode: LockMode) -> None:
        holders = self.model.setdefault(resource, {})
        held = holders.get(txn)
        if held is not None and held.covers(mode):
            expected = True
        else:
            expected = all(m.compatible_with(mode) for t, m in holders.items() if t != txn)
        assert self.locks.try_acquire(txn, resource, mode) == expected
        if expected and not (held is not None and held.covers(mode)):
            holders[txn] = mode

    @rule(txn=TXNS)
    def release(self, txn: int) -> None:
        self.locks.release_all(txn)
        for holders in self.model.values():
            holders.pop(txn, None)

    @invariant()
    def matches_model(self) -> None:
        self.locks.check_invariants()
        assert self.locks.waiting() == {}
        for resource, holders in self.model.items():
            assert self.locks.holders(resource) == holders
        for txn in range(1, 6):
            expected = {r: h[txn] for r, h in self.model.items() if txn in h}
            assert self.locks.held_by(txn) == expected


TestLockManagerModel = LockManagerModel.TestCase
