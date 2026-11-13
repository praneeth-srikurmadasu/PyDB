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
| 4 | 1 | `page_type` | `EMPTY` = 0, `HEADER` = 1, `FREE` = 2, `TABLE_HEAP` = 3, `BTREE_HEADER` = 4, `BTREE_INTERNAL` = 5, `BTREE_LEAF` = 6 (see indexing.md) |
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

## Types

| Type | Storage | Python value |
|---|---|---|
| `INT` | 8 bytes, `<q` | `int` in the signed 64-bit range (`bool` is rejected) |
| `FLOAT` | 8 bytes, `<d` | `float`; an `int` is accepted and stored as a float |
| `BOOL` | 1 byte | `bool` |
| `TEXT` | `[u16 length][UTF-8]` | `str` of at most 1024 bytes in UTF-8 |

`None` is SQL NULL and is recorded in the row's null bitmap (`pydb/types.py`).

## Row format

`pydb/storage/tuple.py`, `RowLayout(types)`:

```
[null bitmap][fixed-width columns][TEXT offsets][TEXT data]
```

- **Null bitmap:** `ceil(n / 8)` bytes, bit `i` (LSB first) set when column `i` is NULL.
- **Fixed-width columns:** every INT, FLOAT and BOOL column in schema order. A NULL column
  keeps its slot, so a column's offset depends only on the schema.
- **TEXT offsets:** one u16 per TEXT column, the offset of its data from the row start.
- **TEXT data:** `[u16 length][bytes]` per non-NULL TEXT column.

Example, schema `(INT, TEXT, BOOL)`, row `(7, 'hi', NULL)`, 16 bytes:

```
04 | 07 00 00 00 00 00 00 00 | 00 | 0c 00 | 02 00 68 69
```

## Slotted pages

`pydb/storage/slotted_page.py`. After the common header:

| Offset | Size | Field |
|---|---|---|
| 24 | 2 | slot count |
| 26 | 2 | free pointer (start of the tuple area; 4096 when empty) |
| 28 | 4 | next page id |
| 32 | 4 | prev page id |
| 36 | 4 per slot | slot array: `(offset u16, length u16)` |

The slot array grows forward and tuples grow backward from the end of the page. The largest
tuple is `4096 - 36 - 4 = 4056` bytes; anything larger raises `RowTooLargeError`.

- **Delete** sets the slot's offset to 0 (a tombstone).
- **Update** overwrites in place when the new tuple is not longer; otherwise it moves the
  tuple within the page if the page has room after compaction, and reports failure if not.
- **Compact** packs live tuples against the end of the page. Slot ids do not change.
- **Slot ids are never reused**, so a RID always means the same row. A deleted row's RID
  must stay unclaimed until its delete can no longer be rolled back. The cost is 4 bytes
  per dead slot.

## Table heap

`pydb/storage/table_heap.py`. All page access goes through the buffer pool. A table is a
doubly linked list of slotted pages starting at
`first_page_id`. A row is addressed by a `RID(page_id, slot_id)`.

- **Insert** starts at an in-memory hint (the last page that accepted a row), walks forward
  along the chain, and appends a page at the tail when every page from the hint on is full.
- **Update** stays in place when the row fits on its page. Otherwise it inserts the new
  version, deletes the old one and returns the new RID; the caller updates the indexes.
- **Scan** yields live rows page by page, in slot order.

**Limits:** the hint only moves forward, so space freed on pages before the hint is reused
only after the table is reopened. Empty pages stay in the chain. Pages reach the disk in
whatever order the buffer pool evicts or flushes them, so a crash can leave the chain
inconsistent until the write-ahead log makes multi-page changes atomic.

## Buffer pool

`pydb/storage/buffer_pool.py`, `BufferPoolManager(disk, pool_size=256, log_flusher=None)`.

```
page_table:  {page_id -> frame_id}
frames[i]:   data (4096 bytes) | page_id | pin_count | dirty | reader-writer latch
free_frames: frames that have never held a page (or whose page was deleted)
replacer:    LRU order over frames with pin_count == 0   (pydb/storage/replacer.py)
```

- **`fetch_page`** pins a resident page (a hit) or loads it into a free frame, evicting the
  least recently unpinned frame if necessary (a miss). A dirty victim is written first.
  When every frame is pinned it raises `BufferPoolFullError`.
- **`new_page`** allocates a disk page and pins a zeroed `EMPTY` copy of it.
- **`unpin_page(page_id, dirty)`** drops one pin and ORs in the dirty flag. At zero pins the
  frame becomes an eviction candidate.
- **`page(page_id, write=False)`** and **`create_page()`** are context managers that pin,
  take the frame latch, and on exit release the latch and unpin (dirty if writing), even
  when the block raises. Code should use these rather than pairing fetch and unpin by hand.
- **`flush_page`**, **`flush_all`** write dirty pages; **`delete_page`** drops an unpinned
  page and returns it to the disk free list.
- **Write-ahead rule:** before writing a dirty page, the pool calls
  `log_flusher(page_lsn)`. If the call raises, the page stays dirty and resident.
- **Policy:** steal (a dirty page of unfinished work may be evicted) and no-force (nothing is
  written at commit). Changes not yet flushed are lost if the process stops without
  `flush_all()`; the write-ahead log will make that safe.

**Statistics:** `stats` returns hits, misses, evictions, writes and `hit_rate`;
`pinned_pages()` reports pin counts, and is empty when no pins have leaked.

**Latching:** the pool latch (a `threading.Lock`) protects the page table, frame metadata,
the replacer, the statistics and all disk I/O. Each frame's `RWLatch`
(`pydb/storage/latch.py`) protects its bytes: many readers or one writer, and a waiting
writer blocks new readers. The lock order is always frame latch, then pool latch. Eviction
only touches unpinned frames, which no thread can be holding a latch on, so the pool never
waits for a frame latch while holding its own.

## Counters

`num_reads` and `num_writes` count physical page reads and writes, including the header
page.
