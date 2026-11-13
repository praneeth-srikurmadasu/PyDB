"""On-page layout of B+ tree nodes.

After the 24-byte common page header, both node kinds start with::

    offset  size  field
    24      2     key_count  (u16)
    26      2     key_size   (u16)  bytes reserved per key; every key slot has this size
    28      4     next_leaf  (i32)  leaves only, INVALID_PAGE_ID otherwise
    32      4     prev_leaf  (i32)  leaves only, INVALID_PAGE_ID otherwise
    36            entries

A key slot is ``[u16 length][key_size bytes, zero padded]``.

* Leaf entry:     ``[key slot][page_id i32][slot_id u16]``   (the RID the key points to)
* Internal node:  ``[child_0 i32]`` then ``[key slot][child i32]`` per key. Child ``i``
  holds keys ``k`` with ``keys[i-1] <= k < keys[i]``.

Nodes are decoded into Python lists, changed, and encoded back.
"""

import struct
from dataclasses import dataclass, field

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import CorruptPageError
from pydb.storage import page
from pydb.storage.page import PageType
from pydb.storage.table_heap import RID

_NODE_HEADER = struct.Struct("<HHii")
ENTRIES_OFFSET = page.HEADER_SIZE + _NODE_HEADER.size
_CHILD = struct.Struct("<i")
RID_SIZE = 6


def leaf_entry_size(key_size: int) -> int:
    return 2 + key_size + RID_SIZE


def internal_entry_size(key_size: int) -> int:
    return 2 + key_size + _CHILD.size


def leaf_capacity(key_size: int) -> int:
    """Most entries a leaf page can physically hold."""
    return (PAGE_SIZE - ENTRIES_OFFSET) // leaf_entry_size(key_size)


def internal_capacity(key_size: int) -> int:
    """Most keys an internal page can physically hold (it has one more child than keys)."""
    return (PAGE_SIZE - ENTRIES_OFFSET - _CHILD.size) // internal_entry_size(key_size)


@dataclass(slots=True)
class LeafNode:
    page_id: int
    keys: list[bytes] = field(default_factory=list)
    rids: list[RID] = field(default_factory=list)
    next_leaf: int = INVALID_PAGE_ID
    prev_leaf: int = INVALID_PAGE_ID


@dataclass(slots=True)
class InternalNode:
    page_id: int
    keys: list[bytes] = field(default_factory=list)
    children: list[int] = field(default_factory=list)


Node = LeafNode | InternalNode


def read_node(buf: bytes | bytearray, key_size: int) -> Node:
    header = page.read_header(buf)
    count, stored_key_size, next_leaf, prev_leaf = _NODE_HEADER.unpack_from(buf, page.HEADER_SIZE)
    if stored_key_size != key_size:
        raise CorruptPageError(
            f"page {header.page_id}: node key size {stored_key_size}, index uses {key_size}"
        )
    view = memoryview(buf)
    if header.page_type is PageType.BTREE_LEAF:
        if count > leaf_capacity(key_size):
            raise CorruptPageError(f"page {header.page_id}: leaf claims {count} entries")
        end = ENTRIES_OFFSET + count * leaf_entry_size(key_size)
        entries = struct.iter_unpack(f"<H{key_size}siH", view[ENTRIES_OFFSET:end])
        leaf = LeafNode(header.page_id, next_leaf=next_leaf, prev_leaf=prev_leaf)
        for length, key, rid_page, rid_slot in entries:
            leaf.keys.append(key[:length])
            leaf.rids.append(RID(rid_page, rid_slot))
        return leaf
    if header.page_type is PageType.BTREE_INTERNAL:
        if count > internal_capacity(key_size):
            raise CorruptPageError(f"page {header.page_id}: internal node claims {count} keys")
        node = InternalNode(header.page_id, children=[_CHILD.unpack_from(buf, ENTRIES_OFFSET)[0]])
        start = ENTRIES_OFFSET + _CHILD.size
        end = start + count * internal_entry_size(key_size)
        for length, key, child in struct.iter_unpack(f"<H{key_size}si", view[start:end]):
            node.keys.append(key[:length])
            node.children.append(child)
        return node
    raise CorruptPageError(f"page {header.page_id} is a {header.page_type.name} page, not a node")


