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
Python 3.11 or newer. Nothing else is needed — no database, no keys, no Docker.
On Windows both scripts run under Git Bash; PowerShell cannot execute them.

Interactive schema at `/docs`, machine-readable at `/openapi.json`. `/health`
is a liveness probe and deliberately says nothing about the upstream: if
frankfurter.dev is down, this process is still healthy and should not be
restarted for it.

## Testing

```bash
./test.sh                # 60 tests, no network touched
./test.sh -m live        # 5 extra tests against the real frankfurter.dev
```

The default run replaces the HTTP transport with a fake, so the service under
test is the real service and only the socket is simulated. The `live` tests are
excluded by default and exist to re-check the assumptions the fake is built on:
that a closed day comes back dated to the previous publication, that an unknown
currency is a 404, and that an identical pair is a 422. If frankfurter.dev ever
changes one of those, the live run is what tells you before a customer does.

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
{ "error": "future_date", "message": "No rate exists for 2030-01-01; the ECB has published up to 2026-09-03." }
```

`error` is stable and safe to branch on. `message` is for a human and may be
reworded. There is no partially-successful response: a 200 means the number is
usable, and anything else means there is no number.

| `error` | Status | When |
|---|---|---|
| `missing_parameter` | 400 | `amount`, `from` or `to` was not sent. |
| `invalid_amount` | 400 | Not a number, negative, not finite, or above the ceiling. |
| `invalid_currency` | 400 | Not a three-letter code. |
| `unsupported_currency` | 400 | The ECB series does not carry the pair. |
| `invalid_date` | 400 | Not `YYYY-MM-DD`. |
| `future_date` | 400 | Later than today in Frankfurt. |
| `date_out_of_range` | 400 | Before 1999-01-04, where the series starts. |
| `no_rate_available` | 404 | Nothing published on or before the asked date. |
| `upstream_unavailable` | 502 | The rate source refused or could not be reached. |
| `upstream_invalid_response` | 502 | It answered with something unusable. |
| `upstream_timeout` | 504 | It did not answer in time. |
| `internal_error` | 500 | A bug. Never carries a number. |

## Edge cases, and what happens

**The ECB published nothing that day.** Weekends, TARGET holidays, and the
morning of any day before the 16:00 CET fixing. The most recent earlier rate is
returned, and `rate_date` says which day it is really from while `asked_date`
keeps the question. `rate_date == asked_date` is the caller's signal that the
rate was published on the day asked about; when they differ, the rate is
carried forward. Refusing outright would be honest but useless — Saturday is a
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

**The same question twice.** Answered from cache. Rates for a closed day never
change and are kept without expiry; anything touching today expires after
`FX_CACHE_TTL_SECONDS`. The key includes the date, so a rate fetched for one
day can never be served for another. Concurrent identical requests share a
single upstream call rather than each making their own.

## Layout

```
app/config.py    environment, read once at startup
app/errors.py    the one error shape, and the codes
app/fx.py        validation, the upstream call, the cache
app/main.py      HTTP only: parse in, one shape out
tests/           60 offline tests, 5 live ones
tool.py          the Part B subject. Not part of the service; see REVIEW.md.
```

Everything that decides whether an answer is trustworthy is in `app/fx.py` and
is testable without an HTTP server.

## Two things worth knowing

Money arithmetic is `Decimal` end to end, including the parse of the upstream
body, and becomes a float only in the last function before serialisation. JSON
has no decimal type, so `11988.40` goes over the wire as `11988.4`; the scale is
lost in transport but never in the arithmetic.

`result` is rounded to two decimal places for every currency. That is right for
EUR and TRY and wrong for JPY, which has no minor unit. It is recorded in
NOTES.md rather than fixed.
