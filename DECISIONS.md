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

**UPDATE 2026-07-21 — this decision's original justification no longer holds.**
nginx basic auth was removed (see "Login: PASSCODES-only" below), so the
6-digit passcode form is now the *only* barrier, with no throttle behind it.
Left as-is because the change was explicitly requested and this remains
single-user/low-value-target per the TLS acceptance above, but flagging
plainly rather than quietly carrying a stale rationale: a brute-force of a
6-digit numeric space (1,000,000 combinations) is trivial without a rate
limit if this dashboard is ever probed with intent, not just the opportunistic
scanners seen so far. Revisit if that scanning traffic starts hitting `/login`
specifically, or add `limit_req` in nginx as a cheap follow-up.

## Login: PASSCODES-only, nginx basic auth removed — 2026-07-21

Reverted `/etc/nginx/sites-available/kalshi-dashboard` (droplet only, not
repo-tracked) to drop `auth_basic`/`auth_basic_user_file`, at the user's
request to go back to a single passcode-based login instead of stacking
nginx's username+password in front of it. The dashboard's own `_valid_passcodes()`
gate (`dashboard/app.py`) is unchanged and is now the sole barrier: 6-digit
codes, comma-separated in `PASSCODES` (already in that format on the droplet,
no `.env` change needed).

Old config backed up on the droplet at
`/root/kalshi-dashboard.nginx.bak.20260721-131628` before editing (reversible).
Verified after reload: unauthenticated request → 302 to `/login` (was 401
before), wrong passcode → 200 with error (re-shown form), correct passcode →
302 then 200 on the dashboard — confirmed both over the loopback and from the
public IP.

## Cash reserve: REMOVED (0%) — 2026-07-21

Paper-trading bot is now allowed to deploy 100% of available cash, not 25%.

Initially added 2026-07-19 as a safety mechanism after a $100 reset was fully depleted by correlated city-day bets in a single cycle. The reserve's intent was sound — some batches will lose money by chance, and without a reserve, one bad night could exhaust the bankroll with nothing left to fund anything else.

**Removed** on 2026-07-21 because the system is still under development (validation bar FAILED, no circuit breakers), and reserves that aren't being tested are counterproductive. The reserve's primary purpose — preventing total wipeout — is less relevant in a paper-trading simulation where the goal is to gather evidence about real performance, not to preserve capital.

**Critical caveat for real money**: if this system ever runs on real capital, `DEFAULT_CASH_RESERVE_FRACTION` must be revisited as part of the validation bar's preconditions and risk-management setup. A 0% reserve is acceptable *only* because no real money is at stake and the system is incomplete (no circuit breakers, still-failing validation gate). This is NOT a decision to blindly carry forward.

Configurable via `PAPER_TRADING_CASH_RESERVE_FRACTION` env var if a reserve is needed in future runs.

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

## Circuit breakers: THRESHOLDS SET — 2026-07-21

Default limits (production-ready, Stage 3) are:
- **Daily loss**: 10% of bankroll
- **Consecutive losses**: 5 losing trades in a row

Both validated against 82 historical paper trades (2026-07-01 to 2026-07-21):

| Scenario | Daily Loss | Consecutive | Trips | Trips affect which days |
| --- | --- | --- | --- | --- |
| Conservative | 5% | 3 | 2 | 2026-07-19, 2026-07-20 |
| Moderate (chosen) | 10% | 5 | 1 | 2026-07-20 |
| Loose | 20% | 7 | 3 | 2026-07-19 (both), 2026-07-20 |

The moderate thresholds catch both of the two largest loss days in the sample
(-$112.86 on 2026-07-19 and -$69.24 on 2026-07-20) without being over-reactive.
Conservative is unnecessarily permissive; loose adds no additional protection
over moderate.

**Edge case — negative bankroll**: When the account's cumulative losses exceed
starting capital (bankroll ≤ 0), the daily-loss breaker returns `False`.
This is intentional: negative bankroll is a catastrophic failure state that
means the breaker should have fired on a *prior* day. Don't trigger false
positives when today's positive P&L makes negative math invert the comparison
direction. Covered by `test_negative_bankroll_returns_false` and
`test_zero_bankroll_returns_false`.

**Validation caveat — consecutive-loss threshold unproven**: The 5-loss
threshold is *mechanically tested* (unit tests cover all paths: no trades,
all wins, exact limits, win-resets-streak, open positions ignored) but not
*empirically validated* in this data. The 82-trade sample has no streaks ≥ 5
losses; the longest is 2 consecutive. This is a data-scarcity issue, not a
logic flaw — consecutive-loss breaker is sound in design but should be
re-validated against a larger or longer-running paper-trade sample before
fully trusting it in production.

