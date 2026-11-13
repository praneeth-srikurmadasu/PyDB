"""Engine-wide constants and tunable defaults."""

from typing import Final

PAGE_SIZE: Final[int] = 4096
"""Size of every on-disk page in bytes. Page ``n`` lives at offset ``n * PAGE_SIZE``."""

HEADER_PAGE_ID: Final[int] = 0
"""Page 0 holds the database header (magic, format version, catalog root, free list)."""

INVALID_PAGE_ID: Final[int] = -1
"""Sentinel meaning "no page", e.g. the end of a linked list of pages."""

BUFFER_POOL_SIZE: Final[int] = 256
"""Default number of frames in the buffer pool (256 * 4 KiB = 1 MiB)."""

MAX_TEXT_BYTES: Final[int] = 1024
"""Largest UTF-8 encoded TEXT value that can be stored in a row."""

MAX_KEY_TEXT_BYTES: Final[int] = 64
"""Largest UTF-8 encoded TEXT value that fits in a fixed-size B+ tree key slot."""
