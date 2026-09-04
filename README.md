# fx-tool

A currency conversion endpoint for an agent to call, over ECB reference rates
published by [frankfurter.dev](https://frankfurter.dev).

The whole design follows from one rule: **a wrong number is worse than no
number.** Anything the service is not sure of comes back as an error with a
status code, never as a plausible figure. In particular, it never reports a
rate under a date the ECB did not publish it for.

```
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

```json
{
  "amount": 250,
  "from": "EUR",
  "to": "TRY",
  "rate": 47.1234,
  "result": 11780.85,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

## Running it

```bash
./run.sh                 # http://localhost:8080
PORT=9000 ./run.sh
```

`run.sh` creates `.venv` on first run and installs `requirements.txt` into it.
Python 3.11 or newer. Nothing else is needed: no database, no keys, no Docker.
On Windows both scripts run under Git Bash; PowerShell cannot execute them.

Interactive schema at `/docs`, machine-readable at `/openapi.json`. `/health`
is a liveness probe and deliberately says nothing about the upstream: if
frankfurter.dev is down, this process is still healthy and should not be
restarted for it.

## Design

Six modules, and the dependencies only point one way.

```
main.py       HTTP. Parse in, one shape out. Holds no rules.
  fx.py       the rules. No HTTP, no JSON, no expiry arithmetic.
    upstream.py   everything that knows frankfurter.dev exists
    cache.py      "do not ask that again", and nothing about rates
  errors.py   the one error shape, and the catalogue of codes
  config.py   the environment, read once at startup
```

`fx.py` is the file worth reading. It holds every decision about whether an
answer can be trusted — what a usable amount is, which dates are answerable,
what an identical pair means, and the one check a rate source is not allowed to
fail — and it holds nothing else. That is why it is eighty statements, and why
its tests need no server, no socket and no clock.

Its two dependencies exist because they are the two things that would otherwise
smear across it.

**`upstream.py`** is where the provider lives. The URL scheme, the meaning of
its status codes, and the shape of its JSON stop there; what comes out is a
rate and the day it was published for. `fx.py` depends on the `RateSource`
protocol rather than on the class, so swapping providers means writing another
class with a `quote` method and changing one line in `main.py`.

**`cache.py`** knows nothing about currencies. It has one method,
`get_or_fetch`, behind which sit an expiry, a size bound, and the coalescing
that makes five simultaneous identical calls cost the upstream one request.
Those arrive as one problem — "do not ask that again" — so they are solved in
one place, and tested with a counter for a fetch function rather than through a
conversion.

The seam is not decoration. Before it, the tests reached into `service._client`
to install a fake and into `service._cache` to assert a bound. A test reaching
past a public surface is the object saying out loud that it has more than one
job. No test touches a private attribute now.

## Testing

```bash
./test.sh                # 90 tests, no network touched
./test.sh -m live        # 5 more, against the real frankfurter.dev
```

Two fakes, for two different jobs. Most tests replace only the HTTP transport,
so `upstream.py` runs for real and only the socket is simulated. Tests about
coordination — five callers sharing one fetch, a caller disconnecting — replace
the whole `RateSource`, because they are not about HTTP and the fixture should
say so.

Most of the suite is examples. Three cases are not: `tests/test_properties.py`
generates requests and upstream behaviour and asserts the rules rather than the
cases — that a 200 always carries a complete conversion whose result is exactly
the amount times the rate, that `rate_date` is never later than the day asked
about, and that an identical pair never reaches the network. That file is what
found the rounding limit described at the end of this README.

The `live` tests are excluded by default. They exist to re-check the assumptions
the offline fake is built on: that a closed day comes back dated to the previous
publication, that an unknown currency is a 404, and that an identical pair is a
422. If frankfurter.dev changes one of those, the live run is what says so
before a customer does.

Coverage is 99% of `app/`, reproducible with
`./test.sh --cov=app --cov-report=term-missing`. The six uncovered lines are the
uvicorn entrypoint and one branch commented as unreachable. Coverage is not a
target here; it is how the dead error code mentioned below was found.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FX_UPSTREAM_BASE` | `https://api.frankfurter.dev` | Rate source. `/v1/...` is appended. |
| `PORT` | `8080` | Listen port. |
| `FX_UPSTREAM_TIMEOUT_SECONDS` | `5` | Per-request upstream timeout. |
| `FX_CACHE_TTL_SECONDS` | `600` | How long a rate that can still change is reused. |
| `FX_CACHE_MAX_ENTRIES` | `4096` | Cache bound. |
| `FX_MAX_AMOUNT` | `1000000000000` | Largest accepted `amount`. |

All of it is read once, at startup. A value that cannot be used stops the
process there rather than turning into a strange answer under load.

## Errors

Every non-2xx response is the same shape:

```json
{ "error": "future_date", "message": "No rate exists for 2030-01-01; the ECB has published up to 2026-09-04." }
```

`error` is stable and safe to branch on. `message` is for a human and may be
reworded. There is no partially-successful response: a 200 means the number is
usable, and anything else means there is no number.

This table is the whole set, and a test proves it. `app/errors.py` holds the
codes and their statuses in one dictionary, an error with a code outside it
cannot be constructed, and `tests/test_error_catalogue.py` fails if the two
drift apart in either direction. Documenting an error the service cannot return
is the same kind of defect as returning one it does not document.

| `error` | Status | When |
|---|---|---|
| `missing_parameter` | 400 | `amount`, `from` or `to` was not sent. |
| `invalid_amount` | 400 | Not a number, negative, not finite, or above the ceiling. |
| `invalid_currency` | 400 | Not a three-letter code. |
| `invalid_date` | 400 | Not `YYYY-MM-DD`. |
| `invalid_request` | 400 | A parameter failed validation in a way none of the above names. |
| `unsupported_currency` | 400 | The ECB series does not carry the pair. |
| `future_date` | 400 | Later than today in Frankfurt. |
| `date_out_of_range` | 400 | Before 1999-01-04, where the series starts. |
| `not_found` | 404 | No endpoint at that path. |
| `method_not_allowed` | 405 | That method is not allowed on that path. |
| `not_representable` | 422 | The exact result cannot be carried as a JSON number. |
| `internal_error` | 500 | A bug, or a refusal with no better name. Never carries a number. |
| `upstream_unavailable` | 502 | The rate source refused or could not be reached. |
| `upstream_invalid_response` | 502 | It answered with something unusable. |
| `upstream_timeout` | 504 | It did not answer in time. |

## Edge cases, and what happens

**The ECB published nothing that day.** Weekends, TARGET holidays, and the
morning of any day before the 16:00 CET fixing. The most recent earlier rate is
returned, and `rate_date` says which day it is really from while `asked_date`
keeps the question. `rate_date == asked_date` is the caller's signal that the
rate was published on the day asked about; when they differ, the rate is
carried forward. Refusing outright would be honest but useless: Saturday is a
real question with a real answer, as long as nobody pretends the answer is
Saturday's.

**A date in the future.** Refused with `future_date`, before any upstream call.
"Today" is today in Frankfurt, not on the machine running the process, because
that is the calendar the ECB publishes on.

**A date before 1999-01-04.** Refused with `date_out_of_range`. Frankfurter
answers both of these with a bare 404, the same 404 it gives for a currency it
does not carry, so ruling the date out here is what lets the caller be told
which of the two actually went wrong.

**`from` equals `to`.** Answered without calling upstream: rate 1, result equal
to the amount, and `source` is `"identity"` rather than `"ECB via
frankfurter.dev"`. No rate was consulted, so no rate source is credited.
Sending the pair upstream would be worse than useless — frankfurter.dev returns
422 for it.

**A currency neither side carries.** `unsupported_currency`, 400. It is the
caller's request that is wrong, not the rate source.

**The upstream is down, slow, or answering nonsense.** `upstream_unavailable`,
`upstream_timeout`, `upstream_invalid_response`. A missing `rates` object, an
unparseable date, a non-numeric or non-positive rate, and a rate dated *after*
the day asked about are all treated as unusable rather than parsed optimistically.

**A bad amount.** Rejected: non-numeric, negative, `nan`, `inf`, or above
`FX_MAX_AMOUNT`. Zero is accepted and converts to zero. Negatives are rejected
rather than signed-through, because a negative amount reaching a conversion tool
is far more likely to be a bug upstream than a refund.

**The same question twice.** Answered from cache. Rates for a day that is over
never change and are kept without expiry; anything touching today expires after
`FX_CACHE_TTL_SECONDS`. The key includes the date, so a rate fetched for one day
can never be served for another. Concurrent identical requests share a single
upstream call rather than each making their own.

## Known limits

Money arithmetic is `Decimal` end to end, including the parse of the upstream
body, and becomes a float only in the last function before serialisation. JSON
has no decimal type, so `11988.40` goes over the wire as `11988.4`. The scale is
lost in transport, never in the arithmetic.

Above roughly ninety trillion a float can no longer hold a value to the cent.
`123456789012.34` at a rate of `987.6543` is exactly `121932628532230.35` and
serialises as `...30.34`. Rather than send a number that is a cent wrong, the
service checks that the value survives the round trip and refuses with
`not_representable` when it does not. Every conversion a real customer makes is
many orders of magnitude below that line.

`result` is rounded to two decimal places for every currency. That is right for
EUR and TRY and wrong for JPY, which has no minor unit. It is written down in
NOTES.md rather than half-fixed.

`tool.py` in the repository root is the subject of Part B. It is not part of the
service; see REVIEW.md.
