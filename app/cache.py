"""A cache that also stops the same question being asked twice at once.

Two problems arrive together and are cheaper to solve together. A rate that has
already been fetched should not be fetched again for a while, and a burst of
identical calls arriving before the first one returns should cost the upstream
one request rather than one per caller. Both are "do not ask again", so both
live behind one method.

Nothing here knows what a rate is. It is tested on its own in
`tests/test_cache.py` with a counter for a fetch function.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class SharedCache(Generic[K, V]):
    """Bounded, least-recently-used, with an optional per-entry expiry.

    Bounded because an unbounded dict keyed by user input is a memory leak with
    extra steps. Per-entry expiry rather than one global TTL because the caller
    knows which answers are final and which can still change.
    """

    def __init__(self, max_entries: int, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._entries: OrderedDict[K, tuple[V, float | None]] = OrderedDict()
        self._inflight: dict[K, asyncio.Task[V]] = {}

    async def get_or_fetch(
        self,
        key: K,
        ttl_seconds: float | None,
        fetch: Callable[[], Awaitable[V]],
    ) -> V:
        """Return the cached value, the one being fetched, or a fresh one.

        `ttl_seconds` of None means the value never expires; zero or less means
        do not keep it at all. A `fetch` that raises is not cached, so the next
        caller tries again rather than inheriting a failure.
        """
        hit = self._get(key)
        if hit is not None:
            return hit

        running = self._inflight.get(key)
        if running is not None:
            # Shielded: the caller that started this fetch may disconnect, and
            # everyone waiting behind it should still get an answer.
            return await asyncio.shield(running)

        task = asyncio.create_task(fetch())
        task.add_done_callback(_mark_retrieved)
        self._inflight[key] = task
        try:
            value = await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)

        self._put(key, value, ttl_seconds)
        return value

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals --------------------------------------------------------

    def _get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self._monotonic() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def _put(self, key: K, value: V, ttl_seconds: float | None) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            return
        expires_at = None if ttl_seconds is None else self._monotonic() + ttl_seconds
        self._entries[key] = (value, expires_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def _mark_retrieved(task: asyncio.Task) -> None:
    """Keep asyncio quiet about a failure nobody was left to await.

    If the caller that started a fetch disconnects before it finishes and no
    one else is waiting, the exception is never retrieved and asyncio logs it
    as an error at garbage-collection time. It is not one.
    """
    if not task.cancelled():
        task.exception()
