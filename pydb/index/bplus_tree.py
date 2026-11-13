"""A disk-resident B+ tree mapping keys to RIDs.

Every entry's stored key is ``encode_key(key) + rid_suffix(rid)``, so entries are unique
even in a non-unique index, and all entries for one key are adjacent. A unique index
additionally refuses an insert when an entry with the same key (and no NULL in it) exists;
as in SQL, keys containing NULL never conflict.

The tree's fixed entry point is its header page (page type BTREE_HEADER)::

    offset  size  field
    24      4     root_page_id   (i32)
    28      2     key_size       (u16)  bytes per key slot, including the RID suffix
    30      1     unique         (u8)
    31      2     leaf_max       (u16)  most entries in a leaf
    33      2     internal_max   (u16)  most keys in an internal node
    35      1     column_count   (u8)
    36      n     column types   (one DataType byte each)

Nodes other than the root hold at least half their maximum (``max // 2``).

Concurrency: one reader-writer latch per tree object. Range scans take it per leaf, so a
scan running alongside writers is not isolated from them; transaction locks provide that.
"""

import itertools
import struct
from bisect import bisect_left
from collections.abc import Iterator, Sequence

from pydb.config import INVALID_PAGE_ID
from pydb.errors import CorruptPageError, DuplicateKeyError, TypeMismatchError
from pydb.index import key_codec
from pydb.index.bplus_node import (
    InternalNode,
    LeafNode,
    Node,
    internal_capacity,
    internal_child_for,
    key_count,
    leaf_capacity,
    leaf_insert_at,
    leaf_key,
    leaf_lower_bound,
    leaf_remove_at,
    leaf_rid,
    next_leaf_of,
    node_type,
    read_node,
    write_node,
)
from pydb.storage import page
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.latch import RWLatch
from pydb.storage.page import PageType
from pydb.storage.table_heap import RID
from pydb.types import DataType, Value

_TREE_HEADER = struct.Struct("<iHBHHB")
_RID_SUFFIX = struct.Struct(">IH")
RID_SUFFIX_SIZE = _RID_SUFFIX.size
MAX_KEY_COLUMNS = 16
MIN_FANOUT = 3

Key = tuple[Value, ...]
Path = list[tuple[int, int]]
"""Internal nodes from the root down to a leaf's parent: ``(page_id, child_index)``."""


def rid_suffix(rid: RID) -> bytes:
    """Big-endian so that entries with equal keys sort by RID."""
    return _RID_SUFFIX.pack(rid.page_id, rid.slot_id)


