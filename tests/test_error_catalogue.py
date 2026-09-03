"""The README's error table and the code are the same list, or this fails.

A documented error the service cannot return is a lie about the contract, and
an undocumented one a caller cannot plan for. Both are cheap to introduce and
invisible in review, so neither is left to review.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app import errors
from app.errors import CATALOGUE, FxError

ROOT = Path(__file__).resolve().parent.parent
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(\d{3})\s*\|")


def documented() -> dict[str, int]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return {
        match.group(1): int(match.group(2))
        for line in readme.splitlines()
        if (match := ROW.match(line))
    }


def constructed() -> set[str]:
    """Every code that appears in an FxError(...) call anywhere in app/."""
    found = set()
    for path in (ROOT / "app").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "FxError" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def test_the_readme_documents_exactly_the_catalogued_errors():
    assert documented() == CATALOGUE


def test_every_catalogued_code_is_actually_constructed_somewhere():
    # Catches the reverse drift: a code kept in the catalogue and the README
    # long after the branch that raised it was removed.
    assert constructed() == set(CATALOGUE)


def test_a_code_outside_the_catalogue_cannot_be_constructed():
    with pytest.raises(KeyError):
        FxError("something_i_just_made_up", "nope")


def test_every_factory_produces_a_catalogued_code():
    samples = [
        errors.missing_parameter("amount"),
        errors.invalid_amount("too big"),
        errors.invalid_currency("from", "EURO"),
        errors.invalid_date("nope"),
        errors.invalid_request("amount", "bad"),
        errors.unsupported_currency("EUR", "ZWL"),
        errors.not_found("/nowhere"),
        errors.method_not_allowed("POST", "/tools/convert"),
        errors.internal_error(),
        errors.upstream_unavailable("HTTP 503"),
        errors.upstream_invalid_response("nonsense"),
        errors.upstream_timeout(5.0),
    ]
    for error in samples:
        assert error.status_code == CATALOGUE[error.code]
        assert set(error.body()) == {"error", "message"}
        assert error.message.strip()
