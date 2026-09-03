"""The one error shape this service returns.

Every non-2xx response has the same body::

    {"error": "<code>", "message": "<readable description>"}

`error` is a stable identifier a caller may branch on. `message` is for a
human reading a log and may be reworded at any time.

There is deliberately no success-shaped error. A caller that receives 200
has a number it can use; anything else is a refusal to answer.

`CATALOGUE` below is the only place a code and its status are written down.
`FxError` reads the status from it, so a code that is not catalogued cannot be
constructed, and `tests/test_error_catalogue.py` fails if the README documents
a different set. Documenting an error the service cannot produce is the same
class of defect as producing one it does not document.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

CATALOGUE: dict[str, int] = {
    "missing_parameter": 400,
    "invalid_amount": 400,
    "invalid_currency": 400,
    "invalid_date": 400,
    "invalid_request": 400,
    "unsupported_currency": 400,
    "future_date": 400,
    "date_out_of_range": 400,
    "not_found": 404,
    "method_not_allowed": 405,
    "not_representable": 422,
    "internal_error": 500,
    "upstream_unavailable": 502,
    "upstream_invalid_response": 502,
    "upstream_timeout": 504,
}


class FxError(Exception):
    """An error the caller is allowed to see. The status comes from CATALOGUE."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CATALOGUE:
            raise KeyError(f"{code!r} is not in the error catalogue")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = CATALOGUE[code]

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


def invalid_request(field: str, detail: str) -> FxError:
    return FxError("invalid_request", f"'{field}' is not valid: {detail}")


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


def not_representable(field: str, value: Decimal) -> FxError:
    return FxError(
        "not_representable",
        f"The exact value of '{field}' ({value:f}) cannot be carried as a JSON number. "
        f"Convert a smaller amount.",
    )


def not_found(path: str) -> FxError:
    return FxError("not_found", f"No endpoint at {path}.")


def method_not_allowed(method: str, path: str) -> FxError:
    return FxError("method_not_allowed", f"{method} is not allowed on {path}.")


def internal_error() -> FxError:
    # Deliberately says nothing. The detail belongs in the log, not the body.
    return FxError("internal_error", "The request could not be completed.")


def upstream_unavailable(detail: str) -> FxError:
    return FxError("upstream_unavailable", f"The rate source could not be reached: {detail}")


def upstream_invalid_response(detail: str) -> FxError:
    return FxError("upstream_invalid_response", f"The rate source returned {detail}")


def upstream_timeout(seconds: float) -> FxError:
    return FxError("upstream_timeout", f"The rate source did not answer within {seconds:g}s.")
