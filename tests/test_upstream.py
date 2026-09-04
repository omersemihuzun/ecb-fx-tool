"""`FrankfurterRates` on its own: what it retries, and what it refuses to guess.

The staleness rule is not here. Whether a rate that parsed correctly is an
answer to the question is a rule, and rules are tested in test_staleness.py
against `FxService`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.errors import FxError
from app.upstream import FrankfurterRates
from conftest import FakeUpstream, no_wait, rates_response


def build(upstream: FakeUpstream, attempts: int = 2) -> FrankfurterRates:
    return FrankfurterRates(
        httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
        "https://upstream.test",
        timeout_seconds=5.0,
        attempts=attempts,
        sleep=no_wait,
    )


async def test_a_blip_is_retried_and_the_caller_never_sees_it():
    upstream = FakeUpstream()
    replies = [httpx.Response(503), rates_response("2026-09-02", {"TRY": 47.1234})]
    upstream.responder = lambda _: replies.pop(0)

    quote = await build(upstream).quote("EUR", "TRY", None)

    assert quote.rate == Decimal("47.1234")
    assert upstream.call_count == 2


async def test_a_timeout_is_retried_once_and_then_reported():
    upstream = FakeUpstream()
    upstream.raises(httpx.ConnectTimeout("too slow"))

    with pytest.raises(FxError) as caught:
        await build(upstream).quote("EUR", "TRY", None)

    assert caught.value.code == "upstream_timeout"
    assert upstream.call_count == 2


async def test_a_bad_request_is_not_retried():
    # Sending the same wrong question again cannot make it right, and doing so
    # doubles the load on an upstream that is already refusing us.
    upstream = FakeUpstream()
    upstream.always(httpx.Response(404, json={"message": "not found"}))

    with pytest.raises(FxError) as caught:
        await build(upstream).quote("EUR", "ZWL", None)

    assert caught.value.code == "unsupported_currency"
    assert upstream.call_count == 1


async def test_an_unexpected_4xx_is_not_reported_as_a_bad_currency():
    # 404 is what Frankfurter answers for a code it does not carry. Anything
    # else in the 4xx range is a protocol change, and telling the caller their
    # currency is wrong would send them off fixing the wrong thing.
    upstream = FakeUpstream()
    upstream.always(httpx.Response(418, text="teapot"))

    with pytest.raises(FxError) as caught:
        await build(upstream).quote("EUR", "TRY", None)

    assert caught.value.code == "upstream_invalid_response"
    assert "418" in caught.value.message


async def test_retrying_can_be_switched_off():
    upstream = FakeUpstream()
    upstream.always(httpx.Response(503))

    with pytest.raises(FxError):
        await build(upstream, attempts=1).quote("EUR", "TRY", None)

    assert upstream.call_count == 1


async def test_a_dated_request_asks_for_that_date_and_no_other():
    upstream = FakeUpstream()

    await build(upstream).quote("EUR", "TRY", date(2026, 8, 28))

    assert upstream.paths() == ["/v1/2026-08-28"]
    assert upstream.requests[0].url.params["base"] == "EUR"
