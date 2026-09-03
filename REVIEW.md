# Review of tool.py

Every claim below was reproduced by importing `tool.py`, replacing
`tool.client` with an `httpx.MockTransport` that answers the way
api.frankfurter.dev actually answers, and calling the endpoint. The upstream
behaviour the fake reproduces was checked against the live service first: a
closed day returns the previous publication date, an unknown currency returns
404, and an identical pair returns 422.

## 1. Every failure is a 200 with a rate of zero

`convert` catches everything and returns a success-shaped body with
`rate: 0.0, result: 0.0`. Upstream down, currency not carried, identical pair,
malformed body — all the same answer. An agent has nothing to branch on, so it
tells the customer their 250 EUR is worth 0.00 TRY, in the same confident tone
it uses when it is right. There is no signal in the status code, no signal in
the body, and `print` puts the real cause somewhere nobody is reading.

*Verify:* point the client at a transport returning 503 and call the endpoint.
Observed: `200 {"rate": 0.0, "result": 0.0, "source": "ECB via frankfurter.dev"}`.
Same output for `to=ZWL` and for `from=EUR&to=EUR`.

## 2. The documented query parameters do not reach the handler

The signature declares `from_` and `on`, so the endpoint's actual parameters are
`?from_=` and `?on=`. A call written exactly as the brief documents it —
`?amount=250&from=USD&to=TRY&date=2026-08-28` — silently falls back to the
defaults `EUR`/`TRY` and to today's rate. The customer asked about dollars on a
date in August and got euros at this afternoon's fixing, correctly computed and
completely unrelated to the question.

*Verify:* `?amount=250&from=USD&to=TRY`. Observed: the upstream call is
`base=EUR&symbols=TRY`, and the response echoes `"from": "EUR"` — the tool
tells you which currency it used, but only after it has already used it.

## 3. The cache is keyed without the date, and a cache hit invents the rate date

The key is `f"{base}-{target}"`. The first answer for a pair becomes the answer
for that pair on every date, for the life of the process — no expiry, no bound.
Worse, the cached branch returns `str(on or date.today())` rather than the date
the rate came from, so the stale number arrives wearing today's date.

*Verify:* call with `on=2020-01-02`, then call with no date. Observed: one
upstream request, and the second response carries the 2020 rate of 6.6 with
`"rate_date": "2026-09-03"`.

## 4. The rate is rounded to two decimals before it is used

`rate = round(rate, 2)` runs before the multiplication. Today's EUR/TRY of
55.9145 becomes 55.91, which is 1.13 TRY off on 250 EUR and scales linearly
with the amount. The damage is proportional to how small the rate is: a pair
quoted at 0.0234 becomes 0.02, a 15% error on every conversion.

*Verify:* stub the rate at 0.0234 and convert 10000. Expected 234.00, observed
200.00.

## 5. Dates are neither validated nor reported honestly

`payload["date"]` is the one field that says what a rate actually is, and
`fetch_rate` reads the response without ever looking at it, returning the date
that was asked for instead. A Saturday request gets Friday's rate labelled
Saturday. There is no `asked_date` in the response, so the caller cannot tell
the difference either. Nothing rejects a date in the future or before the ECB
series starts in 1999; both go upstream, come back 404, and land in finding 1.

The weekend comment describes something the code does not do. On a weekend
Frankfurter already returns the previous publication day with a 200, so the
fallback branch is never taken. What actually reaches it is an unknown currency
or an identical pair — where it re-queries `/latest` and fails a second time.

*Verify:* `on=2025-08-30`, a Saturday. Upstream returns `"date": "2025-08-29"`;
the tool reports `rate_date` as 2025-08-30.

## 6. Amounts are not validated, and errors come in two shapes

`amount: float` accepts `nan` and `inf`. Observed for `amount=nan`: a 200 whose
body is `{"amount": null, ..., "rate": 55.91, "result": null}` — a real-looking
rate beside a null result. `amount=abc` is rejected, but by FastAPI, as
`422 {"detail": [...]}`. So a caller has to parse three different bodies —
`{detail}`, the success shape, and the zero-filled success shape — to work out
whether it has a number.

*Verify:* `?amount=nan` and `?amount=abc`, both reproduced above.

## 7. Not customer-facing today, but it is what makes the rest hard to fix

`UPSTREAM` is hardcoded, so `FX_UPSTREAM_BASE` and `PORT` do nothing and the
tests the fixes need cannot point anywhere but production. `client` is built at
import time and never closed, which binds it to whichever event loop existed at
import and leaks connections on shutdown. `_cache` is module state, so tests
leak into each other unless someone remembers to clear it.

## The one I would fix before shipping tonight

Finding 1. Not because it is the largest error — finding 2 produces a
plausible wrong number, which is worse — but because it is the one change that
makes the others visible. Right now an outage, a typo in a currency code, and a
schema change upstream all look identical to the agent and to us. Re-raise
instead of swallowing, return `{"error", "message"}` under a non-2xx status,
and every remaining bug in this file stops being silent. It is a small change,
it needs no new dependency, and the rest can be fixed on Monday with the logs
it produces over the weekend.

## Things that look suspicious but are fine

- **No timeout on the client.** `httpx.AsyncClient()` already defaults to 5
  seconds on connect, read, write and pool; `httpx.AsyncClient().timeout` prints
  `Timeout(timeout=5.0)`. Worth making explicit so it is a decision rather than
  a default, but nothing hangs today.
- **One module-level client shared by every request.** That is the correct
  shape — it is what keeps the connection pool alive. The lifecycle is the
  problem, not the sharing.
- **`except Exception` at the request boundary.** A reasonable place for a
  catch-all. What it returns is the defect; catching is not.
- **`round(result, 2)` on the final amount.** Right for EUR and TRY, which both
  have two minor units. Only the rounding of the rate does damage.
- **An in-process dict as the cache.** Fine for a single-process tool. The key
  and the missing expiry are the bugs, not the choice of store.
- **`/health` answering `{"ok": true}` without checking upstream.** Correct for
  a liveness probe. Wiring liveness to a third party turns their outage into
  our restart loop.
