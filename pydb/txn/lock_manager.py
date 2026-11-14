"""Table locks for strict two-phase locking, with deadlock detection.

Every statement locks the tables it touches before it runs: SHARED to read, EXCLUSIVE to
write or change the schema. Locks are held until the transaction commits or rolls back
(*strict* 2PL), which makes every schedule of committed transactions serializable: no
transaction can see another's uncommitted rows, and because whole tables are locked there
are no phantoms either.

**Granting.** A request is granted when it is compatible with every other holder (only
SHARED with SHARED is compatible) and no incompatible request is queued ahead of it, so a
waiting writer is not starved by a stream of new readers. A holder of SHARED asking for
EXCLUSIVE is an *upgrade*; upgrades queue ahead of ordinary requests, since they already
hold part of the lock.

**Deadlocks.** Before a request waits, the manager follows the waits-for graph from the
requesting transaction: an edge ``A -> B`` means A is waiting for a lock that B holds in an
incompatible mode, or that B has queued ahead of A. If the walk returns to the requester,
waiting would deadlock, so the request fails at once with :class:`DeadlockError` and the
requester is the victim. Checking only when a transaction starts to wait is enough: a
cycle can only close when some transaction starts waiting, because a transaction that has
just been granted a lock is running, not waiting.

**Timeouts.** A wait longer than the timeout fails with :class:`LockTimeoutError`. Locks
are never released on their own; the caller rolls the transaction back and calls
:meth:`LockManager.release_all`.
"""

import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass, field
from enum import Enum

from pydb.errors import DeadlockError, LockTimeoutError, TransactionError


class LockMode(Enum):
    SHARED = "S"
    EXCLUSIVE = "X"

    def compatible_with(self, other: "LockMode") -> bool:
        return self is LockMode.SHARED and other is LockMode.SHARED

    def covers(self, other: "LockMode") -> bool:
        """Whether holding ``self`` already grants everything ``other`` would."""
        return self is LockMode.EXCLUSIVE or other is LockMode.SHARED


@dataclass(eq=False, slots=True)
class _Request:
    txn_id: int
    mode: LockMode
    upgrade: bool


@dataclass(slots=True)
class _Lock:
    holders: dict[int, LockMode] = field(default_factory=dict)
    queue: list[_Request] = field(default_factory=list)  # waiting requests, in grant order


