"""HTTP surface. Parsing in, one shape out, no business rules of its own."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import errors
from .config import Settings
from .errors import FxError
from .fx import Conversion, FxService

logger = logging.getLogger("fx")


class ConvertResponse(BaseModel):
    """The success shape. Documented here so an agent can read the schema."""

    model_config = ConfigDict(populate_by_name=True)

    amount: int | float
    from_: str = Field(alias="from", examples=["EUR"])
    to: str = Field(examples=["TRY"])
    rate: int | float = Field(description="Units of 'to' per one unit of 'from', as published.")
    result: int | float = Field(description="amount * rate, rounded to two decimal places.")
    rate_date: date = Field(description="The date the ECB published this rate for.")
    asked_date: date = Field(description="The date the caller asked about.")
    source: str = Field(description="Where the rate came from, or 'identity' if none was needed.")


class ErrorResponse(BaseModel):
    error: str
    message: str


def _number(value: Decimal) -> int | float:
    """Render a Decimal as a JSON number.

    All arithmetic upstream of this is exact. JSON has no decimal type, so the
    value becomes a float here and nowhere earlier. Integral values are emitted
    as integers so `amount=250` comes back as 250.
    """
    return int(value) if value == value.to_integral_value() else float(value)


def _body(conversion: Conversion) -> dict[str, Any]:
    return {
        "amount": _number(conversion.amount),
        "from": conversion.base,
        "to": conversion.target,
        "rate": _number(conversion.rate),
        "result": _number(conversion.result),
        "rate_date": conversion.rate_date.isoformat(),
        "asked_date": conversion.asked_date.isoformat(),
        "source": conversion.source,
    }


def get_service(request: Request) -> FxService:
    return request.app.state.service


def create_app(settings: Settings | None = None, service: FxService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            # Tests hand in a service wired to a fake transport.
            app.state.service = service
            yield
            return

        # One client for the process, opened and closed with it. Creating it
        # at import time binds it to whichever event loop happened to exist.
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"user-agent": "mangolab-fx-tool/1.0"},
        )
        app.state.service = FxService(client, settings)
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="fx-tool",
        version="1.0",
        summary="Currency conversion over ECB reference rates, for agent tool calls.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    if service is not None:
        app.state.service = service

    _register_routes(app)
    _register_error_handlers(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get(
        "/tools/convert",
        response_model=ConvertResponse,
        response_model_by_alias=True,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
        summary="Convert an amount between two currencies.",
        description=(
            "Returns the ECB reference rate for the requested date. If the ECB "
            "published nothing that day, the most recent earlier rate is returned "
            "and `rate_date` says so; `rate_date` is never the date you asked for "
            "unless a rate was really published then."
        ),
    )
    async def convert(
        amount: Decimal = Query(..., description="How much to convert.", examples=[250]),
        from_: str = Query(..., alias="from", description="Source currency, ISO 4217.", examples=["EUR"]),
        to: str = Query(..., description="Target currency, ISO 4217.", examples=["TRY"]),
        asked_date: date | None = Query(
            None,
            alias="date",
            description="Rate date, YYYY-MM-DD. Defaults to the latest published rate.",
        ),
        service: FxService = Depends(get_service),
    ) -> Any:
        conversion = await service.convert(amount, from_, to, asked_date)
        return _body(conversion)

    @app.get("/health", summary="Liveness only. Says nothing about the rate source.")
    async def health() -> dict[str, bool]:
        return {"ok": True}


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FxError)
    async def _fx_error(_: Request, exc: FxError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.warning("%s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        translated = _translate_validation_error(exc)
        return JSONResponse(status_code=translated.status_code, content=translated.body())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        code = codes.get(exc.status_code, "request_rejected")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": str(exc.detail)},
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Nothing gets to leave as a 200 with a made-up number in it.
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "The request could not be completed."},
        )


def _translate_validation_error(exc: RequestValidationError) -> FxError:
    """Turn FastAPI's validation report into this service's error shape.

    A caller should never have to parse two different error formats depending
    on how far into the handler the request got.
    """
    first = exc.errors()[0]
    field = str(first["loc"][-1])
    field = {"from_": "from", "asked_date": "date"}.get(field, field)
    given = first.get("input")

    if first["type"] == "missing":
        return errors.missing_parameter(field)
    if field == "amount":
        return errors.invalid_amount(f"'amount' must be a number, got {given!r}.")
    if field == "date":
        return errors.invalid_date(str(given))
    if field in ("from", "to"):
        return errors.invalid_currency(field, str(given))
    return FxError("invalid_request", f"'{field}' is not valid: {first['msg']}")


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
