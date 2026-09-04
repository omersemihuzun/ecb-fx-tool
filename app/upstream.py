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

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Protocol

import httpx

from . import errors
from .config import Settings
from .errors import FxError

# Long enough to clear a blip, short enough that a caller with a person waiting
# does not notice. Not configurable: a knob nobody would turn.
RETRY_BACKOFF_SECONDS = 0.25


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

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        timeout_seconds: float,
        attempts: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._attempts = max(1, attempts)
        self._sleep = sleep

    @classmethod
    def open(cls, settings: Settings) -> "FrankfurterRates":
        """Build one with its own client, to be closed with `aclose`."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"user-agent": "mangolab-fx-tool/1.0"},
        )
        return cls(
            client,
            settings.upstream_base,
            settings.upstream_timeout_seconds,
            settings.upstream_attempts,
        )

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
        """Fetch, retrying only what retrying can fix.

        A GET is idempotent, so one more attempt after a timeout or a 5xx costs
        the caller a little latency and nothing else. A 4xx is not retried: the
        request is wrong, and sending it again will not make it right.
        """
        transient: FxError | None = None
        for attempt in range(self._attempts):
            if attempt:
                await self._sleep(RETRY_BACKOFF_SECONDS)
            try:
                response = await self._client.get(url, params={"base": base, "symbols": target})
            except httpx.TimeoutException:
                transient = errors.upstream_timeout(self._timeout_seconds)
                continue
            except httpx.HTTPError as exc:
                transient = errors.upstream_unavailable(type(exc).__name__)
                continue

            if response.status_code >= 500:
                transient = errors.upstream_unavailable(f"HTTP {response.status_code}")
                continue
            if response.status_code == 404:
                # What Frankfurter answers for a code it does not carry. The
                # caller has already ruled the date out, so the pair is what is
                # left. Any other 4xx is a protocol change, not a bad pair, and
                # guessing "bad currency" there would misdirect the caller.
                raise errors.unsupported_currency(base, target)
            if response.status_code >= 400:
                raise errors.upstream_invalid_response(
                    f"an unexpected HTTP {response.status_code}."
                )
            return response

        assert transient is not None  # the loop runs at least once
        raise transient

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
