import threading
import time
from collections.abc import Callable

import pytest

from pydb.errors import DeadlockError, LockTimeoutError, TransactionError
from pydb.txn.lock_manager import LockManager, LockMode

S = LockMode.SHARED
X = LockMode.EXCLUSIVE
T = ("table", "t")
U = ("table", "u")


class Waiter:
    """Run a blocking call on a thread and observe whether it has returned."""

    def __init__(self, call: Callable[[], None]) -> None:
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(call,), daemon=True)
        self.thread.start()

    def _run(self, call: Callable[[], None]) -> None:
        try:
            call()
        except BaseException as exc:
            self.error = exc

    def blocked(self, wait: float = 0.15) -> bool:
        self.thread.join(wait)
        return self.thread.is_alive()

    def finish(self) -> None:
        self.thread.join(5)
        assert not self.thread.is_alive(), "still waiting"
        if self.error is not None:
            raise self.error


def wait_until_waiting(locks: LockManager, txn_id: int) -> None:
    deadline = time.monotonic() + 5
    while txn_id not in locks.waiting():
        assert time.monotonic() < deadline, f"txn {txn_id} never started waiting"
        time.sleep(0.005)


def test_compatibility_matrix() -> None:
    assert S.compatible_with(S)
    assert not S.compatible_with(X)
    assert not X.compatible_with(S)
    assert not X.compatible_with(X)
    assert X.covers(S) and X.covers(X) and S.covers(S) and not S.covers(X)


def test_shared_locks_are_shared() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    locks.acquire(2, T, S)
    assert locks.holders(T) == {1: S, 2: S}
    locks.check_invariants()


def test_exclusive_excludes_everyone() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    assert not locks.try_acquire(2, T, S)
    assert not locks.try_acquire(2, T, X)
    assert locks.holders(T) == {1: X}
    assert locks.waiting() == {}


def test_reacquiring_a_held_or_weaker_mode_is_a_no_op() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    locks.acquire(1, T, S)
    locks.acquire(1, T, X)
    assert locks.held_by(1) == {T: X}


def test_waiter_is_granted_when_holder_releases() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    waiter = Waiter(lambda: locks.acquire(2, T, S))
    assert waiter.blocked()
    locks.release_all(1)
    waiter.finish()
    assert locks.holders(T) == {2: S}


def test_release_all_releases_every_resource() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    locks.acquire(1, U, S)
    locks.release_all(1)
    assert locks.held_by(1) == {}
    assert locks.holders(T) == {} and locks.holders(U) == {}
    locks.check_invariants()


def test_upgrade_when_sole_holder_is_immediate() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    locks.acquire(1, T, X)
    assert locks.holders(T) == {1: X}


def test_upgrade_waits_for_other_readers() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    locks.acquire(2, T, S)
    waiter = Waiter(lambda: locks.acquire(1, T, X))
    assert waiter.blocked()
    locks.release_all(2)
    waiter.finish()
    assert locks.holders(T) == {1: X}


def test_queued_writer_is_not_starved_by_new_readers() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    writer = Waiter(lambda: locks.acquire(2, T, X))
    wait_until_waiting(locks, 2)
    # A new reader is compatible with the holder but must queue behind the writer.
    reader = Waiter(lambda: locks.acquire(3, T, S))
    assert reader.blocked()
    locks.release_all(1)
    writer.finish()
    assert reader.blocked()
    locks.release_all(2)
    reader.finish()
    assert locks.holders(T) == {3: S}


def test_upgrade_jumps_ahead_of_queued_requests() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    locks.acquire(2, T, S)
    writer = Waiter(lambda: locks.acquire(3, T, X))
    wait_until_waiting(locks, 3)
    upgrade = Waiter(lambda: locks.acquire(1, T, X))
    wait_until_waiting(locks, 1)
    locks.release_all(2)
    upgrade.finish()  # granted before the earlier-queued writer
    assert writer.blocked()
    locks.release_all(1)
    writer.finish()


