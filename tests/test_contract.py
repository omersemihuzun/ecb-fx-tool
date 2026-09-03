"""The published contract.

An agent decides how to call this tool by reading `/openapi.json`, so the
schema is part of the product. These tests fail if the documented shape and
the served shape drift apart.
"""

from __future__ import annotations

from conftest import body_of, raw_response

DOCUMENTED_FIELDS = ["amount", "from", "to", "rate", "result", "rate_date", "asked_date", "source"]


def test_the_schema_documents_exactly_the_fields_the_service_sends(client, upstream):
    upstream.always(raw_response('{"date":"2026-08-28","rates":{"TRY":47.1234}}'))
    served = body_of(
        client.get(
            "/tools/convert",
            params={"amount": 250, "from": "EUR", "to": "TRY", "date": "2026-08-28"},
        )
    )

    schema = client.get("/openapi.json").json()
    documented = schema["components"]["schemas"]["ConvertResponse"]["properties"]

    assert list(documented) == DOCUMENTED_FIELDS
    assert list(served) == DOCUMENTED_FIELDS


def test_the_schema_names_the_query_parameters_the_endpoint_accepts(client):
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/tools/convert"]["get"]["parameters"]

    assert [p["name"] for p in parameters] == ["amount", "from", "to", "date"]
    assert [p["name"] for p in parameters if p["required"]] == ["amount", "from", "to"]


def test_every_documented_failure_status_carries_the_error_shape(client):
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/tools/convert"]["get"]["responses"]

    for status in ("400", "404", "422", "502", "504"):
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
    assert schema["components"]["schemas"]["ErrorResponse"]["required"] == ["error", "message"]
