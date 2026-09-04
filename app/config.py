"""Runtime configuration. Read once, at startup, so a bad value fails loudly
before the service starts accepting traffic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

# The ECB reference series starts on 1999-01-04. frankfurter.dev answers
# requests for earlier dates with the oldest rates it holds, and that answer
# is indistinguishable from a real one, so we refuse those dates ourselves.
ECB_SERIES_START = date(1999, 1, 4)

# The ECB fixes and publishes reference rates on TARGET business days at
# around 16:00 CET. "Today" therefore has to mean today in Frankfurt, not
# today on whatever machine happens to be running this process.
ECB_TIMEZONE = "Europe/Berlin"

DEFAULT_UPSTREAM_BASE = "https://api.frankfurter.dev"

# How far a published rate may sit behind the date asked about before the
# service refuses to use it. The longest real gap in the series since 2019 is
# five days, at Easter, so seven never touches a legitimate closure. A larger
# gap means the feed has stopped, or the pair is no longer published, and a
# months-old rate is not an answer to today's question however honestly it is
# dated.
DEFAULT_MAX_STALENESS_DAYS = 7


class ConfigError(ValueError):
    """Raised when an environment variable is present but unusable."""


def _read(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _number(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    upstream_base: str = DEFAULT_UPSTREAM_BASE
    port: int = 8080

    # Frankfurter is a small static-ish service; if it has not answered in
    # five seconds it is not going to, and the caller is an agent with a
    # user waiting on the other end.
    upstream_timeout_seconds: float = 5.0

    # A read is idempotent, so one retry on a timeout or a 5xx costs a caller
    # latency and nothing else. Not retried on a 4xx: the request is wrong and
    # sending it again will not fix it.
    upstream_attempts: int = 2

    max_staleness_days: int = DEFAULT_MAX_STALENESS_DAYS

    # Only applies to quotes that can still change. Rates for a past date
    # are final and are cached without expiry. See fx.FxService.
    latest_cache_ttl_seconds: float = 600.0
    cache_max_entries: int = 4096

    # A ceiling on the input, not on the business. It exists so that a
    # malformed or hostile amount cannot turn into an answer at all.
    max_amount: Decimal = Decimal("1000000000000")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        defaults = cls()

        port_raw = _read(env, "PORT")
        if port_raw is None:
            port = defaults.port
        else:
            try:
                port = int(port_raw)
            except ValueError as exc:
                raise ConfigError(f"PORT must be an integer, got {port_raw!r}") from exc
            if not 1 <= port <= 65535:
                raise ConfigError(f"PORT must be between 1 and 65535, got {port}")

        max_amount_raw = _read(env, "FX_MAX_AMOUNT")
        if max_amount_raw is None:
            max_amount = defaults.max_amount
        else:
            try:
                max_amount = Decimal(max_amount_raw)
            except InvalidOperation as exc:
                raise ConfigError(
                    f"FX_MAX_AMOUNT must be a decimal number, got {max_amount_raw!r}"
                ) from exc
            if not max_amount.is_finite() or max_amount <= 0:
                raise ConfigError(f"FX_MAX_AMOUNT must be finite and positive, got {max_amount}")

        return cls(
            upstream_base=(_read(env, "FX_UPSTREAM_BASE") or defaults.upstream_base).rstrip("/"),
            port=port,
            upstream_timeout_seconds=_number(
                env, "FX_UPSTREAM_TIMEOUT_SECONDS", defaults.upstream_timeout_seconds, minimum=0.1
            ),
            upstream_attempts=int(
                _number(env, "FX_UPSTREAM_ATTEMPTS", defaults.upstream_attempts, minimum=1.0)
            ),
            max_staleness_days=int(
                _number(env, "FX_MAX_STALENESS_DAYS", defaults.max_staleness_days, minimum=1.0)
            ),
            latest_cache_ttl_seconds=_number(
                env, "FX_CACHE_TTL_SECONDS", defaults.latest_cache_ttl_seconds, minimum=0.0
            ),
            cache_max_entries=int(
                _number(env, "FX_CACHE_MAX_ENTRIES", defaults.cache_max_entries, minimum=1.0)
            ),
            max_amount=max_amount,
        )