def test_two_transaction_deadlock_is_detected() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    locks.acquire(2, U, X)
    first = Waiter(lambda: locks.acquire(1, U, X))
    wait_until_waiting(locks, 1)
    with pytest.raises(DeadlockError, match="deadlock"):
        locks.acquire(2, T, X)  # closes the cycle: 2 is the victim
    assert locks.deadlocks == 1
    assert first.blocked()
    locks.release_all(2)  # the victim rolls back
    first.finish()
    assert locks.holders(U) == {1: X}
    locks.check_invariants()


def test_upgrade_deadlock_is_detected() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    locks.acquire(2, T, S)
    first = Waiter(lambda: locks.acquire(1, T, X))
    wait_until_waiting(locks, 1)
    with pytest.raises(DeadlockError):
        locks.acquire(2, T, X)
    locks.release_all(2)
    first.finish()


def test_three_transaction_cycle_is_detected() -> None:
    locks = LockManager()
    a, b, c = ("table", "a"), ("table", "b"), ("table", "c")
    locks.acquire(1, a, X)
    locks.acquire(2, b, X)
    locks.acquire(3, c, X)
    one = Waiter(lambda: locks.acquire(1, b, X))
    wait_until_waiting(locks, 1)
    two = Waiter(lambda: locks.acquire(2, c, X))
    wait_until_waiting(locks, 2)
    with pytest.raises(DeadlockError):
        locks.acquire(3, a, S)
    locks.release_all(3)
    two.finish()
    locks.release_all(2)
    one.finish()


def test_waiting_behind_a_waiter_without_a_cycle_is_not_a_deadlock() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    second = Waiter(lambda: locks.acquire(2, T, X))
    wait_until_waiting(locks, 2)
    third = Waiter(lambda: locks.acquire(3, T, X))
    assert third.blocked()
    locks.release_all(1)
    second.finish()
    locks.release_all(2)
    third.finish()
    assert locks.deadlocks == 0


def test_timeout() -> None:
    locks = LockManager(timeout=0.1)
    locks.acquire(1, T, X)
    started = time.monotonic()
    with pytest.raises(LockTimeoutError, match="timed out"):
        locks.acquire(2, T, S)
    assert time.monotonic() - started >= 0.09
    assert locks.timeouts == 1
    assert locks.waiting() == {}
    locks.release_all(1)
    locks.acquire(2, T, X)  # the failed request left nothing behind


def test_a_timed_out_writer_unblocks_readers_queued_behind_it() -> None:
    locks = LockManager()
    locks.acquire(1, T, S)
    writer = Waiter(lambda: locks.acquire(2, T, X, timeout=0.2))
    wait_until_waiting(locks, 2)
    reader = Waiter(lambda: locks.acquire(3, T, S))
    with pytest.raises(LockTimeoutError):
        writer.finish()
    reader.finish()
    assert locks.holders(T) == {1: S, 3: S}


def test_close_fails_waiters() -> None:
    locks = LockManager()
    locks.acquire(1, T, X)
    waiter = Waiter(lambda: locks.acquire(2, T, S))
    wait_until_waiting(locks, 2)
    locks.close()
    with pytest.raises(TransactionError, match="closing"):
        waiter.finish()
    with pytest.raises(TransactionError):
        locks.acquire(3, U, S)


def test_many_threads_hammering_one_lock_keep_invariants() -> None:
    locks = LockManager()
    errors: list[BaseException] = []
    inside: list[int] = []
    guard = threading.Lock()

    def worker(txn_id: int) -> None:
        try:
            for i in range(50):
                mode = X if (txn_id + i) % 3 == 0 else S
                locks.acquire(txn_id, T, mode)
                with guard:
                    inside.append(txn_id)
                    holders = locks.holders(T)
                    if mode is X:
                        assert holders == {txn_id: X}
                    locks.check_invariants()
                locks.release_all(txn_id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert len(inside) == 8 * 50
    assert locks.holders(T) == {}
