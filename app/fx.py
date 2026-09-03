"""Conversion logic: validation, the upstream call, and the cache in front of it.

Everything that decides whether an answer is trustworthy lives here, so it can
be tested without an HTTP server and without a network.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

from . import errors
from .config import ECB_SERIES_START, ECB_TIMEZONE, Settings

IDENTITY_SOURCE = "identity"
ECB_SOURCE = "ECB via frankfurter.dev"

_CENT = Decimal("0.01")


def ecb_today() -> date:
    return datetime.now(ZoneInfo(ECB_TIMEZONE)).date()


@dataclass(frozen=True)
class Quote:
    """A rate, and the date the ECB actually published it for."""

    rate: Decimal
    rate_date: date


@dataclass(frozen=True)
class Conversion:
    amount: Decimal
    base: str
    target: str
    rate: Decimal
    result: Decimal
    rate_date: date
    asked_date: date
    source: str


class _Cache:
    """Bounded, least-recently-used, with an optional per-entry expiry.

    Bounded because an unbounded dict keyed by user input is a memory leak
    with extra steps. Per-entry expiry because a rate for a past date is
    final, while `latest` changes every business afternoon.
    """

    def __init__(self, max_entries: int, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._entries: OrderedDict[tuple[str, str, str], tuple[Quote, float | None]] = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> Quote | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        quote, expires_at = entry
        if expires_at is not None and self._monotonic() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return quote

    def put(self, key: tuple[str, str, str], quote: Quote, ttl_seconds: float | None) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            return
        expires_at = None if ttl_seconds is None else self._monotonic() + ttl_seconds
        self._entries[key] = (quote, expires_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class FxService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        today: Callable[[], date] = ecb_today,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._today = today
        self._cache = _Cache(settings.cache_max_entries, monotonic)
        self._inflight: dict[tuple[str, str, str], asyncio.Task[Quote]] = {}

    # -- public ---------------------------------------------------------

    async def convert(
        self,
        amount: Decimal,
        base: str,
        target: str,
        asked_date: date | None,
    ) -> Conversion:
        amount = self._check_amount(amount)
        base = self._check_currency("from", base)
        target = self._check_currency("to", target)

        today = self._today()
        effective_date = asked_date if asked_date is not None else today

        if effective_date < ECB_SERIES_START:
            raise errors.date_out_of_range(effective_date, ECB_SERIES_START)
        if effective_date > today:
            # Frankfurter answers an out-of-range date with a bare 404, which is
            # the same answer it gives for a currency it does not carry. Ruling
            # the date out here is what lets the caller be told which of the two
            # actually went wrong.
            raise errors.future_date(effective_date, today)

        if base == target:
            # No rate is involved, so no rate source is credited. One unit of
            # a currency is one unit of it on every date the series covers.
            return Conversion(
                amount=amount,
                base=base,
                target=target,
                rate=Decimal(1),
                result=amount.quantize(_CENT, rounding=ROUND_HALF_UP),
                rate_date=effective_date,
                asked_date=effective_date,
                source=IDENTITY_SOURCE,
            )

        quote = await self._quote(base, target, asked_date, today)
        return Conversion(
            amount=amount,
            base=base,
            target=target,
            rate=quote.rate,
            result=(amount * quote.rate).quantize(_CENT, rounding=ROUND_HALF_UP),
            rate_date=quote.rate_date,
            asked_date=effective_date,
            source=ECB_SOURCE,
        )

    # -- validation -----------------------------------------------------

    def _check_amount(self, amount: Decimal) -> Decimal:
        if not amount.is_finite():
            raise errors.invalid_amount("'amount' must be a finite number.")
        if amount < 0:
            raise errors.invalid_amount("'amount' must not be negative.")
        if amount > self._settings.max_amount:
            raise errors.invalid_amount(
                f"'amount' must not exceed {self._settings.max_amount:f}."
            )
        return amount

    @staticmethod
    def _check_currency(name: str, value: str) -> str:
        candidate = value.strip().upper()
        if len(candidate) != 3 or not candidate.isascii() or not candidate.isalpha():
            raise errors.invalid_currency(name, value)
        return candidate

    # -- fetching -------------------------------------------------------

    async def _quote(
        self,
        base: str,
        target: str,
        asked_date: date | None,
        today: date,
    ) -> Quote:
        # A caller who named no date wants the newest published rate, which is
        # not the same question as "the rate for today" on a day before the
        # 16:00 CET fixing. Keep the two as separate cache keys.
        path = "latest" if asked_date is None else asked_date.isoformat()
        key = (base, target, path)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # Rates for a closed day never change; anything touching today still can.
        final = asked_date is not None and asked_date < today
        ttl = None if final else self._settings.latest_cache_ttl_seconds

        # Single-flight: a burst of identical agent calls should cost the
        # upstream one request, not one per caller.
        task = self._inflight.get(key)
        if task is not None:
            return await asyncio.shield(task)

        task = asyncio.create_task(self._fetch(base, target, path, asked_date))
        self._inflight[key] = task
        try:
            quote = await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)

        self._cache.put(key, quote, ttl)
        return quote

    async def _fetch(self, base: str, target: str, path: str, asked_date: date | None) -> Quote:
        url = f"{self._settings.upstream_base}/v1/{path}"
        try:
            response = await self._client.get(url, params={"base": base, "symbols": target})
        except httpx.TimeoutException as exc:
            raise errors.upstream_timeout(self._settings.upstream_timeout_seconds) from exc
        except httpx.HTTPError as exc:
            raise errors.upstream_unavailable(type(exc).__name__) from exc

        if response.status_code >= 500:
            raise errors.upstream_unavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # Frankfurter answers 404 for a code it does not carry. The date
            # has already been range-checked above, so the pair is what is left.
            raise errors.unsupported_currency(base, target)

        payload = self._parse(response)
        rate = self._read_rate(payload, base, target)
        rate_date = self._read_date(payload, asked_date)
        return Quote(rate=rate, rate_date=rate_date)

    # -- parsing --------------------------------------------------------

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        try:
            # parse_float keeps the published precision; going through a
            # binary float first and rounding later is how rates drift.
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
    def _read_date(payload: dict, asked_date: date | None) -> date:
        raw = payload.get("date")
        if not isinstance(raw, str):
            raise errors.upstream_invalid_response("a response with no 'date'.")
        try:
            rate_date = date.fromisoformat(raw)
        except ValueError as exc:
            raise errors.upstream_invalid_response(f"an unparseable date ({raw!r}).") from exc

        # The upstream may legitimately answer with an earlier date than the
        # one asked for, on a weekend or a holiday. A later one would mean we
        # are about to label a rate with a date it does not belong to.
        if asked_date is not None and rate_date > asked_date:
            raise errors.upstream_invalid_response(
                f"a rate dated {raw} for a request about {asked_date.isoformat()}."
            )
        if rate_date < ECB_SERIES_START:
            raise errors.upstream_invalid_response(f"a date outside the ECB series ({raw}).")
        return rate_date
