import random
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.errors import CorruptPageError, DuplicateKeyError, TypeMismatchError
from pydb.index.bplus_node import InternalNode, LeafNode, read_node, write_node
from pydb.index.bplus_tree import BPlusTree
from pydb.storage import page
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.storage.table_heap import RID
from pydb.types import DataType

INT, TEXT, FLOAT = DataType.INT, DataType.TEXT, DataType.FLOAT


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[BufferPoolManager]:
    disk = DiskManager(tmp_path / "tree.db")
    bpm = BufferPoolManager(disk, 64)
    yield bpm
    assert bpm.pinned_pages() == {}
    disk.close()


def small_tree(pool: BufferPoolManager, *, unique: bool = True) -> BPlusTree:
    """Fanout 4/3 so a few dozen keys already build a multi-level tree."""
    return BPlusTree.create(pool, [INT], unique=unique, leaf_max=4, internal_max=3)


def rid(n: int) -> RID:
    return RID(n // 100 + 1, n % 100)


# ------------------------------------------------------------------ basics


def test_empty_tree(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    assert tree.search((1,)) == []
    assert list(tree.range_scan()) == []
    assert tree.delete((1,), rid(1)) is False
    assert tree.check_invariants() == 0
    assert tree.height() == 1


def test_insert_and_search(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in (5, 1, 9, 3):
        tree.insert((k,), rid(k))
    for k in (1, 3, 5, 9):
        assert tree.search((k,)) == [rid(k)]
    assert tree.search((4,)) == []
    assert tree.check_invariants() == 4


def test_unique_index_rejects_duplicates(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    tree.insert((7,), rid(1))
    with pytest.raises(DuplicateKeyError, match="duplicate key"):
        tree.insert((7,), rid(2))
    assert tree.search((7,)) == [rid(1)]


def test_unique_index_allows_many_nulls(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for n in range(5):
        tree.insert((None,), rid(n))
    assert tree.search((None,)) == [rid(n) for n in range(5)]


def test_duplicate_detection_across_leaf_boundaries(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(0, 200, 2):
        tree.insert((k,), rid(k))
    for k in range(0, 200, 2):
        with pytest.raises(DuplicateKeyError):
            tree.insert((k,), rid(k + 1))
    tree.check_invariants()


def test_non_unique_index_keeps_every_rid(pool: BufferPoolManager) -> None:
    tree = small_tree(pool, unique=False)
    rids = [RID(p, s) for p in (3, 1, 2) for s in (5, 0)]
    for r in rids:
        tree.insert((42,), r)
        tree.insert((41,), r)
    assert tree.search((42,)) == sorted(rids)  # equal keys are ordered by RID
    with pytest.raises(DuplicateKeyError, match="already in the index"):
        tree.insert((42,), rids[0])
    assert tree.delete((42,), rids[0])
    assert rids[0] not in tree.search((42,))
    assert tree.check_invariants() == 11


def test_wrong_key_arity(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    with pytest.raises(TypeMismatchError):
        tree.insert((1, 2), rid(1))
    with pytest.raises(TypeMismatchError):
        tree.delete((), rid(1))


# ------------------------------------------------------------------ structure changes


def test_splits_grow_the_tree_and_merges_shrink_it(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    keys = list(range(200))
    random.Random(3).shuffle(keys)
    for k in keys:
        tree.insert((k,), rid(k))
    assert tree.height() >= 4
    assert tree.check_invariants() == 200
    pages_before = pool.disk.free_page_count
    for k in keys[:195]:
        assert tree.delete((k,), rid(k))
        tree.check_invariants()
    assert tree.height() <= 2
    assert pool.disk.free_page_count > pages_before  # merged nodes went back to the free list
    assert [k for (k,), _ in tree.range_scan()] == sorted(keys[195:])


def test_delete_everything_leaves_an_empty_root_leaf(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(50):
        tree.insert((k,), rid(k))
    for k in range(50):
        assert tree.delete((k,), rid(k))
    assert tree.height() == 1
    assert tree.check_invariants() == 0
    tree.insert((1,), rid(1))
    assert tree.search((1,)) == [rid(1)]


def test_sequential_inserts_and_reverse_deletes(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(300):
        tree.insert((k,), rid(k))
    tree.check_invariants()
    for k in reversed(range(300)):
        tree.delete((k,), rid(k))
    assert tree.check_invariants() == 0


def test_delete_of_missing_entry(pool: BufferPoolManager) -> None:
    tree = small_tree(pool, unique=False)
    tree.insert((1,), RID(1, 1))
    assert tree.delete((1,), RID(1, 2)) is False
    assert tree.delete((2,), RID(1, 1)) is False
    assert tree.search((1,)) == [RID(1, 1)]


# ------------------------------------------------------------------ range scans


@pytest.fixture
def evens(pool: BufferPoolManager) -> BPlusTree:
    tree = small_tree(pool)
    for k in range(0, 100, 2):
        tree.insert((k,), rid(k))
    return tree


def keys_of(it: Iterator[tuple[tuple[object, ...], RID]]) -> list[object]:
    return [key[0] for key, _ in it]


def test_range_inclusive_and_exclusive_bounds(evens: BPlusTree) -> None:
    assert keys_of(evens.range_scan((10,), (20,))) == [10, 12, 14, 16, 18, 20]
    assert keys_of(evens.range_scan((10,), (20,), low_inclusive=False, high_inclusive=False)) == [
        12,
        14,
        16,
        18,
    ]
    assert keys_of(evens.range_scan((11,), (19,))) == [12, 14, 16, 18]


def test_range_open_ends(evens: BPlusTree) -> None:
    assert keys_of(evens.range_scan(None, (4,))) == [0, 2, 4]
    assert keys_of(evens.range_scan((94,), None)) == [94, 96, 98]
    assert keys_of(evens.range_scan()) == list(range(0, 100, 2))


def test_empty_and_inverted_ranges(evens: BPlusTree) -> None:
    assert keys_of(evens.range_scan((21,), (21,))) == []
    assert keys_of(evens.range_scan((50,), (10,))) == []
    assert keys_of(evens.range_scan((200,), None)) == []


def test_range_returns_rids(evens: BPlusTree) -> None:
    assert list(evens.range_scan((6,), (8,))) == [((6,), rid(6)), ((8,), rid(8))]


def test_composite_key_prefix_ranges(pool: BufferPoolManager) -> None:
    tree = BPlusTree.create(pool, [TEXT, INT], unique=True, leaf_max=4, internal_max=3)
    rows = [(city, n) for city in ("berlin", "delhi", "lima", "oslo") for n in range(5)]
    for i, row in enumerate(rows):
        tree.insert(row, rid(i))
    assert [k for k, _ in tree.range_scan(("delhi",), ("delhi",))] == [
        ("delhi", n) for n in range(5)
    ]
    assert [k for k, _ in tree.range_scan(("delhi", 3), ("lima", 1))] == [
        ("delhi", 3),
        ("delhi", 4),
        ("lima", 0),
        ("lima", 1),
    ]
    after_delhi = tree.range_scan(("delhi",), ("lima",), low_inclusive=False, high_inclusive=False)
    assert list(after_delhi) == []
    assert len(tree.search(("oslo",))) == 5
    tree.check_invariants()


def test_float_and_text_keys(pool: BufferPoolManager) -> None:
    tree = BPlusTree.create(pool, [FLOAT], unique=False)
    values = [3.5, -0.0, -2.25, 1e10, -1e10, 0.0]
    for i, v in enumerate(values):
        tree.insert((v,), rid(i))
    assert keys_of(tree.range_scan()) == sorted(values)
    texts = BPlusTree.create(pool, [TEXT], unique=True)
    words = ["pear", "apple", "", "Zebra", "apple pie", "éclair"]
    for i, w in enumerate(words):
        texts.insert((w,), rid(i))
    assert keys_of(texts.range_scan()) == sorted(words)


# ------------------------------------------------------------------ scale and persistence


def test_100k_random_inserts_are_all_found(pool: BufferPoolManager) -> None:
    tree = BPlusTree.create(pool, [INT], unique=True)
    keys = list(range(100_000))
    random.Random(7).shuffle(keys)
    for k in keys:
        tree.insert((k,), rid(k))
    assert tree.check_invariants() == 100_000
    scanned = list(tree.range_scan())
    assert scanned == [((k,), rid(k)) for k in range(100_000)]
    for k in random.Random(8).sample(keys, 2000):
        assert tree.search((k,)) == [rid(k)]
    assert tree.height() == 3


def test_tree_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    keys = list(range(500))
    random.Random(5).shuffle(keys)
    with DiskManager(path) as disk:
        pool = BufferPoolManager(disk, 16)
        tree = BPlusTree.create(pool, [INT], unique=False, leaf_max=8, internal_max=5)
        for k in keys:
            tree.insert((k,), rid(k))
        for k in keys[:100]:
            tree.delete((k,), rid(k))
        header, root = tree.header_page_id, tree.root_page_id
        pool.flush_all()
    with DiskManager(path) as disk:
        tree = BPlusTree(BufferPoolManager(disk, 16), header)
        assert tree.root_page_id == root
        assert (tree.unique, tree.leaf_max, tree.internal_max) == (False, 8, 5)
        assert tree.key_types == (INT,)
        assert tree.check_invariants() == 400
        assert keys_of(tree.range_scan()) == sorted(keys[100:])


# ------------------------------------------------------------------ validation and corruption


def test_fanout_must_fit_and_be_at_least_three(pool: BufferPoolManager) -> None:
    with pytest.raises(ValueError, match="leaf_max"):
        BPlusTree.create(pool, [INT], unique=True, leaf_max=2)
    with pytest.raises(ValueError, match="internal_max"):
        BPlusTree.create(pool, [INT], unique=True, internal_max=100_000)
    with pytest.raises(TypeMismatchError, match="1 to 16"):
        BPlusTree.create(pool, [], unique=True)


def test_opening_a_non_header_page_fails(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    with pytest.raises(CorruptPageError, match="not a B\\+ tree header"):
        BPlusTree(pool, tree.root_page_id)


def test_check_invariants_detects_unsorted_leaf(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(3):
        tree.insert((k,), rid(k))
    with pool.page(tree.root_page_id, write=True) as frame:
        leaf = read_node(frame.data, tree.key_size)
        assert isinstance(leaf, LeafNode)
        leaf.keys.reverse()
        write_node(frame.data, leaf, tree.key_size)
    with pytest.raises(CorruptPageError, match="strictly increasing"):
        tree.check_invariants()


def test_check_invariants_detects_bad_separator(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(20):
        tree.insert((k,), rid(k))
    with pool.page(tree.root_page_id, write=True) as frame:
        root = read_node(frame.data, tree.key_size)
        assert isinstance(root, InternalNode)
        root.keys[0] = b"\x01" + b"\xff" * 14  # larger than everything to its right
        write_node(frame.data, root, tree.key_size)
    with pytest.raises(CorruptPageError):
        tree.check_invariants()


def test_check_invariants_detects_broken_leaf_chain(pool: BufferPoolManager) -> None:
    tree = small_tree(pool)
    for k in range(20):
        tree.insert((k,), rid(k))
    page_id = tree.root_page_id
    while True:  # walk down the leftmost spine to the first leaf
        with pool.page(page_id) as frame:
            node = read_node(frame.data, tree.key_size)
        if isinstance(node, LeafNode):
            break
        page_id = node.children[0]
    with pool.page(page_id, write=True) as frame:
        struct.pack_into("<i", frame.data, page.HEADER_SIZE + 8, 9999)  # bogus prev link
    with pytest.raises(CorruptPageError, match="prev"):
        tree.check_invariants()
