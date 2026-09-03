"""Contract checks against the real frankfurter.dev.

Deselected by default; `./test.sh` runs without a network. These exist because
the rest of the suite is only as correct as its assumptions about the upstream,
and those assumptions are worth re-checking by hand:

    ./test.sh -m live
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

pytestmark = pytest.mark.live

BASE = "https://api.frankfurter.dev/v1"


@pytest.fixture(scope="module")
def upstream() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=15.0) as client:
        yield client


def test_latest_carries_the_date_it_was_published_for(upstream: httpx.Client):
    payload = upstream.get("/latest", params={"base": "EUR", "symbols": "TRY"}).json()

    assert set(payload) >= {"base", "date", "rates"}
    assert date.fromisoformat(payload["date"]) <= date.today()
    assert payload["rates"]["TRY"] > 0


def test_a_closed_day_is_answered_with_an_earlier_date(upstream: httpx.Client):
    # 2025-08-30 was a Saturday.
    payload = upstream.get("/2025-08-30", params={"base": "EUR", "symbols": "TRY"}).json()

    assert payload["date"] == "2025-08-29"


def test_a_future_date_is_a_404_and_not_a_stale_rate(upstream: httpx.Client):
    ahead = (date.today() + timedelta(days=365)).isoformat()

    response = upstream.get(f"/{ahead}", params={"base": "EUR", "symbols": "TRY"})

    assert response.status_code == 404


def test_an_unknown_currency_is_a_404(upstream: httpx.Client):
    response = upstream.get("/latest", params={"base": "EUR", "symbols": "ZWL"})

    assert response.status_code == 404


def test_an_identical_pair_is_rejected_upstream(upstream: httpx.Client):
    # The service never sends this; it is here to record why it must not.
    response = upstream.get("/latest", params={"base": "EUR", "symbols": "EUR"})

    assert response.status_code == 422
