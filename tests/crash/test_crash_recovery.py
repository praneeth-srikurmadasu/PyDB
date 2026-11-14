"""Kill a process running transactions, then check what recovery brings back.

After every crash the parent reopens the database (running recovery) and checks:

* every transaction the child reported as committed is fully present;
* no rolled-back or unreported transaction is present, even partly. The one transaction
  in flight at the crash may have committed (if COMMIT became durable just before the kill)
  and is then accepted, but only completely;
* every index matches its table's heap exactly, and every B+ tree passes its invariant
  checks.

``PYDB_CRASH_ROUNDS`` sets how many crashes each test causes (default 6) and
``PYDB_CRASH_SEED`` varies where the injected crashes land (default 0).
"""

import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pydb.catalog.integrity import check_table
from pydb.database import Database

WORKLOAD = Path(__file__).with_name("workload.py")
ROUNDS = int(os.environ.get("PYDB_CRASH_ROUNDS", "6"))
SEED = int(os.environ.get("PYDB_CRASH_SEED", "0"))


def child(
    db: Path, *args: str, fault: str | None = None, kill_after: float | None = None
) -> list[str]:
    env = dict(os.environ)
    env.pop("PYDB_FAULT", None)
    if fault is not None:
        env["PYDB_FAULT"] = fault
    process = subprocess.Popen(
        [sys.executable, "-u", str(WORKLOAD), str(db), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if kill_after is not None:
        time.sleep(kill_after)
        process.kill()  # abrupt termination, on Windows and Linux alike
    out, err = process.communicate(timeout=180)
    assert "Traceback" not in err, err
    return out.splitlines()


class Model:
    """What the database must contain, given which transactions committed."""

    def __init__(self) -> None:
        self.committed: set[int] = set()
        self.next_k = 1

    def read(self, lines: list[str]) -> int | None:
        """Record the child's output; return the transaction left in flight, if any."""
        pending = None
        for line in lines:
            word, _, number = line.partition(" ")
            if word == "BEGIN":
                pending = int(number)
                self.next_k = max(self.next_k, pending + 1)
            elif word == "COMMITTED":
                self.committed.add(int(number))
                pending = None
            elif word == "ROLLED":
                pending = None
        return pending

    @staticmethod
    def expected(committed: set[int]) -> tuple[set[tuple[int, int]], dict[int, int]]:
        items = {(10 * k + i, k) for k in committed if k + 15 not in committed for i in range(3)}
        counters = {c: sum(1 for k in committed if k % 5 == c) for c in range(5)}
        return items, counters

    def verify(self, path: Path, pending: int | None) -> None:
        with Database(path) as db:
            items = {
                (int(str(r[0])), int(str(r[1])))
                for r in db.execute("SELECT id, txn FROM items").rows
            }
            counters = {
                int(str(r[0])): int(str(r[1]))
                for r in db.execute("SELECT id, n FROM counters").rows
            }
            db.pool.check_invariants()
            assert check_table(db.catalog.get_table("items")) == len(items)
            assert check_table(db.catalog.get_table("counters")) == 5
            db.pool.check_invariants()
        if (items, counters) == self.expected(self.committed):
            return
        if pending is not None and (items, counters) == self.expected(self.committed | {pending}):
            self.committed.add(pending)  # its COMMIT was durable before the crash
            return
        want_items, want_counters = self.expected(self.committed)
        pytest.fail(
            f"state after recovery is wrong (pending={pending}): "
            f"missing {sorted(want_items - items)[:10]}, "
            f"unexpected {sorted(items - want_items)[:10]}, "
            f"counters {counters} != {want_counters}"
        )


@pytest.fixture
def path(tmp_path: Path) -> Path:
    p = tmp_path / "crash.db"
    with Database(p) as db:
        db.execute_script(
            "CREATE TABLE items (id INT PRIMARY KEY, txn INT, pad TEXT);"
            "CREATE INDEX items_txn ON items (txn);"
            "CREATE TABLE counters (id INT PRIMARY KEY, n INT);"
            "INSERT INTO counters VALUES (0, 0), (1, 0), (2, 0), (3, 0), (4, 0);"
        )
    return p


def test_random_kills(path: Path) -> None:
    rng = random.Random(SEED + 1)
    model = Model()
    for _ in range(ROUNDS):
        lines = child(path, "run", str(model.next_k), "100000", kill_after=rng.uniform(0.6, 2.5))
        pending = model.read(lines)
        model.verify(path, pending)
    assert len(model.committed) > 5  # the workload made real progress


@pytest.mark.parametrize("point", ["log_flush", "log_flushed"])
def test_crash_during_commit(path: Path, point: str) -> None:
    rng = random.Random(f"{SEED}-{point}")
    model = Model()
    for _ in range(ROUNDS):
        lines = child(
            path, "run", str(model.next_k), "100000", fault=f"{point}:{rng.randint(2, 40)}"
        )
        model.verify(path, model.read(lines))


def test_torn_page_writes(path: Path) -> None:
    rng = random.Random(SEED + 7)
    model = Model()
    for _ in range(ROUNDS):
        lines = child(
            path, "run", str(model.next_k), "100000", fault=f"page_write:{rng.randint(5, 120)}"
        )
        model.verify(path, model.read(lines))


def test_crash_during_recovery(path: Path) -> None:
    model = Model()
    model.verify(path, model.read(child(path, "run", "1", "20")))  # some committed history
    lines = child(path, "abandon", "1000", "400")  # a big transaction that never commits
    assert lines[-1] == "ABANDONED 1000"
    for step in (1, 3, 10, 40, 150):
        # recovery has to undo 400 inserts; die part-way through, again and again
        child(path, "open", fault=f"undo_step:{step}")
    child(path, "open", fault="page_write:3")  # and once while writing pages
    model.verify(path, pending=None)
