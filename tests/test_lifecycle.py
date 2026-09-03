"""The parts that only run in production, and the branches nothing else reaches.

The rest of the suite hands the app a service wired to a fake transport, which
means the code that builds and closes the real one is never exercised by it.
That code is exactly what REVIEW.md faults `tool.py` for, so it gets a test.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import ECB_TIMEZONE, ConfigError, Settings
from app.errors import FxError
from app.fx import FxService, ecb_today
from app.main import create_app
from conftest import TODAY, build_harness, rates_response


def test_the_app_opens_its_own_client_and_closes_it_on_shutdown():
    app = create_app(settings=Settings(upstream_base="https://upstream.test"))

    with TestClient(app):
        service = app.state.service
        assert isinstance(service, FxService)
        client = service._client
        assert client.is_closed is False

    assert client.is_closed


def test_the_client_is_built_with_the_configured_timeout():
    app = create_app(settings=Settings(upstream_base="https://x.test", upstream_timeout_seconds=1.5))

    with TestClient(app):
        assert app.state.service._client.timeout.read == 1.5


def test_today_is_read_off_the_ecb_calendar_not_the_local_one():
    # The only assertion that holds wherever this runs: it is Frankfurt's date.
    assert ecb_today() == datetime.now(ZoneInfo(ECB_TIMEZONE)).date()


# --- error paths nothing else reaches ---------------------------------------


def test_the_wrong_method_gets_the_service_error_shape(client):
    response = client.post("/tools/convert", params={"amount": 1, "from": "EUR", "to": "TRY"})

    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"


def test_an_unexpected_http_exception_never_leaks_its_own_shape():
    app = create_app(settings=Settings(upstream_base="https://x.test"))

    @app.get("/boom")
    async def boom() -> None:
        raise StarletteHTTPException(status_code=431, detail="headers too large")

    with TestClient(app, raise_server_exceptions=False) as probe:
        response = probe.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


async def test_a_non_finite_amount_is_refused_at_the_service_boundary(service: FxService):
    # FastAPI rejects "nan" before the service sees it. The service does not
    # rely on that, because it is also called directly from these tests.
    with pytest.raises(FxError) as caught:
        await service.convert(Decimal("NaN"), "EUR", "TRY", None)

    assert caught.value.code == "invalid_amount"


async def test_a_rate_dated_before_the_ecb_series_is_rejected(service: FxService, upstream):
    upstream.always(rates_response("1998-12-31", {"TRY": 47.1234}))

    with pytest.raises(FxError) as caught:
        await service.convert(Decimal(1), "EUR", "TRY", None)

    assert caught.value.code == "upstream_invalid_response"


# --- configuration -----------------------------------------------------------


def test_caching_can_be_turned_off_entirely(upstream):
    harness = build_harness(
        settings=Settings(upstream_base="https://upstream.test", latest_cache_ttl_seconds=0.0)
    )
    for _ in range(3):
        harness.client.get("/tools/convert", params={"amount": 1, "from": "EUR", "to": "TRY"})

    assert harness.upstream.call_count == 3


def test_a_non_numeric_duration_stops_the_process_at_startup():
    with pytest.raises(ConfigError):
        Settings.from_env({"FX_UPSTREAM_TIMEOUT_SECONDS": "soon"})


# --- single flight, when the shared call goes wrong --------------------------


async def test_concurrent_callers_all_see_the_same_failure(service: FxService, upstream):
    upstream.always(httpx.Response(503))

    results = await asyncio.gather(
        *(service.convert(Decimal(1), "EUR", "TRY", None) for _ in range(5)),
        return_exceptions=True,
    )

    assert upstream.call_count == 1
    assert {type(r) for r in results} == {FxError}
    assert {r.code for r in results} == {"upstream_unavailable"}


async def test_one_caller_giving_up_does_not_take_the_others_with_it(service: FxService, upstream):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        upstream.requests.append(request)
        started.set()
        await release.wait()
        return rates_response("2026-09-02", {"TRY": 47.1234})

    service._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))

    first = asyncio.create_task(service.convert(Decimal(1), "EUR", "TRY", None))
    second = asyncio.create_task(service.convert(Decimal(1), "EUR", "TRY", None))
    await started.wait()

    # The caller that opened the upstream request disconnects.
    first.cancel()
    release.set()
    conversion = await second

    assert conversion.rate == Decimal("47.1234")
    assert upstream.call_count == 1
