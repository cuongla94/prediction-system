# Decisions

Settled trade-offs. Recorded here so they are not re-discovered and re-flagged
as new findings by a later review — each one was weighed with evidence, and the
reasoning matters more than the verdict.

`scripts/run_audit.py` checks this file weekly and flags if these markers go
missing, because losing them is how re-litigation starts.

---

## TLS: RISK ACCEPTED — 2026-07-20

The dashboard is served over plain HTTP on port 80. The nginx basic-auth
password, the dashboard passcode, and the Flask session cookie all cross the
network in cleartext, and `SESSION_COOKIE_SECURE` stays `False` *because* of
this — setting it without TLS would stop the cookie being sent and break login
outright.

**Accepted** on the grounds that this is single-user access to a read-only
dashboard with no write path to real money, and Let's Encrypt would require
buying and pointing a domain the project does not have.

**Revisit if** the dashboard gains more users, is accessed from untrusted
networks, or ever gains a write path to real money.

## Login rate limiting: SKIPPED — 2026-07-20

No `limit_req` in nginx and no application-side throttle on the passcode form.

**Skipped** deliberately, not overlooked: nginx basic auth sits in front of the
passcode form, so an attacker must clear that first. Evidence that it is doing
real work — the nginx log shows opportunistic scanners probing `/.env`,
`/.env.prod`, `/.git/HEAD` and `/terraform.tfstate` daily, and **every one
receives a 401**. The only IP ever served a 200 is the operator's.

Distinct from session expiry, which *was* actioned: sessions expire after 5 days
of inactivity, enforced both by the cookie's `Expires` and independently by
Flask's `max_age` when unsealing.

## Trading-mechanics work: PAUSED — 2026-07-20

No changes to `paper_trading/`, `scripts/run_paper_trading.py`, position sizing,
exits, or entry logic until a revisit trigger fires.

Three independent tests agreed the model has no edge over Kalshi's own prices:

| Test | n | Model Brier | Market Brier |
| --- | --- | --- | --- |
| Live settled markets | 462 markets / 20 cities | 0.1224 | 0.0048 |
| Full backtest (day-ahead, held out) | 17,692 rows / 40 series | 0.1215 | 0.0010 |
| Same-day proof (time-matched prices) | 3 decision times | 0.1045 / 0.1034 / 0.0790 | 0.0861 / 0.0473 / 0.0212 |

Supporting: **0 wins from 171** markets where the model claimed >10 points of
edge.

Every trading mechanic operates *on top of* a signal. Sizing, exits and entry
filters change how much you win or lose given an edge; none can manufacture one.
The natural instinct — raise `min_edge` to be more selective — actively selects
for *larger* model error here.

**Revisit trigger** (check with `uv run scripts/calibration_trend.py`):

- **STRONG** — pooled skill vs market turns positive for any series, confirmed by
  `scripts/run_backtest.py` reporting `TRADEABLE`. The only condition that
  justifies resuming trading-mechanics work.
- **WEAK** — 4 consecutive improving weekly recalibrations while skill is still
  negative. Means "investigate the forecasting core", *not* "resume trading
  work".

Neither fired = the pause stands, no re-litigation needed.
