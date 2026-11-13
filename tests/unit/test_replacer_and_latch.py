import threading
import time

import pytest

from pydb.storage.latch import RWLatch
from pydb.storage.replacer import LRUReplacer

# ------------------------------------------------------------------ LRU replacer


def test_victims_come_out_in_unpin_order() -> None:
    r = LRUReplacer()
    for frame in (3, 1, 2):
        r.unpin(frame)
    assert len(r) == 3
    assert [r.victim(), r.victim(), r.victim()] == [3, 1, 2]
    assert r.victim() is None


def test_pinned_frames_are_never_victims() -> None:
    r = LRUReplacer()
    r.unpin(1)
    r.unpin(2)
    r.pin(1)
    assert r.victim() == 2
    assert r.victim() is None


def test_unpinning_twice_keeps_the_original_position() -> None:
    r = LRUReplacer()
    r.unpin(1)
    r.unpin(2)
    r.unpin(1)
    assert r.victim() == 1


def test_repin_then_unpin_moves_to_the_back() -> None:
    r = LRUReplacer()
    r.unpin(1)
    r.unpin(2)
    r.pin(1)
    r.unpin(1)
    assert [r.victim(), r.victim()] == [2, 1]


def test_pin_of_unknown_frame_is_harmless() -> None:
    r = LRUReplacer()
    r.pin(42)
    assert len(r) == 0


# ------------------------------------------------------------------ reader-writer latch


def test_readers_share_the_latch() -> None:
    latch = RWLatch()
    inside = threading.Barrier(3, timeout=5)

    def reader() -> None:
        with latch.read():
            inside.wait()  # all three readers must be inside at once

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    with latch.read():
        inside.wait()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()


def test_writer_excludes_readers_and_writers() -> None:
    latch = RWLatch()
    events: list[str] = []
    latch.acquire_write()

    def reader() -> None:
        with latch.read():
            events.append("reader")

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    events.append("writer done")
    latch.release_write()
    t.join(timeout=5)
    assert events == ["writer done", "reader"]


def test_waiting_writer_blocks_new_readers() -> None:
    latch = RWLatch()
    events: list[str] = []
    latch.acquire_read()

    def writer() -> None:
        with latch.write():
            events.append("writer")

    def late_reader() -> None:
        with latch.read():
            events.append("late reader")

    w = threading.Thread(target=writer)
    w.start()
    time.sleep(0.05)  # writer is now waiting
    r = threading.Thread(target=late_reader)
    r.start()
    time.sleep(0.05)
    assert events == []
    latch.release_read()
    w.join(timeout=5)
    r.join(timeout=5)
    assert events == ["writer", "late reader"]


def test_unbalanced_release_is_an_error() -> None:
    latch = RWLatch()
    with pytest.raises(RuntimeError):
        latch.release_read()
    with pytest.raises(RuntimeError):
        latch.release_write()


def test_concurrent_writers_do_not_lose_updates() -> None:
    latch = RWLatch()
    counter = [0]

    def work() -> None:
        for _ in range(2000):
            with latch.write():
                value = counter[0]
                counter[0] = value + 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter[0] == 8000
