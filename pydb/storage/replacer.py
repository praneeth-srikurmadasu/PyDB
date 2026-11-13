"""Choosing which buffer-pool frame to evict."""

from collections import OrderedDict


class LRUReplacer:
    """Least-recently-unpinned order over frames that are eligible for eviction.

    A frame becomes eligible when its pin count drops to zero (``unpin``) and stops being
    eligible when it is pinned again (``pin``). ``victim`` removes and returns the frame that
    has been eligible the longest. All operations are O(1). Not thread-safe; the buffer pool
    calls it under its own latch.
    """

    def __init__(self) -> None:
        self._order: OrderedDict[int, None] = OrderedDict()

    def unpin(self, frame_id: int) -> None:
        """Mark a frame evictable. A frame that is already evictable keeps its position."""
        if frame_id not in self._order:
            self._order[frame_id] = None

    def pin(self, frame_id: int) -> None:
        """Mark a frame not evictable."""
        self._order.pop(frame_id, None)

    def victim(self) -> int | None:
        """Remove and return the least recently unpinned frame, or None if none is evictable."""
        if not self._order:
            return None
        frame_id, _ = self._order.popitem(last=False)
        return frame_id

    def __len__(self) -> int:
        return len(self._order)
