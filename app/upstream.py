"""Everything that knows frankfurter.dev exists.

The URL scheme, the meaning of its status codes, and the shape of its JSON stop
here. What comes out is a `Quote`: a rate and the day it was published for,
both already checked for being the right kind of thing. Whether that day is an
acceptable answer to the question that was asked is a rule, and rules live in
`fx.py`.

Swapping rate providers should mean writing another class with this `quote`
method and changing one line in `main.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

from . import errors
from .config import Settings


@dataclass(frozen=True)
class Quote:
    """A rate, and the date the publisher says it belongs to."""

    rate: Decimal
    rate_date: date


class RateSource(Protocol):
    """What `FxService` needs from a rate provider, and nothing more."""

    async def quote(self, base: str, target: str, on: date | None) -> Quote:
        """The rate for `on`, or the most recent one if `on` is None."""


class FrankfurterRates:
    """`RateSource` backed by the ECB reference series at frankfurter.dev."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout_seconds: float) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def open(cls, settings: Settings) -> "FrankfurterRates":
        """Build one with its own client, to be closed with `aclose`."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"user-agent": "mangolab-fx-tool/1.0"},
        )
        return cls(client, settings.upstream_base, settings.upstream_timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def quote(self, base: str, target: str, on: date | None) -> Quote:
        # A caller who named no date wants the newest published rate, which is
        # not the same question as "the rate for today" on a morning before the
        # 16:00 CET fixing.
        path = "latest" if on is None else on.isoformat()
        response = await self._get(f"{self._base_url}/v1/{path}", base, target)
        payload = self._parse(response)
        return Quote(rate=self._read_rate(payload, base, target), rate_date=self._read_date(payload))

    # -- transport --------------------------------------------------------

    async def _get(self, url: str, base: str, target: str) -> httpx.Response:
        try:
            response = await self._client.get(url, params={"base": base, "symbols": target})
        except httpx.TimeoutException as exc:
            raise errors.upstream_timeout(self._timeout_seconds) from exc
        except httpx.HTTPError as exc:
            raise errors.upstream_unavailable(type(exc).__name__) from exc

        if response.status_code >= 500:
            raise errors.upstream_unavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # Frankfurter answers 404 for a code it does not carry and for a
            # date outside its range. The caller has already ruled the date out,
            # so the pair is what is left.
            raise errors.unsupported_currency(base, target)
        return response

    # -- parsing ----------------------------------------------------------

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        try:
            # parse_float keeps the published precision; going through a binary
            # float first and rounding later is how rates drift.
            payload = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise errors.upstream_invalid_response("a body that is not JSON.") from exc
        if not isinstance(payload, dict):
            raise errors.upstream_invalid_response("a JSON value that is not an object.")
        return payload

    @staticmethod
    def _read_rate(payload: dict, base: str, target: str) -> Decimal:
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            raise errors.upstream_invalid_response("a response with no 'rates' object.")
        if target not in rates:
            raise errors.unsupported_currency(base, target)

        raw = rates[target]
        try:
            rate = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise errors.upstream_invalid_response(f"a non-numeric rate ({raw!r}).") from exc
        if not rate.is_finite() or rate <= 0:
            raise errors.upstream_invalid_response(f"a rate that cannot be true ({raw!r}).")
        return rate

    @staticmethod
    def _read_date(payload: dict) -> date:
        raw = payload.get("date")
        if not isinstance(raw, str):
            raise errors.upstream_invalid_response("a response with no 'date'.")
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise errors.upstream_invalid_response(f"an unparseable date ({raw!r}).") from exc
