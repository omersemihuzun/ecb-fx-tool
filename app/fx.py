"""The rules.

Everything that decides whether an answer can be trusted is here, and nothing
else is. No HTTP, no JSON, no expiry arithmetic: those are in `upstream.py` and
`cache.py`, behind interfaces this module can be tested against without a
network and without a clock.

The rule the rest follows from: the service never reports a rate under a date
the ECB did not publish it for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable
from zoneinfo import ZoneInfo

from . import errors
from .cache import SharedCache
from .config import ECB_SERIES_START, ECB_TIMEZONE, Settings
from .upstream import Quote, RateSource

IDENTITY_SOURCE = "identity"
ECB_SOURCE = "ECB via frankfurter.dev"

_CENT = Decimal("0.01")

#: (base, target, the date asked about, or None for "the newest published").
CacheKey = tuple[str, str, date | None]


def ecb_today() -> date:
    """Today on the calendar the ECB publishes against, not the server's."""
    return datetime.now(ZoneInfo(ECB_TIMEZONE)).date()


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


class FxService:
    def __init__(
        self,
        rates: RateSource,
        settings: Settings,
        today: Callable[[], date] = ecb_today,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rates = rates
        self._settings = settings
        self._today = today
        self._cache: SharedCache[CacheKey, Quote] = SharedCache(
            settings.cache_max_entries, monotonic
        )

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
        effective_date = self._check_date(asked_date, today)

        if base == target:
            # No rate is involved, so no rate source is credited. One unit of a
            # currency is one unit of it on every date the series covers.
            return self._conversion(
                amount, base, target, Decimal(1), effective_date, effective_date, IDENTITY_SOURCE
            )

        quote = await self._quote(base, target, asked_date, today)
        self._check_quote_date(quote, effective_date)
        return self._conversion(
            amount, base, target, quote.rate, quote.rate_date, effective_date, ECB_SOURCE
        )

    # -- rules ------------------------------------------------------------

    def _check_amount(self, amount: Decimal) -> Decimal:
        if not amount.is_finite():
            raise errors.invalid_amount("'amount' must be a finite number.")
        if amount < 0:
            raise errors.invalid_amount("'amount' must not be negative.")
        if amount > self._settings.max_amount:
            raise errors.invalid_amount(f"'amount' must not exceed {self._settings.max_amount:f}.")
        return amount

    @staticmethod
    def _check_currency(name: str, value: str) -> str:
        candidate = value.strip().upper()
        if len(candidate) != 3 or not candidate.isascii() or not candidate.isalpha():
            raise errors.invalid_currency(name, value)
        return candidate

    @staticmethod
    def _check_date(asked_date: date | None, today: date) -> date:
        effective = asked_date if asked_date is not None else today
        if effective < ECB_SERIES_START:
            raise errors.date_out_of_range(effective, ECB_SERIES_START)
        if effective > today:
            # Frankfurter answers an out-of-range date with a bare 404, the same
            # answer it gives for a currency it does not carry. Ruling the date
            # out here is what lets the caller be told which of the two it was.
            raise errors.future_date(effective, today)
        return effective

    @staticmethod
    def _check_quote_date(quote: Quote, asked_date: date) -> None:
        """The one thing a rate source is not allowed to get wrong.

        An earlier date than the one asked about is normal: the ECB does not
        publish at weekends, and the rate is carried forward. A later one would
        mean labelling a rate with a day it does not belong to, which is the
        failure this whole service is built to avoid.
        """
        if quote.rate_date > asked_date:
            raise errors.upstream_invalid_response(
                f"a rate dated {quote.rate_date.isoformat()} for a request about "
                f"{asked_date.isoformat()}."
            )
        if quote.rate_date < ECB_SERIES_START:
            raise errors.upstream_invalid_response(
                f"a date outside the ECB series ({quote.rate_date.isoformat()})."
            )

    # -- fetching ---------------------------------------------------------

    async def _quote(self, base: str, target: str, asked_date: date | None, today: date) -> Quote:
        # "The newest published rate" and "the rate for today" are different
        # questions on a morning before the fixing, so they are different keys.
        key: CacheKey = (base, target, asked_date)

        # A rate for a day that is over is final. Anything touching today can
        # still change when the 16:00 CET fixing lands.
        settled = asked_date is not None and asked_date < today
        ttl = None if settled else self._settings.latest_cache_ttl_seconds

        return await self._cache.get_or_fetch(
            key, ttl, lambda: self._rates.quote(base, target, asked_date)
        )

    # -- assembly ---------------------------------------------------------

    @staticmethod
    def _conversion(
        amount: Decimal,
        base: str,
        target: str,
        rate: Decimal,
        rate_date: date,
        asked_date: date,
        source: str,
    ) -> Conversion:
        return Conversion(
            amount=amount,
            base=base,
            target=target,
            rate=rate,
            result=(amount * rate).quantize(_CENT, rounding=ROUND_HALF_UP),
            rate_date=rate_date,
            asked_date=asked_date,
            source=source,
        )
