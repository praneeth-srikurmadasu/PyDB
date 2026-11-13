# Indexing

PyDB indexes are disk-resident B+ trees (`pydb/index/bplus_tree.py`) that map a key of one
or more columns to the RID of a row. Every node is one page, accessed through the buffer
pool.

## Key encoding

`pydb/index/key_codec.py` turns a tuple of values into bytes whose byte order equals the
value order, with NULL lowest. The tree compares raw bytes and never interprets types.

| Column | Encoding |
|---|---|
| NULL | `00` |
| INT | `01` + `(v + 2^63)` as 8 bytes big-endian (the sign bit flipped) |
| FLOAT | `01` + IEEE 754 big-endian; negative values have every bit inverted, others only the sign bit flipped. `-0.0` is stored as `0.0`; NaN sorts above `+inf` |
| BOOL | `01` + `00` or `01` |
| TEXT | `01` + UTF-8 with each `00` byte written as `00 FF`, then a `00` terminator |

Examples: `-1` is `01 7F FF FF FF FF FF FF FF`, `0` is `01 80 00 00 00 00 00 00 00`;
`'ab'` is `01 61 62 00`.

Each column encoding is self-delimiting, so a composite key is a concatenation, and the
encoding of the leading columns is a byte prefix of the full key. Range scans and searches
on a prefix of the key columns use that.

## Entries

Each stored entry key is `encode_key(values) + rid_suffix`, where the suffix is the RID as
`page_id u32, slot_id u16` big-endian. Entries are therefore unique in every index, and all
entries for one key are adjacent and ordered by RID.

- **Unique index:** before inserting, the tree checks for any entry whose key starts with
  the encoded key and raises `DuplicateKeyError`. As in SQL, a key containing NULL never
  conflicts.
- **Non-unique index:** the same layout; only an identical `(key, rid)` pair is refused.

Deleting needs both the key and the RID.

## Page formats

**Header page** (`BTREE_HEADER`), the index's permanent entry point:

| Offset | Size | Field |
|---|---|---|
| 24 | 4 | root page id |
| 28 | 2 | key slot size (bytes, including the RID suffix) |
| 30 | 1 | unique flag |
| 31 | 2 | `leaf_max` |
| 33 | 2 | `internal_max` |
| 35 | 1 | column count |
| 36 | n | column types |

The root moves when the tree grows or shrinks; the header page does not, so whatever refers
to the index stores the header page id.

**Nodes** (`pydb/index/bplus_node.py`). After the common header: key count (u16), key slot
size (u16), next leaf (i32), prev leaf (i32), then entries from offset 36. A key slot is
`[u16 length][key, zero padded to the slot size]`.

- Leaf entry: `[key slot][page_id i32][slot_id u16]`. Leaves are doubly linked.
- Internal node: `[child_0 i32]` then `[key slot][child i32]` per key. Child `i` holds keys
  `k` with `keys[i-1] <= k < keys[i]`.

**Fixed-size key slots.** Every slot is as large as the largest possible key for the
index's column types (TEXT is limited to 64 bytes in a key), so a node is a plain array and
the i-th key is at a computed offset. Binary search and insertion need no per-entry
pointers. The cost is wasted space for short keys and the 64-byte TEXT limit; longer TEXT
values raise `ValueTooLargeError` when inserted into an index. An INT key uses a 15-byte
slot, so a leaf holds 176 entries and an internal node 194 keys.

## Algorithms

- **Search** descends from the root with one binary search per level, performed on the page
  bytes, then reads matching entries along the leaf chain.
- **Insert** descends, recording the path. If the leaf has room, the entry is inserted in
  place by shifting bytes. Otherwise the leaf is split in half and the first key of the
  right half is copied into the parent. An overfull internal node splits around its middle
  key, which moves up (it is not kept in either half). A root split adds a level.
- **Delete** removes the entry in place unless the leaf would fall below `leaf_max // 2`.
  Then the leaf borrows an entry from a sibling with spare entries (updating the parent's
  separator) or, failing that, merges with a sibling (removing a separator from the parent
  and freeing a page). Internal nodes rebalance the same way, rotating through the parent.
  An internal root left with one child is removed, shrinking the tree.
- **Range scan** `range_scan(low, high, low_inclusive=True, high_inclusive=True)` finds the
  first leaf for `low` and follows the leaf chain until past `high`. Either bound may be
  `None` or a prefix of the key columns.

## Invariants

`check_invariants()` verifies, and the tests call it after every operation in the
property-based tests:

1. Every leaf is at the same depth.
2. Keys are strictly increasing within each node.
3. Every node except the root holds between `max // 2` and `max` entries; an internal root
   has at least one key.
4. Every key in child `i` lies in `[keys[i-1], keys[i])` of its parent.
5. Following the leaf chain forward visits every leaf in tree order, the prev links match,
   and the entries equal the in-order traversal.

## Concurrency

One reader-writer latch per tree object: searches and scans read, inserts and deletes
write. A range scan takes the latch per leaf, so it is not isolated from a concurrent
writer; transaction locks are responsible for that. Latch crabbing (latching nodes
hand over hand) is not implemented.

## Performance

Measured on Linux with Python 3.13, a 256-frame pool: 100,000 random INT inserts take about
6.5 s and build a tree of height 3; 20,000 point searches take about 1 s. Nodes are only
decoded into Python lists for splits, merges and scans.
