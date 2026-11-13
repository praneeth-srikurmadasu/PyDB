"""A reader-writer latch for short critical sections on in-memory pages."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class RWLatch:
    """Many readers or one writer. Waiting writers block new readers, so writers do not starve.

    Not reentrant: a thread that holds the latch must not acquire it again.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._writers_waiting:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            if self._readers <= 0:
                raise RuntimeError("release_read without a matching acquire_read")
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            if not self._writer:
                raise RuntimeError("release_write without a matching acquire_write")
            self._writer = False
            self._cond.notify_all()

    @contextmanager
    def read(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write(self) -> Iterator[None]:
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()
