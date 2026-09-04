"""Every claim in REVIEW.md, asserted against `tool.py`.

Part B is a set of statements about somebody else's code. Statements rot: a
reviewer cannot tell a measured finding from a plausible one, and neither can I
six weeks from now. So each finding is pinned here, and if one stops
reproducing this suite goes red and REVIEW.md is wrong.

The fake mirrors what api.frankfurter.dev actually does, measured first against
the live service and re-checked by `tests/test_live.py`:

    /v1/latest?base=EUR&symbols=EUR   -> 422 {"message": "bad currency pair"}
    /v1/latest?base=EUR&symbols=ZWL   -> 404 {"message": "not found"}
    /v1/2030-01-01?...                -> 404 {"message": "not found"}
    /v1/2025-08-30?...                -> 200 {"date": "2025-08-29", ...}
                                              a Saturday was asked for; the
                                              upstream answers with the Friday
                                              it published, and says so.

No network is touched: `tool.client` is replaced with a MockTransport.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import tool

PUBLISHED = {"2025-08-29": 47.9536, "2020-01-02": 6.6, "latest": 55.9145}


def frankfurter(request: httpx.Request) -> httpx.Response:
    base, symbol = request.url.params["base"], request.url.params["symbols"]
    asked = request.url.path.rsplit("/", 1)[-1]
    if base == symbol:
        return httpx.Response(422, json={"message": "bad currency pair"})
    if "ZWL" in (base, symbol):
        return httpx.Response(404, json={"message": "not found"})
    if asked == "latest":
        return httpx.Response(200, json={"base": base, "date": "2026-09-02",
                                         "rates": {symbol: PUBLISHED["latest"]}})
    if asked == "2025-08-30":  # a Saturday
        return httpx.Response(200, json={"base": base, "date": "2025-08-29",
                                         "rates": {symbol: PUBLISHED["2025-08-29"]}})
    if asked in PUBLISHED:
        return httpx.Response(200, json={"base": base, "date": asked,
                                         "rates": {symbol: PUBLISHED[asked]}})
    return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def broken() -> TestClient:
    """`tool.py` as written, wired to the fake upstream, cache cleared."""
    tool._cache.clear()
    tool.client = httpx.AsyncClient(transport=httpx.MockTransport(frankfurter))
    return TestClient(tool.app)


def call(client: TestClient, **params) -> dict:
    response = client.get("/tools/convert", params=params)
    assert response.status_code == 200, "finding 1: nothing here ever returns non-2xx"
    return response.json()


# --- finding 1: every failure is a 200 with a rate of zero -------------------


def test_an_upstream_outage_is_answered_with_zero(broken: TestClient):
    tool.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503)))

    body = call(broken, amount=250, from_="EUR", to="TRY")

    assert body["rate"] == 0.0 and body["result"] == 0.0
    assert body["source"] == "ECB via frankfurter.dev"  # credited anyway


def test_an_unknown_currency_is_answered_with_zero(broken: TestClient):
    body = call(broken, amount=250, from_="EUR", to="ZWL")

    assert body["result"] == 0.0


def test_converting_a_currency_to_itself_is_answered_with_zero(broken: TestClient):
    body = call(broken, amount=250, from_="EUR", to="EUR")

    assert body["result"] == 0.0, "250 EUR is reported as 0.00 EUR"


# --- finding 2: the documented parameters do not reach the handler -----------


def test_the_documented_from_parameter_is_ignored(broken: TestClient):
    body = call(broken, amount=250, **{"from": "USD"}, to="TRY")

    assert body["from"] == "EUR", "USD was asked for; EUR was converted"


def test_the_documented_date_parameter_is_ignored(broken: TestClient):
    body = call(broken, amount=1, **{"from": "EUR", "date": "2020-01-02"}, to="TRY")

    assert body["rate"] == 55.91, "a 2020 question answered at today's rate"


# --- finding 3: the cache key has no date, and a hit invents the date --------


def test_one_answer_is_reused_for_every_date(broken: TestClient):
    old = call(broken, amount=1, from_="EUR", to="TRY", on="2020-01-02")
    now = call(broken, amount=1, from_="EUR", to="TRY")

    assert old["rate"] == now["rate"] == 6.6, "the 2020 rate answers both"
    assert now["rate_date"] != "2020-01-02", "and is stamped with the wrong date"


# --- finding 4: the rate is rounded before it is used ------------------------


def test_the_published_rate_is_truncated_to_two_decimals(broken: TestClient):
    body = call(broken, amount=250, from_="EUR", to="TRY")

    assert body["rate"] == 55.91, "55.9145 as published"
    assert body["result"] == 13977.50, "13978.63 at the real rate: 1.13 TRY lost"


# --- finding 5: rate_date is the question, not the answer --------------------


def test_a_saturday_gets_fridays_rate_labelled_saturday(broken: TestClient):
    body = call(broken, amount=1, from_="EUR", to="TRY", on="2025-08-30")

    assert body["rate"] == 47.95, "Friday's rate, rounded"
    assert body["rate_date"] == "2025-08-30", "reported as Saturday's"
    assert "asked_date" not in body, "and nothing in the body reveals the swap"


# --- finding 6: amounts are unvalidated, and errors come in two shapes -------


def test_a_nan_amount_produces_a_null_result_beside_a_real_rate(broken: TestClient):
    body = call(broken, amount="nan", from_="EUR", to="TRY")

    assert body["result"] is None
    assert body["rate"] == 55.91


def test_an_unparseable_amount_uses_fastapis_error_shape(broken: TestClient):
    response = broken.get("/tools/convert", params={"amount": "abc", "from_": "EUR", "to": "TRY"})

    assert response.status_code == 422
    assert "detail" in response.json(), "a third body shape for the caller to parse"


# --- finding 7: configuration and lifecycle ----------------------------------


def test_the_upstream_is_hardcoded_so_it_cannot_be_pointed_anywhere_else():
    assert tool.UPSTREAM == "https://api.frankfurter.dev/v1"
    assert "environ" not in tool.__dict__, "FX_UPSTREAM_BASE and PORT do nothing"


def test_the_client_is_built_at_import_and_never_closed():
    from pathlib import Path

    source = Path(tool.__file__).read_text(encoding="utf-8")

    assert "client = httpx.AsyncClient()" in source, "built at import time"
    assert "aclose" not in source and "lifespan" not in source, "and never closed"


def test_the_cache_is_module_state_with_no_bound_and_no_expiry():
    source = __import__("pathlib").Path(tool.__file__).read_text(encoding="utf-8")

    assert "_cache: dict[str, float] = {}" in source
    assert "ttl" not in source.lower() and "expire" not in source.lower()