class BPlusTree:
    def __init__(self, pool: BufferPoolManager, header_page_id: int) -> None:
        self.pool = pool
        self.header_page_id = header_page_id
        self._latch = RWLatch()
        with pool.page(header_page_id) as frame:
            header = page.read_header(frame.data)
            if header.page_type is not PageType.BTREE_HEADER:
                raise CorruptPageError(f"page {header_page_id} is not a B+ tree header page")
            fields = _TREE_HEADER.unpack_from(frame.data, page.HEADER_SIZE)
            start = page.HEADER_SIZE + _TREE_HEADER.size
            raw_types = bytes(frame.data[start : start + fields[5]])
        self._root: int = fields[0]
        self.key_size: int = fields[1]
        self.unique = bool(fields[2])
        self.leaf_max: int = fields[3]
        self.internal_max: int = fields[4]
        self.key_types = tuple(DataType(t) for t in raw_types)
        self.leaf_min = self.leaf_max // 2
        self.internal_min = self.internal_max // 2

    @classmethod
    def create(
        cls,
        pool: BufferPoolManager,
        key_types: Sequence[DataType],
        *,
        unique: bool,
        leaf_max: int | None = None,
        internal_max: int | None = None,
    ) -> "BPlusTree":
        """Create an empty tree. ``leaf_max`` and ``internal_max`` default to what fits a page."""
        if not 1 <= len(key_types) <= MAX_KEY_COLUMNS:
            raise TypeMismatchError(f"an index key needs 1 to {MAX_KEY_COLUMNS} columns")
        key_size = key_codec.max_key_size(key_types) + RID_SUFFIX_SIZE
        leaf_max = _check_fanout("leaf_max", leaf_max, leaf_capacity(key_size))
        internal_max = _check_fanout("internal_max", internal_max, internal_capacity(key_size))
        with pool.create_page() as frame:
            root = frame.page_id
            write_node(frame.data, LeafNode(root), key_size)
        with pool.create_page() as frame:
            header_page_id = frame.page_id
            page.write_header(frame.data, page.PageHeader(header_page_id, PageType.BTREE_HEADER, 0))
            _TREE_HEADER.pack_into(
                frame.data,
                page.HEADER_SIZE,
                root,
                key_size,
                int(unique),
                leaf_max,
                internal_max,
                len(key_types),
            )
            start = page.HEADER_SIZE + _TREE_HEADER.size
            frame.data[start : start + len(key_types)] = bytes(key_types)
        return cls(pool, header_page_id)

    @property
    def root_page_id(self) -> int:
        return self._root

    # ----------------------------------------------------------------- public operations

    def insert(self, key: Sequence[Value], rid: RID) -> None:
        prefix = self._encode_full(key)
        entry = prefix + rid_suffix(rid)
        with self._latch.write():
            if self.unique and None not in key and self._has_prefix(prefix):
                raise DuplicateKeyError(f"duplicate key {tuple(key)!r} in unique index")
            path, leaf_id = self._descend(entry)
            with self.pool.page(leaf_id, write=True) as frame:
                i = leaf_lower_bound(frame.data, self.key_size, entry)
                count = key_count(frame.data)
                if i < count and leaf_key(frame.data, self.key_size, i) == entry:
                    raise DuplicateKeyError(
                        f"entry {tuple(key)!r} -> {rid} is already in the index"
                    )
                if count < self.leaf_max:
                    leaf_insert_at(frame.data, self.key_size, i, entry, rid)
                    return
                leaf = self._decode_leaf(frame.data)
            leaf.keys.insert(i, entry)
            leaf.rids.insert(i, rid)
            self._split_leaf(path, leaf)

    def delete(self, key: Sequence[Value], rid: RID) -> bool:
        """Remove the entry for ``key`` pointing at ``rid``. Return False if it is absent."""
        entry = self._encode_full(key) + rid_suffix(rid)
        with self._latch.write():
            path, leaf_id = self._descend(entry)
            with self.pool.page(leaf_id, write=True) as frame:
                i = leaf_lower_bound(frame.data, self.key_size, entry)
                count = key_count(frame.data)
                if i == count or leaf_key(frame.data, self.key_size, i) != entry:
                    return False
                if not path or count - 1 >= self.leaf_min:
                    leaf_remove_at(frame.data, self.key_size, i)
                    return True
                leaf = self._decode_leaf(frame.data)
            del leaf.keys[i]
            del leaf.rids[i]
            self._rebalance_leaf(path, leaf)
            return True

    def search(self, key: Sequence[Value]) -> list[RID]:
        """RIDs of every entry whose key equals ``key`` (or starts with it, for a prefix)."""
        prefix = key_codec.encode_key(self.key_types, key)
        rids: list[RID] = []
        with self._latch.read():
            _, page_id = self._descend(prefix)
            while page_id != INVALID_PAGE_ID:
                with self.pool.page(page_id) as frame:
                    i = leaf_lower_bound(frame.data, self.key_size, prefix)
                    count = key_count(frame.data)
                    while i < count:
                        if not leaf_key(frame.data, self.key_size, i).startswith(prefix):
                            return rids
                        rids.append(leaf_rid(frame.data, self.key_size, i))
                        i += 1
                    page_id = next_leaf_of(frame.data)
        return rids

    def range_scan(
        self,
        low: Sequence[Value] | None = None,
        high: Sequence[Value] | None = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> Iterator[tuple[Key, RID]]:
        """Yield ``(key, rid)`` in key order for keys between ``low`` and ``high``.

        Either bound may be None (unbounded) or a prefix of the key columns, in which case
        it is compared against that many leading columns.
        """
        low_b = None if low is None else key_codec.encode_key(self.key_types, low)
        high_b = None if high is None else key_codec.encode_key(self.key_types, high)
        with self._latch.read():
            leaf: LeafNode | None
            if low_b is None:
                leaf = self._leftmost_leaf()
                start = 0
            else:
                _, leaf_id = self._descend(low_b)
                leaf = self._load_leaf(leaf_id)
                start = bisect_left(leaf.keys, low_b)
        while leaf is not None:
            for entry, rid in zip(leaf.keys[start:], leaf.rids[start:], strict=True):
                if low_b is not None and not low_inclusive and entry[: len(low_b)] <= low_b:
                    continue
                if high_b is not None:
                    head = entry[: len(high_b)]
                    if head > high_b or (head == high_b and not high_inclusive):
                        return
                yield key_codec.decode_key(self.key_types, entry), rid
            if leaf.next_leaf == INVALID_PAGE_ID:
                return
            with self._latch.read():
                node = self._load(leaf.next_leaf)
            if not isinstance(node, LeafNode):
                raise CorruptPageError(f"leaf chain reaches non-leaf page {node.page_id}")
            leaf, start = node, 0

    def __len__(self) -> int:
        return sum(1 for _ in self.range_scan())

    def height(self) -> int:
        with self._latch.read():
            depth, node = 1, self._load(self._root)
            while isinstance(node, InternalNode):
                depth += 1
                node = self._load(node.children[0])
            return depth

    # ----------------------------------------------------------------- invariants

    def check_invariants(self) -> int:
        """Verify the tree's structure and return the number of entries.

        Checks: node types and fill bounds, sorted keys, separator ranges, equal leaf depth,
        and that the leaf chain (both directions) visits exactly the in-order entries.
        Raises CorruptPageError describing the first violation.
        """
        with self._latch.read():
            in_order: list[bytes] = []
            leaves: list[int] = []
            leaf_depths: set[int] = set()
            self._check_subtree(self._root, 1, None, None, in_order, leaves, leaf_depths)
            if len(leaf_depths) > 1:
                raise CorruptPageError(f"leaves at different depths: {sorted(leaf_depths)}")
            chain: list[bytes] = []
            prev, pid, visited = INVALID_PAGE_ID, leaves[0], []
            while pid != INVALID_PAGE_ID:
                node = self._load(pid)
                if not isinstance(node, LeafNode):
                    raise CorruptPageError(f"leaf chain reaches non-leaf page {pid}")
                if node.prev_leaf != prev:
                    raise CorruptPageError(f"leaf {pid} prev is {node.prev_leaf}, expected {prev}")
                visited.append(pid)
                chain.extend(node.keys)
                prev, pid = pid, node.next_leaf
                if len(visited) > len(leaves):
                    raise CorruptPageError("leaf chain is longer than the tree (cycle?)")
            if visited != leaves:
                raise CorruptPageError("leaf chain does not visit the leaves in tree order")
            if chain != in_order:
                raise CorruptPageError("leaf chain entries differ from the in-order traversal")
            return len(in_order)

    def _check_subtree(
        self,
        page_id: int,
        depth: int,
        low: bytes | None,
        high: bytes | None,
        in_order: list[bytes],
        leaves: list[int],
        leaf_depths: set[int],
    ) -> None:
        node = self._load(page_id)
        is_root = page_id == self._root
        keys = node.keys
        if any(a >= b for a, b in itertools.pairwise(keys)):
            raise CorruptPageError(f"node {page_id}: keys are not strictly increasing")
        if keys and (
            (low is not None and keys[0] < low) or (high is not None and keys[-1] >= high)
        ):
            raise CorruptPageError(f"node {page_id}: keys fall outside the parent's separators")
        if isinstance(node, LeafNode):
            if not is_root and not self.leaf_min <= len(keys) <= self.leaf_max:
                raise CorruptPageError(f"leaf {page_id} has {len(keys)} entries")
            leaf_depths.add(depth)
            leaves.append(page_id)
            in_order.extend(keys)
            return
        if len(keys) > self.internal_max or (not is_root and len(keys) < self.internal_min):
            raise CorruptPageError(f"internal node {page_id} has {len(keys)} keys")
        if is_root and not keys:
            raise CorruptPageError(f"internal root {page_id} has no keys")
        bounds = [low, *keys, high]
        for i, child in enumerate(node.children):
            self._check_subtree(
                child, depth + 1, bounds[i], bounds[i + 1], in_order, leaves, leaf_depths
            )

    # ----------------------------------------------------------------- insert helpers

    def _split_leaf(self, path: Path, leaf: LeafNode) -> None:
        mid = len(leaf.keys) // 2
        with self.pool.create_page() as frame:
            right = LeafNode(
                frame.page_id,
                keys=leaf.keys[mid:],
                rids=leaf.rids[mid:],
                next_leaf=leaf.next_leaf,
                prev_leaf=leaf.page_id,
            )
            write_node(frame.data, right, self.key_size)
        del leaf.keys[mid:]
        del leaf.rids[mid:]
        if leaf.next_leaf != INVALID_PAGE_ID:
            following = self._load_leaf(leaf.next_leaf)
            following.prev_leaf = right.page_id
            self._store(following)
        leaf.next_leaf = right.page_id
        self._store(leaf)
        self._insert_into_parent(path, leaf.page_id, right.keys[0], right.page_id)

    def _insert_into_parent(self, path: Path, left: int, separator: bytes, right: int) -> None:
        if not path:
            with self.pool.create_page() as frame:
                root = InternalNode(frame.page_id, keys=[separator], children=[left, right])
                write_node(frame.data, root, self.key_size)
            self._set_root(root.page_id)
            return
        parent_id, i = path.pop()
        parent = self._load_internal(parent_id)
        parent.keys.insert(i, separator)
        parent.children.insert(i + 1, right)
        if len(parent.keys) <= self.internal_max:
            self._store(parent)
            return
        mid = len(parent.keys) // 2
        up = parent.keys[mid]
        with self.pool.create_page() as frame:
            sibling = InternalNode(
                frame.page_id, keys=parent.keys[mid + 1 :], children=parent.children[mid + 1 :]
            )
            write_node(frame.data, sibling, self.key_size)
        del parent.keys[mid:]
        del parent.children[mid + 1 :]
        self._store(parent)
        self._insert_into_parent(path, parent.page_id, up, sibling.page_id)

    # ----------------------------------------------------------------- delete helpers

    def _rebalance_leaf(self, path: Path, leaf: LeafNode) -> None:
        parent_id, i = path[-1]
        parent = self._load_internal(parent_id)
        left = self._load_leaf(parent.children[i - 1]) if i > 0 else None
        if left is not None and len(left.keys) > self.leaf_min:
            leaf.keys.insert(0, left.keys.pop())
            leaf.rids.insert(0, left.rids.pop())
            parent.keys[i - 1] = leaf.keys[0]
            self._store(left, leaf, parent)
            return
        right = self._load_leaf(parent.children[i + 1]) if i + 1 < len(parent.children) else None
        if right is not None and len(right.keys) > self.leaf_min:
            leaf.keys.append(right.keys.pop(0))
            leaf.rids.append(right.rids.pop(0))
            parent.keys[i] = right.keys[0]
            self._store(right, leaf, parent)
            return
        if left is not None:  # merge leaf into its left sibling
            self._merge_leaves(left, leaf)
            del parent.keys[i - 1]
            del parent.children[i]
        elif right is not None:  # merge the right sibling into leaf
            self._merge_leaves(leaf, right)
            del parent.keys[i]
            del parent.children[i + 1]
        else:
            raise CorruptPageError(f"leaf {leaf.page_id} has no sibling to rebalance with")
        self._fix_internal(parent, path[:-1])

    def _merge_leaves(self, left: LeafNode, right: LeafNode) -> None:
        """Move every entry of ``right`` into ``left`` and free ``right``."""
        left.keys.extend(right.keys)
        left.rids.extend(right.rids)
        left.next_leaf = right.next_leaf
        if right.next_leaf != INVALID_PAGE_ID:
            following = self._load_leaf(right.next_leaf)
            following.prev_leaf = left.page_id
            self._store(following)
        self._store(left)
        self.pool.delete_page(right.page_id)

    def _fix_internal(self, node: InternalNode, path: Path) -> None:
        """``node`` (not yet stored) lost a key and a child; ``path`` leads to its parent."""
        if not path:  # node is the root
            if node.keys:
                self._store(node)
            else:
                self._set_root(node.children[0])
                self.pool.delete_page(node.page_id)
            return
        if len(node.keys) >= self.internal_min:
            self._store(node)
            return
        parent_id, i = path[-1]
        parent = self._load_internal(parent_id)
        left = self._load_internal(parent.children[i - 1]) if i > 0 else None
        if left is not None and len(left.keys) > self.internal_min:
            node.keys.insert(0, parent.keys[i - 1])
            node.children.insert(0, left.children.pop())
            parent.keys[i - 1] = left.keys.pop()
            self._store(left, node, parent)
            return
        right = (
            self._load_internal(parent.children[i + 1]) if i + 1 < len(parent.children) else None
        )
        if right is not None and len(right.keys) > self.internal_min:
            node.keys.append(parent.keys[i])
            node.children.append(right.children.pop(0))
            parent.keys[i] = right.keys.pop(0)
            self._store(right, node, parent)
            return
        if left is not None:
            left.keys += [parent.keys[i - 1], *node.keys]
            left.children += node.children
            self._store(left)
            self.pool.delete_page(node.page_id)
            del parent.keys[i - 1]
            del parent.children[i]
        elif right is not None:
            node.keys += [parent.keys[i], *right.keys]
            node.children += right.children
            self._store(node)
            self.pool.delete_page(right.page_id)
            del parent.keys[i]
            del parent.children[i + 1]
        else:
            raise CorruptPageError(f"internal node {node.page_id} has no sibling")
        self._fix_internal(parent, path[:-1])

    # ----------------------------------------------------------------- navigation and I/O

    def _encode_full(self, key: Sequence[Value]) -> bytes:
        if len(key) != len(self.key_types):
            raise TypeMismatchError(
                f"index key has {len(self.key_types)} columns, got {len(key)} values"
            )
        return key_codec.encode_key(self.key_types, key)

    def _descend(self, target: bytes) -> tuple[Path, int]:
        """Walk from the root to the leaf where ``target`` belongs.

        Returns the path of internal nodes and the leaf's page id. Each level is one binary
        search on the page bytes; nodes are not decoded.
        """
        path: Path = []
        page_id = self._root
        while True:
            with self.pool.page(page_id) as frame:
                kind = node_type(frame.data)
                if kind is PageType.BTREE_LEAF:
                    return path, page_id
                if kind is not PageType.BTREE_INTERNAL:
                    raise CorruptPageError(f"page {page_id} is a {kind.name} page, not a node")
                index, child = internal_child_for(frame.data, self.key_size, target)
            path.append((page_id, index))
            page_id = child

    def _leftmost_leaf(self) -> LeafNode:
        node = self._load(self._root)
        while isinstance(node, InternalNode):
            node = self._load(node.children[0])
        return node

    def _has_prefix(self, prefix: bytes) -> bool:
        """Is there an entry whose key starts with ``prefix``?"""
        _, leaf_id = self._descend(prefix)
        with self.pool.page(leaf_id) as frame:
            i = leaf_lower_bound(frame.data, self.key_size, prefix)
            if i < key_count(frame.data):
                return leaf_key(frame.data, self.key_size, i).startswith(prefix)
            following = next_leaf_of(frame.data)
        if following == INVALID_PAGE_ID:
            return False
        with self.pool.page(following) as frame:
            return key_count(frame.data) > 0 and leaf_key(frame.data, self.key_size, 0).startswith(
                prefix
            )

    def _decode_leaf(self, buf: bytearray) -> LeafNode:
        node = read_node(buf, self.key_size)
        if not isinstance(node, LeafNode):
            raise CorruptPageError(f"page {node.page_id} should be a leaf")
        return node

    def _set_root(self, page_id: int) -> None:
        with self.pool.page(self.header_page_id, write=True) as frame:
            struct.pack_into("<i", frame.data, page.HEADER_SIZE, page_id)
        self._root = page_id

    def _load(self, page_id: int) -> Node:
        with self.pool.page(page_id) as frame:
            return read_node(frame.data, self.key_size)

    def _load_leaf(self, page_id: int) -> LeafNode:
        node = self._load(page_id)
        if not isinstance(node, LeafNode):
            raise CorruptPageError(f"page {page_id} should be a leaf")
        return node

    def _load_internal(self, page_id: int) -> InternalNode:
        node = self._load(page_id)
        if not isinstance(node, InternalNode):
            raise CorruptPageError(f"page {page_id} should be an internal node")
        return node

    def _store(self, *nodes: Node) -> None:
        for node in nodes:
            with self.pool.page(node.page_id, write=True) as frame:
                write_node(frame.data, node, self.key_size)


def _check_fanout(name: str, requested: int | None, capacity: int) -> int:
    if capacity < MIN_FANOUT:
        raise TypeMismatchError(f"index key is too large: a page holds only {capacity} entries")
    if requested is None:
        return capacity
    if not MIN_FANOUT <= requested <= capacity:
        raise ValueError(f"{name} must be between {MIN_FANOUT} and {capacity}, got {requested}")
    return requested