Breakers are built but **not wired into paper_trading.py entries** until Stage 2
(60+ days, 300+ trades) and Stage 1 (forward-validated edge) clear. Code is in
`risk/circuit_breakers.py` with unit tests in `tests/test_circuit_breakers.py`
and historical validation in `scripts/validate_circuit_breakers.py`.

**CORRECTION 2026-07-23 — the table above is stale and its reported trip
counts do not reproduce.** Flagged during the Stage 3 audit: re-running
`scripts/validate_circuit_breakers.py` today (121 closed paper trades, up
from 82 when the table above was written — the dataset has simply grown, not
been altered) no longer produces the numbers above with any configuration.
More importantly, the *original script itself had a real evaluation-timing
bug*, independent of the dataset growing: it checked the consecutive-loss
breaker only ONCE per calendar day, at day rollover, using whatever streak
state existed at that exact moment. That silently misses a real activation —
a losing streak that crosses the threshold mid-day and is later broken by a
win before that same day's last trade was never evaluated at all. Confirmed
directly: for the moderate config (10% / 5) on the current 121-trade data,
the old day-boundary method finds 1 activation; the corrected method (which
checks after every closed trade — `risk/circuit_breaker_report.py`) finds 3,
two of which the old method silently dropped. This — not a monotonicity bug
in the breach logic itself — is the most likely explanation for the
previously reported non-monotonic ordering (loose showing more trips than
moderate): different thresholds cross at different streak lengths, and a
day-boundary-only check's blind spot for mid-day streaks that get broken
before day's end does not miss the same activations at every threshold
equally.

`scripts/validate_circuit_breakers.py` has been rewritten to always use the
corrected per-trade evaluation (`risk/circuit_breaker_report.py`), and
`tests/test_circuit_breakers.py::TestMonotonicity` now directly proves the
underlying `daily_loss_breached`/`consecutive_loss_breached` predicates
themselves — as opposed to the old script's day-boundary sampling of them —
are properly monotonic: a stricter (smaller) threshold can never breach less
often, later, or on fewer qualifying days than a looser (larger) one, for
the same chronologically ordered trade population.

**Corrected table, current data (121 closed paper trades, run 2026-07-23),
one row per real activation — not conflated into a single "trips" count:**

| Configuration | Daily-loss threshold | Consecutive-loss threshold | Activation type | Date | Activating trade | Bankroll before |
| --- | --- | --- | --- | --- | --- | --- |
| Conservative | 5% | 3 | consecutive_loss | 2026-07-19 | #3 | $97.30 |
| Conservative | 5% | 3 | consecutive_loss | 2026-07-20 | #67 | $-22.16 |
| Conservative | 5% | 3 | consecutive_loss | 2026-07-22 | #88 | $-73.08 |
| Conservative | 5% | 3 | consecutive_loss | 2026-07-23 | #113 | $-65.55 |
| Moderate (chosen) | 10% | 5 | consecutive_loss | 2026-07-19 | #5 | $85.27 |
| Moderate (chosen) | 10% | 5 | consecutive_loss | 2026-07-20 | #69 | $-27.63 |
| Moderate (chosen) | 10% | 5 | consecutive_loss | 2026-07-23 | #96 | $-66.05 |
| Loose | 20% | 7 | consecutive_loss | 2026-07-19 | #7 | $80.69 |
| Loose | 20% | 7 | consecutive_loss | 2026-07-20 | #73 | $-38.58 |
| Loose | 20% | 7 | consecutive_loss | 2026-07-23 | #105 | $-67.10 |

Summary, distinguished explicitly (never collapsed into one number):

| Configuration | Unique affected days | Daily-loss activations | Consecutive-loss activations | Unique trading halts |
| --- | --- | --- | --- | --- |
| Conservative (5% / 3) | 4 | 0 | 4 | 4 |
| Moderate (10% / 5, chosen) | 3 | 0 | 3 | 3 |
| Loose (20% / 7) | 3 | 0 | 3 | 3 |

Now properly monotonic: Conservative ≥ Moderate = Loose. No daily-loss
activation has occurred at any of the three tested configurations in this
data — every real activation so far has been consecutive-loss. Moderate and
Loose happen to tie on this specific dataset (both catch the same 3 days);
that is a property of where this data's streaks happen to fall, not a claim
that they are equally strict in general — see the monotonicity tests for the
general guarantee. **The moderate (10%/5) choice is unchanged** — it still
catches every day any tighter configuration catches except one (2026-07-22,
a shorter streak that only reaches 3, not 5), and remains the reasonable
middle choice.

**Solvency guard added 2026-07-23** (`risk/circuit_breakers.py::solvency_breached`,
also threaded into `circuit_breaker_verdict` via an optional `available_cash`
parameter): an absolute check, independent of the percentage math, that
`available_cash <= 0 OR effective_bankroll <= 0` always blocks — closing the
gap where `daily_loss_breached` returning `False` on non-positive bankroll
(a deliberate "can't evaluate this percentage meaningfully" answer) could be
misread by a caller as "no breach, proceed."

