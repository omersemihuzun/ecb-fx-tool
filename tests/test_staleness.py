"""How far a rate may be carried forward before it stops being an answer.

Carrying Friday's rate into Saturday is the whole point of the weekend
handling. Carrying it into November is not, and `rate_date` saying so is not
enough: a caller reading `result` would never look.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.errors import FxError
from app.fx import FxService
from conftest import TODAY, build_harness, rates_response


def answers_with(published: date):
    return lambda _: rates_response(published.isoformat(), {"TRY": 47.1234})


@pytest.mark.parametrize("gap", [0, 1, 3, 5, 7])
async def test_a_gap_a_real_closure_could_produce_is_answered(gap: int):
    # The longest real gap in the ECB series since 2019 is five days, at Easter.
    asked = TODAY - timedelta(days=10)
    harness = build_harness(responder=answers_with(asked - timedelta(days=gap)))

    conversion = await harness.service.convert(Decimal(1), "EUR", "TRY", asked)

    assert conversion.rate_date == asked - timedelta(days=gap)
    assert conversion.asked_date == asked


@pytest.mark.parametrize("gap", [8, 30, 400])
async def test_a_gap_no_closure_explains_is_refused(gap: int):
    asked = TODAY - timedelta(days=500)
    harness = build_harness(responder=answers_with(asked - timedelta(days=gap)))

    with pytest.raises(FxError) as caught:
        await harness.service.convert(Decimal(1), "EUR", "TRY", asked)

    assert caught.value.code == "no_rate_available"
    assert caught.value.status_code == 404


async def test_a_frozen_feed_stops_being_reported_as_today_s_rate():
    # The failure this rule exists for: the upstream keeps answering, with a
    # rate that stopped moving months ago. Nothing else in the service notices.
    harness = build_harness(responder=answers_with(TODAY - timedelta(days=120)))

    with pytest.raises(FxError) as caught:
        await harness.service.convert(Decimal(250), "EUR", "TRY", None)

    assert caught.value.code == "no_rate_available"
    assert "120" not in caught.value.message  # it names dates, not arithmetic
    assert (TODAY - timedelta(days=120)).isoformat() in caught.value.message


async def test_the_limit_is_configurable():
    asked = TODAY - timedelta(days=100)
    settings = dataclasses.replace(
        Settings(upstream_base="https://upstream.test"), max_staleness_days=30
    )
    harness = build_harness(
        responder=answers_with(asked - timedelta(days=20)), settings=settings
    )

    conversion = await harness.service.convert(Decimal(1), "EUR", "TRY", asked)

    assert conversion.rate_date == asked - timedelta(days=20)


async def test_the_refusal_reaches_the_caller_as_a_404(client, upstream):
    upstream.always(rates_response((TODAY - timedelta(days=90)).isoformat(), {"TRY": 47.1234}))

    response = client.get("/tools/convert", params={"amount": 250, "from": "EUR", "to": "TRY"})

    assert response.status_code == 404
    assert response.json()["error"] == "no_rate_available"
    assert "rate" not in response.json()


def test_the_default_clears_the_longest_real_ecb_closure():
    # Measured over the published series from 2019 to 2026: the longest gap
    # between consecutive publications is five days, every year at Easter.
    assert Settings.from_env({}).max_staleness_days >= 5
