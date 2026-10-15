# GoldSentinel

GoldSentinel is a local, safety-first XAU/USD analysis and MetaTrader 5 demo-trading platform. It ships with a FastAPI backend, React/TypeScript terminal, SQLite candle/audit/lifecycle storage, an authentic MT5 adapter, a deterministic mock broker, completed-candle multi-timeframe intelligence, adaptive risk sizing, provider-backed news integration, position management, performance reporting, and replay backtesting.

> Trading is risky. This software provides engineering controls, not profit guarantees or financial advice. Full Auto starts **disarmed after every process restart**, and the MT5 adapter rejects non-demo accounts.

## Quick start on Windows 11

Prerequisites: Python 3.11+, Node.js 20+, MetaTrader 5 (only for demo connectivity), and PowerShell.

```powershell
Copy-Item .env.example .env
.\scripts\setup.ps1
```

Set a strong `GS_API_TOKEN` in `.env`. Start two VS Code terminals:

```powershell
.\scripts\start-backend.ps1
```

```powershell
.\scripts\start-frontend.ps1
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The UI uses `change-me` only as the local development default; in the browser console set your configured token with `localStorage.setItem('gs-token', 'your-token')`, then reload.

## Broker modes

`GS_BROKER=mock` is the safe default and powers development/tests. It generates deterministic data and never contacts a broker.

For an already installed and logged-in MetaTrader 5 Windows terminal:

1. Select a **JustMarkets demo account** in the terminal.
2. Set `GS_BROKER=mt5` and `GS_SYMBOL=XAUUSD.m` in `.env`.
3. Start the backend and inspect `/api/status`.
4. Verify `account.is_demo=true`, live bid/ask, symbol volume limits, and all nine timeframes before arming.

The adapter verifies demo status again immediately before submission. FOK/IOC/RETURN are selected only from the symbol's broker-supported filling modes. Order states are reconciled against broker positions; uncertain submissions are quarantined by idempotency key.

## Calendar and headline providers

GoldSentinel supports a credential-free Forex Factory-compatible weekly calendar (`forex_factory`), native Financial Modeling Prep (`fmp`), and the original `generic_json` adapter. Economic-calendar availability and financial headlines are tracked independently, so a headline outage cannot hide the calendar state and a calendar failure cannot suppress useful headlines. No commercial provider or credential is silently selected.

### Credential-free weekly calendar

The example configuration uses the public weekly JSON feed:

```dotenv
GS_CALENDAR_PROVIDER=forex_factory
GS_CALENDAR_URL=https://nfs.faireconomy.media/ff_calendar_thisweek.json
GS_CALENDAR_API_KEY=
GS_CALENDAR_COUNTRY=US
GS_NEWS_FAIL_SAFE=true
GS_NEWS_REFRESH_SECONDS=300
```

This adapter accepts the feed's `title`, `country`, `date`, `impact`, `forecast`, `actual`, and `previous` fields, then keeps US/USD macro releases relevant to gold. Its offset-free timestamps are interpreted in the feed's America/New_York timezone and normalized to UTC, including daylight-saving transitions. A successful empty list is `VERIFIED_EMPTY`; malformed data, stale cache, HTTP failures, and permission responses are unavailable. The URL is configurable because the public feed is an external dependency whose availability is not controlled by GoldSentinel.

### Financial Modeling Prep

Create an FMP API key and use this exact configuration:

```dotenv
GS_CALENDAR_PROVIDER=fmp
GS_CALENDAR_URL=https://financialmodelingprep.com/stable/economic-calendar
GS_CALENDAR_API_KEY=your_personal_fmp_api_key
GS_CALENDAR_COUNTRY=US
GS_CALENDAR_DAYS_BACK=1
GS_CALENDAR_DAYS_AHEAD=14
GS_NEWS_FAIL_SAFE=true
GS_NEWS_REFRESH_SECONDS=300
```

`GS_CALENDAR_URL` may be left empty when using `fmp`; GoldSentinel then uses the official stable endpoint shown above. The API key is sent only as FMP's documented `apikey` query parameter. It is never sent as a Bearer token or included in status/error output. The adapter sends `country=US` plus inclusive `from` and `to` dates. `GS_CALENDAR_DAYS_BACK + GS_CALENDAR_DAYS_AHEAD` must be at most 89, keeping the inclusive range within FMP's 90-day maximum.

FMP's offset-free `date` values are interpreted as UTC. The adapter validates the documented array response, maps `event`, `date`, `impact`, `estimate`, `actual`, `previous`, `currency`, `country`, and `unit`, filters to US/USD releases relevant to gold, and calculates actual-versus-estimate surprise when both values are numeric. HTTP 401/403, HTTP 402 (subscription/payment required), permission messages, malformed objects, invalid timestamps/impact values, stale data, outages, and HTTP 429 are unavailable states. A successful empty array is reported separately as `VERIFIED_EMPTY` and does not masquerade as an outage.

Requests are cached for `GS_NEWS_REFRESH_SECONDS`; concurrent refreshes are serialized. A 429 `Retry-After` value is honored, so dashboard polling does not consume FMP quota. Access to the economic-calendar endpoint may depend on the FMP subscription attached to your personal key.

### Generic JSON

Set `GS_CALENDAR_PROVIDER=generic_json`, `GS_CALENDAR_URL`, and optionally `GS_CALENDAR_API_KEY`. For this adapter only, the key is sent as `Authorization: Bearer <key>`.

The endpoint must return either a JSON array or `{ "events": [...] }`. Each relevant event needs:

- `title` or `event`
- `timestamp` or `date` as ISO-8601, preferably with `Z` or an explicit offset
- `impact` (`HIGH`, `MEDIUM`, or `LOW`)

Optional fields are `id`, `currency`, `actual`, `forecast`, and `previous`. GoldSentinel retains the endpoint URL as the source and only imports gold-relevant macro events such as CPI, PPI, NFP/payrolls, PCE, Fed/FOMC, GDP, unemployment, retail sales, and ISM.

### Independent financial headlines

Headlines never substitute for the economic calendar and do not clear the news fail-safe. Configure them independently with either `rss` or `generic_json`:

```dotenv
GS_NEWS_PROVIDER=rss
GS_NEWS_URL=https://news.google.com/rss/search?q=gold%20OR%20XAUUSD%20OR%20Federal%20Reserve%20OR%20US%20inflation&hl=en-US&gl=US&ceid=US:en
GS_NEWS_API_KEY=
GS_HEADLINE_REFRESH_SECONDS=300
```

The RSS adapter reads standard RSS/Atom items and filters for gold, USD, inflation, labor, central-bank, and geopolitical context. The generic adapter accepts `{ "articles": [...] }` or a JSON array and may use a Bearer key. Calendar and headline retry timers are separate, minimizing requests and allowing either source to recover independently.

No feed credential is bundled because provider credentials and licensing belong to the operator. The backend refreshes configured providers every `GS_NEWS_REFRESH_SECONDS` (default 300). A configured feed is unavailable until its first successful refresh and becomes stale after three missed refresh intervals. With `GS_NEWS_FAIL_SAFE=true` and the Conservative policy, unavailable or stale calendar data is **not** treated as a clear market: entries and arming are blocked and the dashboard displays the provider state, next high-impact USD event, countdown, release values, surprise, and active restriction.

## Trading readiness and execution blockers

The dashboard's Trading Readiness panel is generated by the backend. It independently reports MT5/demo connectivity and source, M15 market-data validity, news availability, combined exposure/position limits, FULL_AUTO mode, pause state, emergency stop, and explicit arming state. Quote health is shown separately as `LIVE`, `QUIET`, `STALE`, or `DISCONNECTED`; stale or missing broker quotes are never replaced with mock prices in MT5 mode.

- `Resume` means persisted `paused=true`; resuming never rearms the bot.
- Selecting another mode, changing settings, pausing, resetting an emergency stop, or restarting always disarms automatic execution.
- `NO_TRADE / MARKET` means the current structure, zone quality, or reward-to-risk rules found no eligible setup.
- `NO_TRADE / OPERATIONAL` means data or news availability prevented evaluation.
- `NO_TRADE / RISK` means a risk, exposure, or broker-volume limit rejected the setup.
- A valid BUY/SELL can still show `BLOCKED_OPERATIONAL` when the signal exists but mode, pause, news, emergency, or arming state prevents submission.

`GET /api/market-diagnostics` performs a read-only nine-timeframe validation. Normal XAU/USD daily maintenance and weekend closures are recorded as expected gaps rather than feed failures; recent unexpected gaps remain blocking, while old broker-history gaps remain visible as non-blocking warnings.

The live strategy builds a weighted H4/H1/M15/M5/M1 context from completed candles, with M1 deliberately unable to create a setup by itself. It recognizes zone reversals, between-zone trend continuation, pullbacks, and breakout/retests; detects swings, BOS/CHoCH, liquidity sweeps, momentum/engulfing/pin/inside bars, volume and volatility changes; and applies profile-specific evidence thresholds. Immediately before submission, execution fetches a new quote, normalizes SL/TP to the broker tick size, validates stops and freeze levels from the correct trigger side (Bid for BUY protection, Ask for SELL protection), uses MT5 `order_calc_profit` for loss-at-stop sizing when available, and rechecks exposure, circuit breakers, slippage, spread, and reward-to-risk.

Setup fingerprints and lifecycle states (`DETECTED`, `QUALIFIED`, `SUBMITTING`, `FILLED`, `REJECTED`, `UNCERTAIN`, `EXPIRED`) are persisted so a restart or repeated scan cannot duplicate a fill. Uncertain sends remain quarantined until reconciled against an owned broker position. Automated management only runs for `magic=260115` or `GS:`-commented positions while the account is demo, mode is FULL_AUTO, the bot is explicitly armed, and pause/emergency controls are clear. It can move stops to breakeven, trail while locking profit, or close on decisive M5/M15 invalidation; it never widens a stop.

Historical candle storage is incremental and deduplicated by symbol/timeframe/timestamp. `POST /api/history/refresh` (authenticated) imports configurable M1/M5/M15/H1/H4 ranges in batches. Defaults are 14/120/180/540/1080 days and can be changed with `GS_HISTORY_<TF>_DAYS`; `GS_HISTORY_BATCH_SIZE` controls request size. Startup refresh is off by default; set `GS_HISTORY_REFRESH_ON_STARTUP=true` to run one background refresh when the service starts.

The dashboard uses `GET /api/quote` for lightweight quotes and `GET /api/market/{timeframe}/live` for the current candle. Full chart history is loaded only on initial view, timeframe changes, or manual reload. The chart instance is retained across updates, preserving zoom and scroll; the reset-view button is the explicit way to fit all data again.

## Architecture

- `backend/goldsentinel/brokers`: mock and native Windows MT5 brokers
- `market.py`, `zones.py`, `candle_analysis.py`, `market_context.py`, `setups.py`: feed quality, completed-candle evidence, weighted context, zones, and setup detection
- `news.py`, `strategy.py`, `risk.py`: external news context, explainable decisions, adaptive sizing
- `execution.py`, `position_manager.py`: persistent idempotent order state and owned-position management
- `performance.py`, `backtest.py`: all-history analytics and conservative bar replay/walk-forward checks
- `database.py`: SQLite settings, audit records, trades and equity snapshots
- `api.py`: authenticated FastAPI control plane bound to localhost by the startup script
- `frontend`: React terminal using TradingView Lightweight Charts and Recharts

## API safety

Read-only status/market/performance endpoints are local and unauthenticated. Trading, settings, analysis, news refresh, and backtest mutations require `X-API-Token` or `Authorization: Bearer …`. Backend Pydantic models enforce slider limits independently of the frontend.

Emergency stop disarms auto execution immediately, blocks new orders, attempts safe cancellation of broker-reported pending orders, and leaves existing positions and protective stops intact. Resetting it does not rearm the bot.

## Tests and verification

```powershell
.\.venv\Scripts\python.exe -m pytest
cd frontend
npm run typecheck
npm run build
```

Automated tests always use mock execution. They cover nine timeframes, candle validation, incomplete-candle handling, multi-zone selection, executable-price and slippage checks, risk caps, risk profiles, wider-stop sizing, minimum-volume rejection, total exposure, idempotent submission, stop widening, calendar/headline response handling, replay/backtest behavior, authentication, and backend setting validation.

## Current integration status and limits

- The MT5 implementation is complete but cannot be certified against your JustMarkets terminal without a running, logged-in demo terminal. Validate symbol suffix, filling modes, stop level, contract size, partial fills, and reconciliation on that account before use.
- The credential-free weekly, FMP, and generic calendar adapters are implemented. An FMP `402` means the supplied key's subscription does not permit that endpoint; use a compatible subscription or select the credential-free provider. The news fail-safe remains enabled in either case.
- Backtest results are evidence, not a guarantee. Results under 100 trades are explicitly marked statistically limited. Bar-level simulation resolves same-bar SL/TP ambiguity against the strategy (stop first).
- Mock market data is confined to mock mode. Selecting MT5 uses genuine MT5 candles and quotes; provider failure is surfaced rather than replaced with mock data.
- Automated breakeven, structural trailing, early invalidation exits, and lifecycle reconciliation are implemented but must be observed on the JustMarkets demo account before relying on them. Partial-profit automation remains intentionally unimplemented; full close is used for decisive reversal exits.
- The replay engine reuses completed-candle analysis, models spread/slippage/commission, stop-first same-bar ambiguity, breakeven/trailing, structural exits, average R, and setup breakdowns. It remains a bar-level research tool rather than a tick-perfect MT5 simulator.

