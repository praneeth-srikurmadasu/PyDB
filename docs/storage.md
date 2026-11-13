# Storage

## Database file

A database is one file made of fixed-size pages (`PAGE_SIZE = 4096`). Page `n` starts at
byte offset `n * PAGE_SIZE`. `pydb.storage.disk_manager.DiskManager` is the only code that
touches the file. It opens it unbuffered in binary mode and `sync()` calls `os.fsync`.

## Page header

Every page begins with a 24-byte header (`pydb/storage/page.py`), little-endian:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `page_id` | The page's own id; a mismatch on read means a misdirected write |
| 4 | 1 | `page_type` | `EMPTY` = 0, `HEADER` = 1, `FREE` = 2 |
| 5 | 3 | reserved | |
| 8 | 8 | `page_lsn` | LSN of the last log record applied to the page |
| 16 | 4 | checksum | CRC32 (`zlib.crc32`) of bytes `[0:16]` and `[20:4096]` |
| 20 | 4 | reserved | |

The checksum is computed when a page is written and verified when it is read. A mismatch,
a wrong `page_id`, an unknown page type or a short read raises `CorruptPageError`.

## Header page (page 0)

| Offset | Size | Field |
|---|---|---|
| 24 | 8 | magic `PYDB0001` |
| 32 | 4 | format version (1) |
| 36 | 4 | page size |
| 40 | 4 | catalog root page id (`-1` = none) |
| 44 | 4 | next page id (one past the highest page ever allocated) |
| 48 | 4 | free-list head (`-1` = empty) |
| 52 | 4 | free page count |

Opening a file checks the checksum, magic, version, page size and that the file is at least
`next_page_id * PAGE_SIZE` bytes long. Page 0 cannot be written or freed through the public
API.

## Free list

Freed pages form a LIFO stack threaded through the pages themselves. A `FREE` page stores
the id of the next free page at offset 24:

```
header.free_head = 5  ->  page 5 (next = 3)  ->  page 3 (next = -1)
```

`allocate_page()` pops the head if there is one, otherwise it extends the file. Either way
the page is rewritten as a zeroed `EMPTY` page. `deallocate_page()` pushes; freeing a page
that is already `FREE` raises `StorageError`. Both operations are O(1) and need no extra
space.

**Limit:** an allocation or free writes two pages (the page and the header page) without a
log, so a crash between the two writes can leak a page. The write-ahead log will make these
changes atomic.

## Counters

`num_reads` and `num_writes` count physical page reads and writes, including the header
page.
