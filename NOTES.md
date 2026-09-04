# Notes

## Decisions

**A day the ECB did not publish on.** The endpoint answers and says so: the most
recent earlier rate, the real publication date in `rate_date`, the question kept
in `asked_date`. Refusing weekends outright is honest but useless — Saturday is
a real question with a real answer, and the only thing that must not happen is
calling the answer Saturday's. So the rule is narrower than "handle weekends":
**the service never reports a rate under a date the ECB did not publish it for.**
Everything else follows. It is why `asked_date` exists, why a rate dated *after*
the request is treated as a broken upstream response, and why "today" means
today in Frankfurt.

**Future and pre-1999 dates are refused locally,** before any call. Frankfurter
answers both with a bare 404 — the same 404 it gives for a currency it does not
carry — so deciding here is what lets the caller be told which went wrong.
`from` equal to `to` also never leaves the process, and reports `"source":
"identity"` rather than crediting the ECB for a rate of 1 it never produced.
That is the one place I knowingly deviate from the example body in the brief.

**Errors are never success-shaped.** One body, `{"error", "message"}`, under a
real status code, including for FastAPI's own validation failures. Nothing that
fails produces a number.

**A number that will not survive JSON is refused, not shortened.** The example
tests all passed, so I wrote three property tests to state the rule rather than
the cases: a 200 always carries a result equal to the amount times the rate,
quantised to the cent. Widening the generated amounts to the configured ceiling
broke it. A float holds about fifteen significant digits, and 123456789012.34 at
987.6543 is exactly 121932628532230.35, which serialises as `...30.34`. One
cent, in my own code, in the one thing this service exists not to do. The last
function before serialisation now checks the round trip and returns
`not_representable` instead. No real conversion comes near that line, which is
exactly why it would have shipped.

**Three modules below the HTTP layer, not one.** `fx.py` holds the rules,
`upstream.py` holds everything that knows frankfurter.dev exists, `cache.py`
holds "do not ask that again" and nothing about rates. I did not start there.
The split came after the tests began reaching into `service._client` to install
a fake and `service._cache` to assert a bound — a test reaching past a public
surface is the object telling you it has more than one job. `fx.py` went from
163 statements to 80, needs no server, no socket and no clock, and is now the
only file you have to read to know what the service will and will not claim.

**A documented error the service cannot return is a defect too.** Coverage found
`no_rate_available` in the README, and raised nowhere. That is the same fault
REVIEW.md charges `tool.py` with. Codes and statuses now live in one dictionary,
`FxError` refuses a code outside it, and a test compares that dictionary to the
README table in both directions.

## With another day

- Validate currencies against `/v1/currencies`, cached. Today an unsupported
  code and a broken upstream both arrive as a 4xx and I infer which.
- Per-currency minor units, so JPY does not get two decimal places.
- A retry with jitter and a circuit breaker, so an outage costs one slow
  request rather than one per caller.
- Request logging with a correlation id and a counter per error code.
- Serve `rate` and `result` as strings too. That would retire
  `not_representable` rather than merely make it honest.

## AI tools

Claude Code, the way I normally work: I set the shape of the modules and the
decisions above, had it write the mechanical parts, and read every line. I ran
the tests myself rather than believing a summary of them, and both findings
above came out of tools I chose to point at my own work.

What I did not delegate is the ranking in REVIEW.md. Finding the defects in
`tool.py` is mostly reading. Deciding that the silent 200 is the one to fix
tonight, even though the parameter-name bug produces a more plausible wrong
number, is a judgement about which failure the team can still see tomorrow.

## One thing the AI got wrong

Writing the future-date guard, I asserted in a comment that Frankfurter answers
a future date with the latest rate it holds — which would have made that guard
the only thing between a customer and a forecast presented as a fact. It read
well and I had no reason to doubt it. I checked anyway, because the error
message depended on it. `/v1/2030-01-01` returns `404 {"message": "not found"}`.

The guard survived; the reasoning did not. A 404 is also what an unsupported
currency returns, so without the local check a future date reaches the caller as
`unsupported_currency` — the right refusal for the wrong reason, which matters
when an agent is deciding whether to retry with another currency. I rewrote the
comment and turned the probe into `tests/test_live.py`. Next time one of those
assumptions is wrong it will be a red test, not a comment nobody re-read.