class LockManager:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout
        self._locks: dict[Hashable, _Lock] = {}
        self._held: dict[int, set[Hashable]] = {}
        self._waiting: dict[int, tuple[Hashable, _Request]] = {}
        self._condition = threading.Condition(threading.Lock())
        self._closed = False
        self.deadlocks = 0
        self.timeouts = 0

    # ----------------------------------------------------------------- acquiring

    def acquire(
        self, txn_id: int, resource: Hashable, mode: LockMode, *, timeout: float | None = None
    ) -> None:
        """Grant ``mode`` on ``resource`` to ``txn_id``, waiting if necessary.

        ``timeout`` overrides the manager's default; ``0`` means never wait.
        """
        limit = self.timeout if timeout is None else timeout
        with self._condition:
            self._check_open()
            lock = self._locks.setdefault(resource, _Lock())
            held = lock.holders.get(txn_id)
            if held is not None and held.covers(mode):
                return
            request = _Request(txn_id, mode, upgrade=held is not None)
            if request.upgrade:  # after earlier upgrades, before ordinary requests
                position = sum(1 for queued in lock.queue if queued.upgrade)
                lock.queue.insert(position, request)
            else:
                lock.queue.append(request)
            if self._grantable(lock, request):
                self._grant(resource, lock, request)
                return
            if limit is not None and limit <= 0:
                lock.queue.remove(request)
                self._forget_if_unused(resource, lock)
                raise LockTimeoutError(f"{mode.name} lock on {_describe(resource)} is not free")
            self._waiting[txn_id] = (resource, request)
            try:
                if self._closes_cycle(txn_id):
                    self.deadlocks += 1
                    raise DeadlockError(
                        f"deadlock detected: transaction {txn_id} waiting for "
                        f"{mode.name} lock on {_describe(resource)} would wait forever"
                    )
                deadline = None if limit is None else time.monotonic() + limit
                while not self._grantable(lock, request):
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self.timeouts += 1
                        raise LockTimeoutError(
                            f"timed out after {limit:g} s waiting for {mode.name} lock on "
                            f"{_describe(resource)}"
                        )
                    self._condition.wait(remaining)
                    self._check_open()
            except BaseException:
                lock.queue.remove(request)
                self._condition.notify_all()  # requests queued behind this one may proceed
                self._forget_if_unused(resource, lock)
                raise
            finally:
                del self._waiting[txn_id]
            self._grant(resource, lock, request)

    def try_acquire(self, txn_id: int, resource: Hashable, mode: LockMode) -> bool:
        """Grant the lock if that needs no waiting; otherwise change nothing."""
        try:
            self.acquire(txn_id, resource, mode, timeout=0)
        except (DeadlockError, LockTimeoutError):
            return False
        return True

    # ----------------------------------------------------------------- releasing

    def release_all(self, txn_id: int) -> None:
        """Release every lock ``txn_id`` holds (at commit or rollback)."""
        with self._condition:
            for resource in self._held.pop(txn_id, set()):
                lock = self._locks[resource]
                del lock.holders[txn_id]
                self._forget_if_unused(resource, lock)
            self._condition.notify_all()

    def close(self) -> None:
        """Fail every current and future wait (the database is closing)."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    # ----------------------------------------------------------------- inspection

    def held_by(self, txn_id: int) -> dict[Hashable, LockMode]:
        with self._condition:
            return {r: self._locks[r].holders[txn_id] for r in self._held.get(txn_id, ())}

    def holders(self, resource: Hashable) -> dict[int, LockMode]:
        with self._condition:
            lock = self._locks.get(resource)
            return {} if lock is None else dict(lock.holders)

    def waiting(self) -> dict[int, tuple[Hashable, LockMode]]:
        """Transactions currently waiting, with what they wait for."""
        with self._condition:
            return {t: (r, q.mode) for t, (r, q) in self._waiting.items()}

    def check_invariants(self) -> None:
        with self._condition:
            for resource, lock in self._locks.items():
                modes = list(lock.holders.values())
                if LockMode.EXCLUSIVE in modes and len(modes) > 1:
                    raise AssertionError(f"{resource!r}: EXCLUSIVE shared with {lock.holders}")
                for txn_id in lock.holders:
                    if resource not in self._held.get(txn_id, ()):
                        raise AssertionError(f"{resource!r}: holder {txn_id} not indexed")
            for txn_id, resources in self._held.items():
                for resource in resources:
                    if txn_id not in self._locks[resource].holders:
                        raise AssertionError(f"txn {txn_id} indexed on {resource!r}")

    # ----------------------------------------------------------------- internals

    def _grantable(self, lock: _Lock, request: _Request) -> bool:
        for txn_id, mode in lock.holders.items():
            if txn_id != request.txn_id and not mode.compatible_with(request.mode):
                return False
        for queued in lock.queue:
            if queued is request:
                return True
            if queued.txn_id != request.txn_id and not queued.mode.compatible_with(request.mode):
                return False
        raise AssertionError("request is not queued")

    def _grant(self, resource: Hashable, lock: _Lock, request: _Request) -> None:
        lock.queue.remove(request)
        lock.holders[request.txn_id] = request.mode
        self._held.setdefault(request.txn_id, set()).add(resource)
        self._condition.notify_all()  # compatible requests queued behind may now be granted

    def _blockers(self, txn_id: int) -> set[int]:
        """The transactions ``txn_id`` is waiting for (its out-edges in the waits-for graph)."""
        resource, request = self._waiting[txn_id]
        lock = self._locks[resource]
        blockers = {
            holder
            for holder, mode in lock.holders.items()
            if holder != txn_id and not mode.compatible_with(request.mode)
        }
        for queued in lock.queue:
            if queued is request:
                break
            if queued.txn_id != txn_id and not queued.mode.compatible_with(request.mode):
                blockers.add(queued.txn_id)
        return blockers

    def _closes_cycle(self, start: int) -> bool:
        stack = list(self._blockers(start))
        seen: set[int] = set()
        while stack:
            txn_id = stack.pop()
            if txn_id == start:
                return True
            if txn_id in seen or txn_id not in self._waiting:
                continue
            seen.add(txn_id)
            stack.extend(self._blockers(txn_id))
        return False

    def _forget_if_unused(self, resource: Hashable, lock: _Lock) -> None:
        if not lock.holders and not lock.queue:
            del self._locks[resource]

    def _check_open(self) -> None:
        if self._closed:
            raise TransactionError("the database is closing")


def _describe(resource: Hashable) -> str:
    if isinstance(resource, tuple) and len(resource) == 2 and resource[0] == "table":
        return f'table "{resource[1]}"'
    return repr(resource)