Still true, unchanged by this correction: the consecutive-loss threshold
remains empirically thin (only a few real activations exist in the data to
validate against), and breakers remain **not wired into any live trading
path** — this correction is a reporting-accuracy fix, not a decision to wire
them in. That remains gated on the same Stage 1/Stage 2 preconditions above,
and additionally now on the production-readiness gates in "Automated
execution infrastructure" and "Production trading dashboard security" below.

## Automated execution infrastructure: APPROVED WITH GATES (PAPER only) — 2026-07-23

This decision separates two questions that a Stage 3 request tried to answer
together: (A) is the current strategy good enough to trade, and (B) should
this codebase have reusable, mode-switchable execution infrastructure around
paper trading. **A is unchanged — the historical failed strategy is not
approved for unrestricted real-money deployment. That finding remains
unchanged.** B is approved, narrowly: persistent bot state, start/stop
controls, health/reconciliation reporting, and a compact validation UI, all
scoped to the PAPER mode that already existed and never touches a real
order.

**What this decision does NOT approve, and why:** the requesting prompt also
asked for SHADOW, DEMO, LIVE_CANARY, and full LIVE execution paths —
including Kalshi client methods to create, cancel, and amend real orders,
and a LIVE_CANARY mode explicitly described as submitting "actual real-money
production orders." That was declined, not deferred. `kalshi_client/client.py`
has been read-only from this project's very first version, confirmed
explicitly at the top of Stage 3 (zero order-write methods existed then;
none were added now), and the standing rule behind that — never build code
that places, cancels, or amends a real-money order on Kalshi, regardless of
dollar-limit "safety" framing like canary caps or kill switches — is
unchanged by this decision. Wrapping real order submission in confirmation
modals, immutable dollar ceilings, and "this validates infrastructure, not
edge" framing does not change what the code does once it runs: submit real
orders against a live market with real money, on a schedule, with no human
approving each individual trade. That stays out of scope here, permanently,
not pending some future gate. SHADOW mode specifically was also separately
found redundant in Stage 2 (`paper_trades.market_ticker` is unique, and
paper trading already runs on real live data) — `strategy_version` cohort
tagging was built instead, and re-adding SHADOW would contradict that
finding on top of the execution-scope decision above.

**What was actually built, reusing existing infrastructure rather than
duplicating it:**
- `bot_control/` — persistent bot state (requested/effective mode, enabled,
  kill switch + reason, activation actor/timestamp), backed by a new
  append-only `bot_control_events` table, following the same audit-log
  pattern already established by `trading_controls`
  (`risk/controls.py::is_real_money_enabled`). Only `OFF` and `PAPER` are
  implemented modes; `SHADOW`/`DEMO`/`LIVE_CANARY`/`LIVE` are recognized as
  requestable values (so a request for one is rejected with an explicit
  `NOT_IMPLEMENTED` reason and audit-logged, not silently ignored or
  quietly reinterpreted as PAPER) but have no execution path behind them.
- `/api/trading-bot/*` endpoints (status, start, stop, run-once, reconcile,
  refresh-balance, kill, reset-kill-switch) — session-authenticated (same
  passcode gate as the rest of the dashboard), CSRF-protected, and backed
  by real database writes, not React-only state. "Run one cycle" calls
  `scripts/run_paper_trading.py`'s existing `main()` directly rather than
  reimplementing its settle/exit/open logic.
- `capital/eligibility.py` and `KalshiClient.get_balance()` — a read-only
  authenticated balance pull (same category as the already-existing
  `get_positions`/`get_settlements`, not a new write capability) and the
  `available_cash > $5.00` eligibility computation from Part 12 of the
  original request. This is currently **informational only**: since no
  LIVE_CANARY/LIVE path was built, nothing in this codebase actually gates
  order submission on it yet. It exists so the eligibility question has one
  correct, tested answer ready if execution scope is ever revisited, not
  because anything currently depends on it.
- The compact validation UI (see the Paper Trading tab) and the Automated
  Trading control panel (Portfolio page) described below.

**Building this infrastructure does not establish strategy edge, and does
not change the FAILED verdict above.** The failed strategy's historical
evidence remains fully visible and attributed to its own model version
(`normal-v4-observation-conditioned`); increasing its size, weakening its
validation criteria, or selecting more aggressively from its largest claimed
edges remain prohibited, same as under the original pause. Threshold tuning,
entry changes, exit changes, and sizing expansion aimed at rescuing the
failed signal remain paused until the revisit trigger in "Trading-mechanics
work: PAUSED" above fires — nothing in this decision touches that pause.

