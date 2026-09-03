"""End-to-end behaviour of GET /tools/convert, over a fake upstream."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from conftest import FakeUpstream, body_of, rates_response, raw_response


def convert(client, **params):
    return client.get("/tools/convert", params=params)


# --- the happy path ---------------------------------------------------------


def test_returns_the_documented_shape(client, upstream: FakeUpstream):
    upstream.always(rates_response("2026-08-28", {"TRY": 47.1234}))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert response.status_code == 200
    assert body_of(response) == {
        "amount": 250,
        "from": "EUR",
        "to": "TRY",
        "rate": Decimal("47.1234"),
        "result": Decimal("11780.85"),
        "rate_date": "2026-08-28",
        "asked_date": "2026-08-28",
        "source": "ECB via frankfurter.dev",
    }


def test_currency_codes_are_case_insensitive(client, upstream: FakeUpstream):
    upstream.always(rates_response("2026-09-02", {"TRY": 47.1234}))

    response = convert(client, amount=1, **{"from": "eur"}, to="try")

    assert response.status_code == 200
    assert body_of(response)["from"] == "EUR"
    assert upstream.requests[0].url.params["symbols"] == "TRY"


def test_omitting_the_date_asks_upstream_for_latest(client, upstream: FakeUpstream):
    response = convert(client, amount=10, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 200
    assert upstream.paths() == ["/v1/latest"]
    # The caller asked about now, and now is today in Frankfurt.
    assert body_of(response)["asked_date"] == "2026-09-03"


def test_the_published_rate_is_not_rounded(client, upstream: FakeUpstream):
    # A rate below 1 is where rounding does visible damage: 0.0234 -> 0.02
    # understates the result by 15%.
    upstream.always(raw_response('{"amount":1.0,"base":"USD","date":"2026-09-02","rates":{"XYZ":0.0234}}'))

    response = convert(client, amount=10000, **{"from": "USD"}, to="XYZ")

    body = body_of(response)
    assert body["rate"] == Decimal("0.0234")
    assert body["result"] == Decimal("234.00")


def test_result_uses_exact_arithmetic(client, upstream: FakeUpstream):
    # 1.15 * 3 is 3.45 exactly; in binary floating point it is 3.4499999...,
    # which rounds down to 3.44 under a naive round().
    upstream.always(raw_response('{"amount":1.0,"base":"EUR","date":"2026-09-02","rates":{"USD":1.15}}'))

    response = convert(client, amount=3, **{"from": "EUR"}, to="USD")

    assert body_of(response)["result"] == Decimal("3.45")


# --- dates ------------------------------------------------------------------


def test_a_closed_day_reports_the_date_the_rate_belongs_to(client, upstream: FakeUpstream):
    # 2026-08-29 is a Saturday. The ECB published nothing; Frankfurter answers
    # with Friday's fixing.
    upstream.always(rates_response("2026-08-28", {"TRY": 47.1234}))

    body = body_of(convert(client, amount=100, **{"from": "EUR"}, to="TRY", date="2026-08-29"))

    assert body["asked_date"] == "2026-08-29"
    assert body["rate_date"] == "2026-08-28"


def test_a_future_date_is_refused_without_calling_upstream(client, upstream: FakeUpstream):
    response = convert(client, amount=100, **{"from": "EUR"}, to="TRY", date="2030-01-01")

    assert response.status_code == 400
    assert response.json()["error"] == "future_date"
    assert upstream.call_count == 0


def test_a_date_before_the_ecb_series_is_refused(client, upstream: FakeUpstream):
    response = convert(client, amount=100, **{"from": "EUR"}, to="TRY", date="1998-06-01")

    assert response.status_code == 400
    assert response.json()["error"] == "date_out_of_range"
    assert upstream.call_count == 0


def test_an_unparseable_date_is_refused(client, upstream: FakeUpstream):
    response = convert(client, amount=100, **{"from": "EUR"}, to="TRY", date="28-08-2026")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_date"
    assert upstream.call_count == 0


def test_a_rate_dated_after_the_request_is_rejected(client, upstream: FakeUpstream):
    # If the upstream ever answered like this, publishing it would attach a
    # rate to a date it does not belong to.
    upstream.always(rates_response("2026-09-02", {"TRY": 47.1234}))

    response = convert(client, amount=100, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_invalid_response"


# --- currencies -------------------------------------------------------------


def test_identical_currencies_do_not_invent_a_source(client, upstream: FakeUpstream):
    response = convert(client, amount=250, **{"from": "EUR"}, to="EUR", date="2026-08-28")

    body = body_of(response)
    assert body["rate"] == 1
    assert body["result"] == Decimal("250")
    assert body["source"] == "identity"
    assert upstream.call_count == 0


@pytest.mark.parametrize("code", ["EURO", "E", "12", "€UR", ""])
def test_malformed_currency_codes_are_refused(client, upstream: FakeUpstream, code):
    response = convert(client, amount=100, **{"from": code}, to="TRY")

    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_currency", "missing_parameter"}
    assert upstream.call_count == 0


def test_a_currency_the_upstream_rejects_is_reported_as_unsupported(client, upstream: FakeUpstream):
    upstream.always(httpx.Response(404, json={"message": "not found"}))

    response = convert(client, amount=100, **{"from": "EUR"}, to="ZWL")

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_currency"


def test_a_missing_target_rate_is_reported_as_unsupported(client, upstream: FakeUpstream):
    upstream.always(rates_response("2026-09-02", {}))

    response = convert(client, amount=100, **{"from": "EUR"}, to="ZWL")

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_currency"


# --- amounts ----------------------------------------------------------------


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "abc", "1e400"])
def test_amounts_that_cannot_produce_a_true_answer_are_refused(client, upstream, amount):
    response = convert(client, amount=amount, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_amount"
    assert upstream.call_count == 0


def test_zero_is_a_legitimate_amount(client, upstream: FakeUpstream):
    response = convert(client, amount=0, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 200
    assert body_of(response)["result"] == 0


def test_an_amount_above_the_ceiling_is_refused(client, upstream: FakeUpstream):
    response = convert(client, amount="1000000000001", **{"from": "EUR"}, to="TRY")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_amount"
    assert upstream.call_count == 0


# --- upstream failures ------------------------------------------------------


def test_an_upstream_outage_is_never_dressed_up_as_a_rate(client, upstream: FakeUpstream):
    upstream.always(httpx.Response(503, text="upstream down"))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 502
    assert response.json() == {
        "error": "upstream_unavailable",
        "message": "The rate source could not be reached: HTTP 503",
    }


def test_a_timeout_is_reported_as_a_timeout(client, upstream: FakeUpstream):
    upstream.raises(httpx.ConnectTimeout("too slow"))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"


def test_a_connection_failure_is_reported_as_unavailable(client, upstream: FakeUpstream):
    upstream.raises(httpx.ConnectError("no route to host"))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"date": "2026-09-02"}',
        '{"rates": {"TRY": 47.1}}',
        '{"date": "2026-09-02", "rates": {"TRY": "not a number"}}',
        '{"date": "2026-09-02", "rates": {"TRY": -1}}',
        '{"date": "yesterday", "rates": {"TRY": 47.1}}',
    ],
)
def test_a_malformed_upstream_body_is_refused(client, upstream: FakeUpstream, body):
    upstream.always(raw_response(body))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY")

    assert response.status_code in (400, 502)
    assert "rate" not in response.json()


# --- the error contract itself ----------------------------------------------


@pytest.mark.parametrize("missing", ["amount", "from", "to"])
def test_missing_parameters_use_the_same_error_shape(client, missing):
    params = {"amount": 250, "from": "EUR", "to": "TRY"}
    params.pop(missing)

    response = client.get("/tools/convert", params=params)

    assert response.status_code == 400
    assert response.json() == {
        "error": "missing_parameter",
        "message": f"Query parameter '{missing}' is required.",
    }


def test_an_unknown_route_uses_the_same_error_shape(client):
    response = client.get("/tools/convertt")

    assert response.status_code == 404
    assert set(response.json()) == {"error", "message"}


def test_an_unexpected_failure_does_not_leak_a_number(settings, service):
    from fastapi.testclient import TestClient

    from app.main import create_app

    async def explode(*_args, **_kwargs):
        raise RuntimeError("something nobody predicted")

    service.convert = explode
    with TestClient(create_app(settings=settings, service=service), raise_server_exceptions=False) as broken:
        response = broken.get("/tools/convert", params={"amount": 1, "from": "EUR", "to": "TRY"})

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "The request could not be completed.",
    }


def test_health_is_liveness_only(client, upstream: FakeUpstream):
    assert client.get("/health").json() == {"ok": True}
    assert upstream.call_count == 0
