"""The one error shape this service returns.

Every non-2xx response has the same body::

    {"error": "<code>", "message": "<readable description>"}

`error` is a stable identifier a caller may branch on. `message` is for a
human reading a log and may be reworded at any time.

There is deliberately no success-shaped error. A caller that receives 200
has a number it can use; anything else is a refusal to answer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


class FxError(Exception):
    """An error the caller is allowed to see, with the status to send it under."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code

    def body(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def missing_parameter(name: str) -> FxError:
    return FxError("missing_parameter", f"Query parameter '{name}' is required.")


def invalid_amount(detail: str) -> FxError:
    return FxError("invalid_amount", detail)


def invalid_currency(name: str, value: str) -> FxError:
    return FxError(
        "invalid_currency",
        f"'{name}' must be a three-letter ISO 4217 code, got {value!r}.",
    )


def unsupported_currency(base: str, target: str) -> FxError:
    return FxError(
        "unsupported_currency",
        f"The ECB reference series does not cover the pair {base}/{target}.",
    )


def invalid_date(value: str) -> FxError:
    return FxError("invalid_date", f"'date' must be an ISO date (YYYY-MM-DD), got {value!r}.")


def future_date(asked: date, today: date) -> FxError:
    return FxError(
        "future_date",
        f"No rate exists for {asked.isoformat()}; the ECB has published up to {today.isoformat()}.",
    )


def date_out_of_range(asked: date, earliest: date) -> FxError:
    return FxError(
        "date_out_of_range",
        f"The ECB reference series starts on {earliest.isoformat()}; {asked.isoformat()} predates it.",
    )


def no_rate_available(base: str, target: str, asked: date) -> FxError:
    return FxError(
        "no_rate_available",
        f"No published {base}/{target} rate on or before {asked.isoformat()}.",
        status_code=404,
    )


def not_representable(field: str, value: Decimal) -> FxError:
    return FxError(
        "not_representable",
        f"The exact value of '{field}' ({value:f}) cannot be carried as a JSON number. "
        f"Convert a smaller amount.",
        status_code=422,
    )


def upstream_unavailable(detail: str) -> FxError:
    return FxError("upstream_unavailable", f"The rate source could not be reached: {detail}", 502)


def upstream_invalid_response(detail: str) -> FxError:
    return FxError("upstream_invalid_response", f"The rate source returned {detail}", 502)


def upstream_timeout(seconds: float) -> FxError:
    return FxError(
        "upstream_timeout",
        f"The rate source did not answer within {seconds:g}s.",
        status_code=504,
    )
