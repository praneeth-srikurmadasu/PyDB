"""Exception hierarchy. Every error raised by PyDB derives from :class:`PyDBError`."""


class PyDBError(Exception):
    """Base class for all PyDB errors."""


class NotSupportedError(PyDBError):
    """A valid request that this version of PyDB does not implement."""


class StorageError(PyDBError):
    """A failure in the storage layer (files, pages, rows)."""


class CorruptPageError(StorageError):
    """A page read from disk failed its checksum or structural checks."""


class RowTooLargeError(StorageError):
    """A serialized row does not fit in a single page."""
