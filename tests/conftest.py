"""Test fixtures.

The suite never touches the network. Every upstream response is produced by
`FakeUpstream`, which is wired into httpx through a MockTransport, so the
service under test is the real service and only the socket is fake.

`build_harness` is the single constructor; the fixtures below are thin views
onto it, and the property tests call it directly because each generated example
needs its own cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fx import FxService
from app.main import create_app

# Fixed "today" so date behaviour is deterministic. A Thursday; the ECB
# publishes on it.
TODAY = date(2026, 9, 3)

Responder = Callable[[httpx.Request], httpx.Response]


def rates_body(rate_date: str, rates: dict[str, float], base: str = "EUR") -> dict:
    return {"amount": 1.0, "base": base, "date": rate_date, "rates": rates}


def rates_response(rate_date: str, rates: dict[str, float], base: str = "EUR") -> httpx.Response:
    return httpx.Response(200, json=rates_body(rate_date, rates, base))


def raw_response(body: str, status: int = 200) -> httpx.Response:
    """A response whose bytes are exactly `body`, for precision assertions."""
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": "application/json"}
    )


class FakeUpstream:
    """Stands in for api.frankfurter.dev. Counts calls, so caching is testable."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Responder = self._default

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def always(self, response: httpx.Response) -> None:
        self.responder = lambda _: response

    def raises(self, exc: Exception) -> None:
        def _raise(_: httpx.Request) -> httpx.Response:
            raise exc

        self.responder = _raise

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @staticmethod
    def _default(request: httpx.Request) -> httpx.Response:
        # EUR -> TRY at a plausible rate, dated on the last publication day
        # at or before the requested one.
        asked = request.url.path.rsplit("/", 1)[-1]
        rate_date = "2026-09-02" if asked == "latest" else asked
        return rates_response(rate_date, {request.url.params["symbols"]: 47.1234})


class Clock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Harness:
    upstream: FakeUpstream
    settings: Settings
    clock: Clock
    service: FxService
    client: TestClient


def build_harness(
    responder: Responder | None = None,
    settings: Settings | None = None,
    today: date = TODAY,
) -> Harness:
    upstream = FakeUpstream()
    if responder is not None:
        upstream.responder = responder
    settings = settings or Settings(
        upstream_base="https://upstream.test", latest_cache_ttl_seconds=600.0
    )
    clock = Clock()
    service = FxService(
        httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
        settings,
        today=lambda: today,
        monotonic=clock,
    )
    client = TestClient(create_app(settings=settings, service=service))
    return Harness(upstream, settings, clock, service, client)


@pytest.fixture
def harness() -> Harness:
    return build_harness()


@pytest.fixture
def upstream(harness: Harness) -> FakeUpstream:
    return harness.upstream


@pytest.fixture
def settings(harness: Harness) -> Settings:
    return harness.settings


@pytest.fixture
def clock(harness: Harness) -> Clock:
    return harness.clock


@pytest.fixture
def service(harness: Harness) -> FxService:
    return harness.service


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return harness.client


def body_of(response: httpx.Response) -> dict:
    """Parse a response body keeping numeric precision, for assertions."""
    return json.loads(response.text, parse_float=Decimal)
