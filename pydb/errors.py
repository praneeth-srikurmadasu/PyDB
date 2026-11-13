"""Exception hierarchy. Every error raised by PyDB derives from :class:`PyDBError`."""


class PyDBError(Exception):
    """Base class for all PyDB errors."""


class NotSupportedError(PyDBError):
    """A valid request that this version of PyDB does not implement."""


class SqlSyntaxError(PyDBError):
    """SQL text that cannot be tokenized or parsed. Carries the 1-based line and column."""

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"SyntaxError at line {line}, col {column}: {message}")
        self.detail = message
        self.line = line
        self.column = column


class TypeMismatchError(PyDBError):
    """A value does not match the type it is being stored or compared as."""


class ValueTooLargeError(PyDBError):
    """A value is outside the range its type can represent (e.g. TEXT over its byte limit)."""


class DuplicateKeyError(PyDBError):
    """An insert would put a second entry with the same key into a unique index."""


class StorageError(PyDBError):
    """A failure in the storage layer (files, pages, rows)."""


class CorruptPageError(StorageError):
    """A page read from disk failed its checksum or structural checks."""


class RowTooLargeError(StorageError):
    """A serialized row does not fit in a single page."""


class RecordNotFoundError(StorageError):
    """A RID refers to a slot that does not exist or has been deleted."""


class BufferPoolFullError(StorageError):
    """Every frame in the buffer pool is pinned, so no page can be brought in."""
