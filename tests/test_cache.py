"""`SharedCache` on its own, with no rates and no HTTP anywhere near it.

The service-level consequences of this behaviour are in test_convert_caching.py.
Here the fetch function is a counter, which is the point: if the coalescing and
the expiry can only be tested through an FX conversion, they are in the wrong
place.
"""

from __future__ import annotations

import asyncio

import pytest

from app.cache import SharedCache
from conftest import Clock


def counting(value: str = "v"):
    calls = []

    async def fetch() -> str:
        calls.append(value)
        return value

    return calls, fetch


async def test_a_hit_does_not_call_fetch():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())
    calls, fetch = counting()

    for _ in range(3):
        assert await cache.get_or_fetch("k", 60.0, fetch) == "v"

    assert len(calls) == 1


async def test_keys_do_not_collide():
    cache: SharedCache[tuple[str, int], str] = SharedCache(max_entries=8, monotonic=Clock())
    calls, fetch = counting()

    await cache.get_or_fetch(("a", 1), 60.0, fetch)
    await cache.get_or_fetch(("a", 2), 60.0, fetch)

    assert len(calls) == 2


async def test_an_entry_expires_when_its_ttl_runs_out():
    clock = Clock()
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=clock)
    calls, fetch = counting()

    await cache.get_or_fetch("k", 60.0, fetch)
    clock.advance(59)
    await cache.get_or_fetch("k", 60.0, fetch)
    assert len(calls) == 1

    clock.advance(2)
    await cache.get_or_fetch("k", 60.0, fetch)
    assert len(calls) == 2


async def test_a_ttl_of_none_never_expires():
    clock = Clock()
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=clock)
    calls, fetch = counting()

    await cache.get_or_fetch("k", None, fetch)
    clock.advance(365 * 24 * 3600)
    await cache.get_or_fetch("k", None, fetch)

    assert len(calls) == 1


async def test_a_ttl_of_zero_stores_nothing():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())
    calls, fetch = counting()

    await cache.get_or_fetch("k", 0.0, fetch)
    await cache.get_or_fetch("k", 0.0, fetch)

    assert len(calls) == 2
    assert len(cache) == 0


async def test_the_least_recently_used_entry_is_evicted_first():
    cache: SharedCache[int, str] = SharedCache(max_entries=3, monotonic=Clock())
    _, fetch = counting()

    for key in range(10):
        await cache.get_or_fetch(key, 60.0, fetch)

    assert len(cache) == 3
    # The three most recent survive; asking for them again must not refetch.
    calls, fetch = counting()
    for key in (7, 8, 9):
        await cache.get_or_fetch(key, 60.0, fetch)
    assert calls == []


async def test_simultaneous_callers_share_one_fetch():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow() -> str:
        calls.append(1)
        started.set()
        await release.wait()
        return "v"

    waiting = asyncio.gather(*(cache.get_or_fetch("k", 60.0, slow) for _ in range(5)))
    await started.wait()
    release.set()

    assert await waiting == ["v"] * 5
    assert len(calls) == 1


async def test_a_failed_fetch_is_not_cached():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())

    async def boom() -> str:
        raise RuntimeError("upstream said no")

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch("k", 60.0, boom)

    _, fetch = counting("recovered")
    assert await cache.get_or_fetch("k", 60.0, fetch) == "recovered"


async def test_simultaneous_callers_all_see_the_same_failure():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def boom() -> str:
        calls.append(1)
        started.set()
        await release.wait()
        raise RuntimeError("upstream said no")

    waiting = asyncio.gather(
        *(cache.get_or_fetch("k", 60.0, boom) for _ in range(5)), return_exceptions=True
    )
    await started.wait()
    release.set()
    results = await waiting

    assert len(calls) == 1
    assert all(isinstance(r, RuntimeError) for r in results)


async def test_the_caller_who_started_the_fetch_can_leave():
    cache: SharedCache[str, str] = SharedCache(max_entries=8, monotonic=Clock())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow() -> str:
        calls.append(1)
        started.set()
        await release.wait()
        return "v"

    first = asyncio.create_task(cache.get_or_fetch("k", 60.0, slow))
    second = asyncio.create_task(cache.get_or_fetch("k", 60.0, slow))
    await started.wait()

    first.cancel()  # the agent hung up
    release.set()

    assert await second == "v"
    assert len(calls) == 1
