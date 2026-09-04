"""Which answers the service is willing to reuse, and for how long.

`SharedCache` decides how to keep things; these tests are about what the FX
rules ask it to keep. The one that matters is the first: a rate fetched for one
day must never come back as the answer for another.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from app.config import Settings
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


async def test_a_rate_fetched_for_one_day_is_never_served_for_another(service: FxService, upstream):
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


async def test_the_pair_is_part_of_the_question(service: FxService, upstream):
    await service.convert(Decimal(1), "EUR", "TRY", None)
    await service.convert(Decimal(1), "EUR", "USD", None)

    assert upstream.call_count == 2


async def test_the_newest_rate_is_reused_only_briefly(service, upstream, clock, settings: Settings):
    await service.convert(Decimal(1), "EUR", "TRY", None)
    clock.advance(settings.latest_cache_ttl_seconds - 1)
    await service.convert(Decimal(1), "EUR", "TRY", None)
    assert upstream.call_count == 1

    clock.advance(2)
    await service.convert(Decimal(1), "EUR", "TRY", None)
    assert upstream.call_count == 2


async def test_a_day_that_is_over_is_kept_indefinitely(service: FxService, upstream, clock):
    await service.convert(Decimal(1), "EUR", "TRY", date(2020, 1, 2))
    clock.advance(365 * 24 * 3600)
    await service.convert(Decimal(1), "EUR", "TRY", date(2020, 1, 2))

    assert upstream.call_count == 1


async def test_today_is_not_treated_as_settled(service: FxService, upstream, clock, settings):
    # The 16:00 CET fixing may not have happened yet, so today's answer can
    # still change. It gets the same short life as "the newest rate".
    await service.convert(Decimal(1), "EUR", "TRY", TODAY)
    clock.advance(settings.latest_cache_ttl_seconds + 1)
    await service.convert(Decimal(1), "EUR", "TRY", TODAY)

    assert upstream.call_count == 2