def write_node(buf: bytearray, node: Node, key_size: int) -> None:
    """Encode ``node`` into ``buf``, keeping the page's LSN."""
    lsn = page.read_header(buf).page_lsn
    buf[:] = bytes(PAGE_SIZE)
    if isinstance(node, LeafNode):
        if len(node.keys) > leaf_capacity(key_size):
            raise CorruptPageError(f"leaf {node.page_id} overflows its page")
        page.write_header(buf, page.PageHeader(node.page_id, PageType.BTREE_LEAF, lsn))
        _NODE_HEADER.pack_into(
            buf, page.HEADER_SIZE, len(node.keys), key_size, node.next_leaf, node.prev_leaf
        )
        entry = struct.Struct(f"<H{key_size}siH")
        offset = ENTRIES_OFFSET
        for key, rid in zip(node.keys, node.rids, strict=True):
            entry.pack_into(buf, offset, len(key), key, rid.page_id, rid.slot_id)
            offset += entry.size
        return
    if len(node.keys) > internal_capacity(key_size) or len(node.children) != len(node.keys) + 1:
        raise CorruptPageError(f"internal node {node.page_id} has an invalid shape")
    page.write_header(buf, page.PageHeader(node.page_id, PageType.BTREE_INTERNAL, lsn))
    _NODE_HEADER.pack_into(
        buf, page.HEADER_SIZE, len(node.keys), key_size, INVALID_PAGE_ID, INVALID_PAGE_ID
    )
    _CHILD.pack_into(buf, ENTRIES_OFFSET, node.children[0])
    entry = struct.Struct(f"<H{key_size}si")
    offset = ENTRIES_OFFSET + _CHILD.size
    for key, child in zip(node.keys, node.children[1:], strict=True):
        entry.pack_into(buf, offset, len(key), key, child)
        offset += entry.size


# --------------------------------------------------------------------- in-page access
#
# The functions below read and edit a node directly in its page buffer. They are the hot
# path: descending the tree needs one binary search per level, and inserting into or
# deleting from a leaf that does not split or underflow shifts bytes instead of decoding
# the whole node.

_U16 = struct.Struct("<H")
_RID_STRUCT = struct.Struct("<iH")


def node_type(buf: bytes | bytearray) -> PageType:
    return page.read_header(buf).page_type


def key_count(buf: bytes | bytearray) -> int:
    value: int = _U16.unpack_from(buf, page.HEADER_SIZE)[0]
    return value


def next_leaf_of(buf: bytes | bytearray) -> int:
    value: int = _CHILD.unpack_from(buf, page.HEADER_SIZE + 4)[0]
    return value


def _key_at(buf: bytes | bytearray, offset: int) -> bytes:
    (length,) = _U16.unpack_from(buf, offset)
    return bytes(buf[offset + 2 : offset + 2 + length])


def internal_child_for(buf: bytes | bytearray, key_size: int, target: bytes) -> tuple[int, int]:
    """Return ``(index, child_page_id)`` of the child whose range contains ``target``."""
    start = ENTRIES_OFFSET + _CHILD.size
    size = internal_entry_size(key_size)
    lo, hi = 0, key_count(buf)
    while lo < hi:  # bisect_right over the separator keys
        mid = (lo + hi) // 2
        if target < _key_at(buf, start + mid * size):
            hi = mid
        else:
            lo = mid + 1
    if lo == 0:
        child: int = _CHILD.unpack_from(buf, ENTRIES_OFFSET)[0]
    else:
        child = _CHILD.unpack_from(buf, start + (lo - 1) * size + 2 + key_size)[0]
    return lo, child


def leaf_lower_bound(buf: bytes | bytearray, key_size: int, target: bytes) -> int:
    size = leaf_entry_size(key_size)
    lo, hi = 0, key_count(buf)
    while lo < hi:
        mid = (lo + hi) // 2
        if _key_at(buf, ENTRIES_OFFSET + mid * size) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def leaf_key(buf: bytes | bytearray, key_size: int, index: int) -> bytes:
    return _key_at(buf, ENTRIES_OFFSET + index * leaf_entry_size(key_size))


def leaf_insert_at(buf: bytearray, key_size: int, index: int, key: bytes, rid: RID) -> None:
    """Insert an entry at ``index``, shifting later entries right. The leaf must have room."""
    size = leaf_entry_size(key_size)
    count = key_count(buf)
    start = ENTRIES_OFFSET + index * size
    end = ENTRIES_OFFSET + count * size
    buf[start + size : end + size] = buf[start:end]
    struct.pack_into(f"<H{key_size}s", buf, start, len(key), key)
    _RID_STRUCT.pack_into(buf, start + 2 + key_size, rid.page_id, rid.slot_id)
    _U16.pack_into(buf, page.HEADER_SIZE, count + 1)


def leaf_remove_at(buf: bytearray, key_size: int, index: int) -> None:
    """Remove the entry at ``index``, shifting later entries left."""
    size = leaf_entry_size(key_size)
    count = key_count(buf)
    start = ENTRIES_OFFSET + index * size
    end = ENTRIES_OFFSET + count * size
    buf[start : end - size] = buf[start + size : end]
    buf[end - size : end] = bytes(size)
    _U16.pack_into(buf, page.HEADER_SIZE, count - 1)


def leaf_rid(buf: bytes | bytearray, key_size: int, index: int) -> RID:
    offset = ENTRIES_OFFSET + index * leaf_entry_size(key_size) + 2 + key_size
    page_id, slot_id = _RID_STRUCT.unpack_from(buf, offset)
    return RID(page_id, slot_id)
