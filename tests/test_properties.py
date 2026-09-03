"""Invariants, checked against generated input rather than chosen examples.

The tests elsewhere say what happens for cases somebody thought of. These say
what must be true for every case, including the ones nobody thought of. All
three are the same rule from the README stated three ways: the service either
answers with a number that is right, or it does not answer with a number.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import httpx
from hypothesis import given, settings as hyp_settings, strategies as st

from conftest import TODAY, build_harness, raw_response

CENT = Decimal("0.01")
SUCCESS_KEYS = {"amount", "from", "to", "rate", "result", "rate_date", "asked_date", "source"}
ERROR_KEYS = {"error", "message"}

# Deliberately mixes the valid with the hostile: a property is only worth
# stating if the junk cases have to satisfy it too.
AMOUNTS = st.one_of(
    st.decimals(min_value=0, max_value=10**12, places=2, allow_nan=False, allow_infinity=False).map(str),
    st.sampled_from(["0", "-1", "nan", "-inf", "Infinity", "abc", "", "1e400", "1_000"]),
)
CURRENCIES = st.one_of(
    st.sampled_from(["EUR", "TRY", "USD", "eur", "try"]),
    st.sampled_from(["EURO", "E", "12", "€UR", "", "ZWL", " EUR "]),
)
DATES = st.one_of(
    st.dates(min_value=date(1999, 1, 4), max_value=TODAY).map(str),
    st.sampled_from(["2030-01-01", "1998-06-01", "28-08-2026", "", "today", None]),
)


@st.composite
def upstream(draw):
    """A responder that behaves the way frankfurter.dev can behave, or worse."""
    kind = draw(
        st.sampled_from(
            ["ok", "ok", "ok", "http_4xx", "http_5xx", "garbage", "no_rate", "bad_date",
             "late_date", "zero_rate", "transport_error"]
        )
    )
    rate = draw(
        st.decimals(
            min_value=Decimal("0.0001"),
            max_value=Decimal("1000000"),
            places=6,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    back = draw(st.integers(min_value=0, max_value=4))

    def responder(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbols"]
        asked = request.url.path.rsplit("/", 1)[-1]
        published = date(2026, 9, 2) if asked == "latest" else date.fromisoformat(asked)

        if kind == "transport_error":
            raise httpx.ConnectError("no route to host")
        if kind == "http_4xx":
            return httpx.Response(404, json={"message": "not found"})
        if kind == "http_5xx":
            return httpx.Response(503, text="down")
        if kind == "garbage":
            return raw_response("<html>maintenance</html>")
        if kind == "no_rate":
            return raw_response(json.dumps({"date": published.isoformat(), "rates": {}}))
        if kind == "bad_date":
            return raw_response('{"date": "whenever", "rates": {"%s": 1.5}}' % symbol)
        if kind == "zero_rate":
            return raw_response('{"date": "%s", "rates": {"%s": 0}}' % (published, symbol))
        if kind == "late_date":
            late = published + timedelta(days=3)
            return raw_response('{"date": "%s", "rates": {"%s": 1.5}}' % (late, symbol))

        day = max(published - timedelta(days=back), date(1999, 1, 4))
        return raw_response(
            '{"amount":1.0,"base":"X","date":"%s","rates":{"%s":%s}}' % (day, symbol, rate)
        )

    return responder


def call(responder, amount, base, target, when):
    harness = build_harness(responder=responder)
    params = {"amount": amount, "from": base, "to": target}
    if when is not None:
        params["date"] = when
    response = harness.client.get("/tools/convert", params=params)
    return response, json.loads(response.text, parse_float=Decimal)


@given(responder=upstream(), amount=AMOUNTS, base=CURRENCIES, target=CURRENCIES, when=DATES)
@hyp_settings(max_examples=250, deadline=None)
def test_a_response_is_either_a_whole_conversion_or_no_conversion(
    responder, amount, base, target, when
):
    """There is no third shape.

    Whatever the caller sends and whatever the upstream does, the body is
    either every success field with a usable number in it, or exactly an error
    code and a message. Nothing in between ever reaches an agent.
    """
    response, body = call(responder, amount, base, target, when)

    if response.status_code == 200:
        assert set(body) == SUCCESS_KEYS
        assert Decimal(str(body["rate"])) > 0
        assert body["source"]
        # The number has to survive the round trip through JSON intact.
        product = Decimal(str(body["amount"])) * Decimal(str(body["rate"]))
        assert Decimal(str(body["result"])) == product.quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        assert response.status_code >= 400
        assert set(body) == ERROR_KEYS
        assert body["error"] and body["message"]


@given(responder=upstream(), amount=AMOUNTS, base=CURRENCIES, target=CURRENCIES, when=DATES)
@hyp_settings(max_examples=250, deadline=None)
def test_a_rate_is_never_reported_under_a_day_it_was_not_published_for(
    responder, amount, base, target, when
):
    """The rule the whole service exists to keep.

    `rate_date` may be earlier than the day asked about, because the ECB does
    not publish every day. It may never be later, and it may never be a day
    the caller did not ask about without `asked_date` saying so.
    """
    response, body = call(responder, amount, base, target, when)
    if response.status_code != 200:
        return

    rate_date = date.fromisoformat(body["rate_date"])
    asked_date = date.fromisoformat(body["asked_date"])
    assert rate_date <= asked_date
    assert asked_date <= TODAY
    if when not in (None, "") and len(str(when)) == 10:
        assert body["asked_date"] == when


@given(
    amount=st.decimals(min_value=0, max_value=10**9, places=2, allow_nan=False, allow_infinity=False),
    currency=st.sampled_from(["EUR", "TRY", "USD", "JPY"]),
)
@hyp_settings(max_examples=100, deadline=None)
def test_converting_a_currency_to_itself_costs_nothing_and_changes_nothing(amount, currency):
    harness = build_harness()
    response = harness.client.get(
        "/tools/convert", params={"amount": str(amount), "from": currency, "to": currency}
    )

    body = json.loads(response.text, parse_float=Decimal)
    assert response.status_code == 200
    assert Decimal(str(body["rate"])) == 1
    assert Decimal(str(body["result"])) == amount.quantize(CENT, rounding=ROUND_HALF_UP)
    assert body["source"] == "identity"
    assert harness.upstream.call_count == 0