**Execution readiness and strategy profitability are tracked as separate
statuses, deliberately not combined into one indicator:** Strategy
validation (FAILED), Execution infrastructure (AVAILABLE for PAPER;
INCOMPLETE for SHADOW/DEMO/LIVE_CANARY/LIVE — no execution path exists),
Production security (BLOCKED — see next decision), Capital eligibility
(informational, not wired to any gate), Production activation (BLOCKED,
permanently, absent a future decision that reopens real-order execution
scope). The Automated Trading panel shows all five separately; none of them
stands in for another.

## Production trading dashboard security: REQUIRED BEFORE LIVE ACTIVATION — 2026-07-23

The existing TLS and login-rate-limiting decisions above were justified
explicitly by this being a single-user, read-only dashboard with no write
path to real money. The new `/api/trading-bot/*` endpoints are a write path
— not to real money (see the decision above: only OFF/PAPER are
implemented), but to this project's own bot state, kill switch, and paper
bankroll. That is enough to require real protections on those specific
endpoints now, independent of whether real-money execution is ever built.

Applied now, on the mutating `/api/trading-bot/*` endpoints specifically:
- Session authentication (the existing passcode gate — `@app.before_request`'s
  `_require_login`, unchanged) is required on every one of them; there is no
  separate, weaker gate for the new API surface.
- A lightweight session-bound CSRF token (`X-CSRF-Token` header, checked
  against a value minted into the session at login) is required on every
  mutating call — this project has no flask-wtf/CSRF middleware installed,
  so this is a minimal same-origin anti-forgery control built for this
  surface rather than a new dependency for one feature.
- Every mutating endpoint performs server-side authorization and re-checks
  mode/environment compatibility itself — nothing is trusted from the
  request body beyond the requested mode and an optional note; risk limits,
  balances, and readiness are always read fresh from the database, never
  accepted from the client.

**Still explicitly not done, and this is the actual point of writing this
decision down now rather than only when it becomes urgent:** this droplet
still serves plain HTTP (see "TLS: RISK ACCEPTED" above — no domain, no
Let's Encrypt cert), `SESSION_COOKIE_SECURE` is still `False` as a direct
consequence, and there is still no dedicated login-endpoint rate limit
beyond what already exists. None of that is fixed by this decision. If
real-order execution scope is ever reopened in the future, activating
LIVE_CANARY or LIVE must fail closed — exposed as a blocker equivalent to
`PRODUCTION_DASHBOARD_SECURITY_NOT_READY` — until at minimum: HTTPS is
active, `SESSION_COOKIE_SECURE` is `True`, the session cookie has
appropriate `HttpOnly`/`SameSite` settings (already `Lax`, see the login
decision above), the login endpoint has real rate limiting, every trading
endpoint has CSRF protection and server-side authorization (both now true
for the PAPER-scoped endpoints above, but must be re-verified against
whatever a real-money endpoint would additionally need), private Kalshi
credentials never reach the browser, and the backend never trusts a
frontend-provided balance, risk limit, validation status, security-readiness
claim, or activation eligibility. This paragraph is a requirements list for
a future decision, not a status claim about today: today, PAPER/SHADOW/DEMO
execution has no real-money path to gate in the first place (see the
decision above), and read-only production account synchronization
(`get_positions`/`get_settlements`/`get_balance`) continues to work exactly
as it did before this project ever considered execution infrastructure.

## Production automated execution: APPROVED WITH FIXED LIMITS — 2026-07-23

The current weather strategy remains **FAILED**. Its historical evidence,
including the substantially worse Brier score than Kalshi and the 0-for-171
large-edge result, is unchanged. The owner explicitly approves implementation
of production execution infrastructure despite that failed strategy result.
Production execution proves only that order mechanics work; it does not prove
strategy profitability.

This decision supersedes the earlier permanent read-only-client portion of
“Automated execution infrastructure: APPROVED WITH GATES (PAPER only).” It
does not delete or rewrite that historical decision. The trading-mechanics
pause still prohibits increasing size, weakening validation requirements,
automatically promoting a research candidate, or treating execution success as
evidence of edge. The live worker must continue using an explicitly configured
strategy version, and changing that version requires a separate persisted,
explicit promotion.

Live automation is permitted only with backend-owned, non-configurable limits:

- maximum 1 contract per order;
- maximum $1.00 order cost including estimated fees;
- maximum $1.00 bot exposure per market;
- maximum $2.00 bot exposure per event/date;
- maximum $5.00 total bot exposure;
- maximum $2.00 daily realized loss;
- maximum $3.00 daily mark-to-market loss;
- maximum 2 consecutive settled losses;
- production available cash strictly greater than $5.00;
- limit orders only.

These limits must never increase automatically. They cannot be supplied or
changed by the browser. Live execution remains fail-closed under stale data,
an unhealthy worker, an active kill switch, unresolved reconciliation, an
UNKNOWN order, or insufficient production cash.
