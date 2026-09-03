# Notes

## Decisions

**A day the ECB did not publish on.** The endpoint answers, and says so. It
returns the most recent earlier rate, puts the real publication date in
`rate_date`, and keeps the question in `asked_date`. When the two differ, the
rate was carried forward. I considered refusing weekends outright, which is
honest but not useful — "what was 250 EUR worth on Saturday" is a real question
with a real answer, and the only thing that must not happen is calling the
answer Saturday's. So the rule I actually implemented is narrower than
"handle weekends": the service never reports a rate under a date the ECB did
not publish it for. Everything else follows from that. It is why `asked_date`
exists, why a rate dated *after* the request is treated as a broken upstream
response, and why "today" means today in Frankfurt rather than on whichever
machine is running the process.

**Future and pre-1999 dates are refused locally,** before any network call.
Not for speed — Frankfurter answers both with a bare 404, and that is the same
404 it returns for a currency it does not carry. Deciding here is what lets the
caller be told whether the date or the pair was the problem.

**`from` equal to `to` never leaves the process,** and reports `"source":
"identity"` instead of crediting the ECB. This is the one place I knowingly
deviate from the example body in the brief. Writing "ECB via frankfurter.dev"
next to a rate of 1 that no rate source produced seemed like exactly the kind of
small lie the rest of the task is about avoiding. Frankfurter returns 422 for
the pair anyway.

**Decimal, not float, from the upstream parse to the last function before
serialisation.** `response.json(parse_float=Decimal)` keeps the published
precision; the rate is never rounded, only the result is, to two places, half
up. The two-place rounding is currency-blind and therefore wrong for JPY. I left
it wrong and wrote it down rather than half-fixing it.

**Errors are never success-shaped.** One body, `{"error", "message"}`, under a
real status code, including for FastAPI's own validation failures — a caller
should not have to parse two formats depending on how far into the handler the
request got. Nothing that fails produces a number.

**A number that will not survive JSON is refused rather than shortened.** The
example tests all pass with the rate never rounded and the arithmetic exact, so
I wrote three property tests to state the rule instead of the cases: a 200
always carries a result equal to the amount times the rate, quantised to the
cent. Widening the generated amounts to the configured ceiling broke it. A JSON
float holds about fifteen significant digits, and 123456789012.34 at a rate of
987.6543 is exactly 121932628532230.35, which serialises as `...30.34`. One
cent, in my own code, in the one thing this service exists not to do. Rather
than lower the ceiling and hope, the last function before serialisation now
checks that the value round-trips and returns `not_representable` when it does
not. No real conversion comes close to that line, which is exactly why it would
have shipped.

**The cache key is `(from, to, date)`,** never just the pair. A rate for a
closed day is final and is kept without expiry; anything touching today expires
after ten minutes, because the 16:00 CET fixing may not have happened yet.
Concurrent identical calls share one upstream request.

## With another day

- Validate currency codes against `/v1/currencies` (cached for a day, and
  skipped rather than fatal if that call fails). Today an unsupported code and
  a genuinely broken upstream both arrive as an upstream 4xx and I infer which
  from context, which will eventually be wrong.
- Per-currency minor units, so JPY and KRW do not get two decimal places.
- One retry with jitter on a timeout or a 5xx, and a circuit breaker so a
  frankfurter.dev outage costs one slow request rather than one per caller.
- Structured request logging with a correlation id, and a counter per error
  code — the codes exist mostly so that this is possible later.
- Serve `rate` and `result` as strings as well, under different keys. JSON
  numbers cannot carry `11988.40`; the trailing zero is lost in transport even
  though the arithmetic never loses it. That would also retire
  `not_representable` rather than merely making it honest.

## AI tools

Claude Code, the way I normally work: I wrote the shape of the module and the
decisions above, had it produce the mechanical parts — the cache, the error
factories, the parametrised test bodies — and reviewed every line. I ran the
tests myself rather than believing the summary of them.

The part I did not delegate was the ranking in REVIEW.md. Finding the seven
defects in `tool.py` is mostly reading; deciding that the silent 200 is the one
to fix tonight, even though the parameter-name bug produces a more plausible
wrong number, is a judgement about which failure the team can still see
tomorrow. That is the part worth being asked about in person, so I made it
myself.

## One thing the AI got wrong

Writing the future-date guard, I asserted in a code comment that Frankfurter
answers a future date with the latest rate it holds, which would have made the
guard the only thing standing between a customer and a forecast presented as a
fact. It reads well and I had no reason to doubt it. I checked anyway, because
the guard's error message depended on it, and hitting `/v1/2030-01-01` returns
`404 {"message": "not found"}` instead.

The guard survived, the reasoning did not. A 404 is also what an unsupported
currency returns, so without the local check a future date is reported to the
caller as `unsupported_currency` — the right refusal for the wrong reason,
which is worse than it sounds when an agent is deciding whether to retry with a
different currency. I rewrote the comment, and turned the probe into
`tests/test_live.py`, which is deselected from `./test.sh` and asserts the
upstream behaviours the offline fake is built on. The next time one of those
assumptions is wrong it will be a red test rather than a comment nobody
re-read.
