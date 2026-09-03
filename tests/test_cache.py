"""Caching behaviour, at the service level.

The point of these tests is not that a cache exists. It is that the cache
cannot answer a question it was not asked: a rate fetched for one date must
never come back labelled with another.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.errors import FxError
from app.fx import FxService
from conftest import TODAY, rates_response


def by_path(mapping: dict[str, httpx.Response]):
    def responder(request: httpx.Request) -> httpx.Response:
        return mapping[request.url.path.rsplit("/", 1)[-1]]

    return responder


async def test_a_repeated_question_costs_one_upstream_call(service: FxService, upstream):
    for _ in range(3):
        await service.convert(Decimal(100), "EUR", "TRY", date(2026, 8, 28))

    assert upstream.call_count == 1


async def test_the_cache_is_keyed_by_date(service: FxService, upstream):
    upstream.responder = by_path(
        {
            "2020-01-02": rates_response("2020-01-02", {"TRY": 6.6}),
            "latest": rates_response("2026-09-02", {"TRY": 47.1234}),
        }
    )

    old = await service.convert(Decimal(1), "EUR", "TRY", date(2020, 1, 2))
    now = await service.convert(Decimal(1), "EUR", "TRY", None)

    assert old.rate == Decimal("6.6")
    assert now.rate == Decimal("47.1234")
    assert upstream.call_count == 2


async def test_the_cache_is_keyed_by_pair(service: FxService, upstream):
    await service.convert(Decimal(1), "EUR", "TRY", None)
    await service.convert(Decimal(1), "EUR", "USD", None)

    assert upstream.call_count == 2


async def test_the_latest_rate_expires(service: FxService, upstream, clock, settings: Settings):
    await service.convert(Decimal(1), "EUR", "TRY", None)
    clock.advance(settings.latest_cache_ttl_seconds - 1)
    await service.convert(Decimal(1), "EUR", "TRY", None)
    assert upstream.call_count == 1

    clock.advance(2)
    await service.convert(Decimal(1), "EUR", "TRY", None)
    assert upstream.call_count == 2


async def test_a_closed_day_is_cached_indefinitely(service: FxService, upstream, clock):
    await service.convert(Decimal(1), "EUR", "TRY", date(2020, 1, 2))
    clock.advance(365 * 24 * 3600)
    await service.convert(Decimal(1), "EUR", "TRY", date(2020, 1, 2))

    assert upstream.call_count == 1


async def test_today_is_not_treated_as_final(service: FxService, upstream, clock, settings):
    # The 16:00 CET fixing may not have happened yet, so today's answer can
    # still change. It gets the same expiry as `latest`.
    await service.convert(Decimal(1), "EUR", "TRY", TODAY)
    clock.advance(settings.latest_cache_ttl_seconds + 1)
    await service.convert(Decimal(1), "EUR", "TRY", TODAY)

    assert upstream.call_count == 2


async def test_concurrent_identical_requests_share_one_upstream_call(service: FxService, upstream):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        upstream.requests.append(request)
        started.set()
        await release.wait()
        return rates_response("2026-09-02", {"TRY": 47.1234})

    # MockTransport takes an async handler, which lets the first call block
    # while the others queue up behind it.
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))

    calls = [service.convert(Decimal(1), "EUR", "TRY", None) for _ in range(5)]
    task = asyncio.gather(*calls)
    await started.wait()
    release.set()
    results = await task

    assert upstream.call_count == 1
    assert {conversion.rate for conversion in results} == {Decimal("47.1234")}


async def test_a_failed_fetch_is_not_cached(service: FxService, upstream):
    upstream.always(httpx.Response(503))
    with pytest.raises(FxError):
        await service.convert(Decimal(1), "EUR", "TRY", None)

    upstream.always(rates_response("2026-09-02", {"TRY": 47.1234}))
    conversion = await service.convert(Decimal(1), "EUR", "TRY", None)

    assert conversion.rate == Decimal("47.1234")
    assert upstream.call_count == 2


async def test_the_cache_does_not_grow_without_bound(upstream, settings: Settings):
    small = dataclasses.replace(settings, cache_max_entries=3)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle))
    service = FxService(client, small, today=lambda: TODAY)

    for day in range(1, 11):
        await service.convert(Decimal(1), "EUR", "TRY", date(2026, 8, day))

    assert len(service._cache) == 3  # noqa: SLF001 - the bound is the behaviour under test
