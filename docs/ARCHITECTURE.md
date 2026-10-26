# Architecture (milestone 2)

`fart` is a read-only consumer of public Solana / pump.fun data. It never signs, never trades.
Milestone 1 delivered complete and redundant ingestion, an event-time data model in which hindsight
is impossible, and honest outcome measurement. Milestone 2 adds per-mint trades over the RPC (PumpPortal
serves trades only to funded keys), pump-amm events, the Global-account check, an X confirmation layer
and Telegram notifications to the owner through a durable outbox. Still no ML, no web UI, no trading,
and the only outbound write anywhere is `sendMessage` to the owner chat.

## Processes

```
 PumpPortal wss (creates, migrations) ──┐
 RPC wss: mint authority + migration ───┼─► fart-ingest (asyncio) ──► SQLite (WAL) ◄── fart-engine (15 s)
 RPC wss: one logsSubscribe per mint ───┘      │ raw gz  │ sweep/getBlockTime/Global (RPC http)
                                               ▼         ▼                          ◄── fart-social (15 s)  X API v2
                                        data/raw/...  coins / migrations / trades / coverage / ws_subscriptions
                                                                                    ◄── fart-notify (20 s)  Telegram
                                                                                    ◄── fart-measure-track (5 min), -report (daily)
```

* **fart-ingest** (`fart/ingest/service.py`): one PumpPortal connection (`pumpportal.py`; trade
  subscriptions only with `PUMPPORTAL_API_KEY`), one RPC `logsSubscribe` connection on the pump.fun mint
  authority and migration account (`rpc_logs.py`), one RPC connection with a `logsSubscribe` per watched
  mint (`rpc_trades.py`: bonding curve before graduation, pump-amm pool after it; cap 150; probation
  dropped above 80% of the CU cap), a periodic signature sweep + bounded transaction backfill +
  `getBlockTime` fill over HTTP RPC (`sweep.py`, `rpc_trades.fill_block_times`), hourly coverage rows
  (`coverage.py`), weekly retention (`retention.py`), and a Global-account check at startup
  (`chain/global_account.py`, logged only). Every raw message is written to
  `data/raw/<source>/YYYYMMDD/HH.jsonl.gz` before normalization (`raw.py`).
* **fart-engine** (`fart/engine/service.py`): computes as-of features for watched mints and meta
  clusters (now including the X features and the latest fast mega post), promotes watchlist entries,
  writes `features_asof` and `decisions` (kind `observe` / `observe_meta`). Observations only.
* **fart-social** (`fart/social/service.py`): X mega-account watch (60 s), velocity refresh (120 s),
  KOL watch (180 s), contract-address / ticker confirmation counts (15 min per key) and `fast_post`
  decisions; idles without `X_BEARER_TOKEN`. Money: `fart/social/xbudget.py`.
* **fart-notify** (`fart/notify/service.py`): builds Dutch messages into the `outbox` (observation
  batches, fast posts, the daily digest, health alerts) and sends what is due to the owner chat
  (`notify/telegram.py`); idles unless `TELEGRAM_NOTIFY_ENABLED=1`.
* **fart-measure-track** (`fart/measure/outcomes.py`): samples DexScreener for every subject under
  measurement, writes due `outcomes` rows (+1 h / +6 h / +24 h) and the paper trade per decision.
* **fart-measure-report** (`fart/measure/report.py`): the daily recall report to `out/recall/`.

All processes share `data/fart.db` (SQLite, WAL, 5 s busy timeout, schema `PRAGMA user_version` 2) and
the budget lock file. Cross-process coordination happens through tables only: the engine writes the
`watchlist`, the ingest daemon diffs it against its subscriptions every 5 s; the social daemon writes
`decisions`, the notify daemon reads them.

## The budget gate (`fart/net/budget.py`)

Every external call passes `Budget.acquire(source, method, cost)`:

1. backoff check (`health.backoff_until`, set on 429/5xx/network errors, honoring Retry-After),
2. daily caps per source: requests and cost units (for `rpc` the cost is estimated compute units
   from the table in `config/budget.json`; the cap comes from `RPC_DAILY_BUDGET_CU`),
3. token bucket (`rate_buckets`, persisted, consumed inside `BEGIN IMMEDIATE` under a file lock so
   several processes share one bucket).

A refused call is written to `api_ledger` with `blocked_reason` and logged; cap warnings are
throttled to once per hour. `Budget.record()` writes the ledger row and updates `health`
(last_ok/last_err/counts/latency EWMA/consecutive errors). `fart status` prints all of it.

Websocket connects go through the same gate (`pumpportal.connect` is limited to 12 per hour;
RPC `ws_connect`/`ws_subscribe`/`ws_unsubscribe`/`ws_notification` are accounted as estimated CU,
aggregated per minute). At 80% of the RPC daily cap the trade stream keeps only the observe /
early_buyers / meta_burst subscriptions; at the cap the log streams disconnect and the sweep pauses
until the next UTC day; PumpPortal keeps running (graceful degradation).

The X source costs estimated USD per call (`X_DAILY_BUDGET_USD` hard cap, actual cost booked from the
response: posts and author expansions billed once per UTC day) and adds a pacing policy on top
(`XBudget`: hourly envelope weighted to 13:00-02:00 UTC, 30% reserved for the account watches,
$1/day KOL sub-cap). The Telegram source is a 20-per-minute bucket. Refusals of every kind are rows in
`api_ledger` with a reason; `fart status` shows spend per RPC method and per X kind.

## Event time versus our clock

Every chain-derived row has `slot` and `block_time` (chain) **and** `received_at` (our clock).
PumpPortal messages carry no chain time; the signature sweep (`getSignaturesForAddress`, ~50 CU per
1000 signatures) fills `slot`/`block_time` for known signatures within minutes, and RPC log events
carry the event's own `timestamp`.

`db.asof_clause()` is the single predicate used by every feature query:

```
received_at <= as_of AND (block_time IS NULL OR block_time <= as_of)
```

`received_at <= as_of` is the no-hindsight guarantee (the row had reached us by then);
`block_time <= as_of` is the explicit event-time guarantee. Because a message cannot reach us
before it happened on chain, `received_at` is a conservative upper bound of chain time when the
latter is still unknown. `tests/test_engine.py::test_coin_features_as_of_is_immutable_under_later_inserts`
proves that a feature computed as of T does not change when later rows (or late-arriving rows with
earlier chain time) are inserted.

`decisions` and `outcomes` are append-only, enforced by SQLite triggers. Every row carries
`rules_version` (git commit of the code) or `method_version` (measurement semantics). A recompute
is a new row with a new version; nothing is ever updated in place.

## Redundant ingestion of creates and graduations

| path | what it gives | cost |
|---|---|---|
| PumpPortal `subscribeNewToken` / `subscribeMigration` | creates and migrations in real time, names, dev buy, market cap in SOL, no chain time | free, one connection |
| RPC `logsSubscribe mentions=[mint authority]` / `[migration account]` | the same events from chain logs with slot and event timestamps, decoded from `Program data:` lines | CU per notification |
| RPC signature sweep (every 5 min) | chain time for everything known by signature; the list of signatures we never saw | ~50 CU per 1000 signatures |
| RPC `getTransaction` backfill (bounded) | the missed creates/migrations themselves | 59 CU each, max 100 per run / 1500 per day |

`coins.sources` is the JSON list of sources that saw a mint; `coverage` counts `creates_pp`,
`creates_rpc`, `creates_union`, `grads`, `grads_without_create` per UTC hour, and `coverage_gaps`
records every interval a stream was down (startup, disconnect, watchdog, budget cap, shutdown).

## Watchlist and trades

Trades are stored only for watched mints (watchlist cap 200). Reasons in eviction order:
`probation` (every new pump coin for its first 5 minutes, so early-buyer counts exist for every coin),
`early_buyers` (promoted by the engine), `meta_burst`, `graduation` (24 h), `observe`. Entries expire
by TTL or by idleness. Sources of trades: `rpc_trades` (TradeEvent on the bonding curve, decoded from
the per-mint RPC subscription), `pump_amm` (BuyEvent/SellEvent on the pump-amm pool after graduation),
`rpc_logs` (the dev buy inside the create transaction) and `pumpportal` (only with a funded key). A
trade seen twice keeps one row and fills chain time where the first arrival lacked it.

## Social confirmation

`social_posts` keeps the platform time and our clock apart; `social_counts` holds the per-key
counts; `x_spend`/`x_dedupe` the money. Confirmation runs on unambiguous keys only (the mint, a
non-generic `$TICKER`, an exact multi-word name). `fast_post` decisions tie a fast mega post to a
burst of coin names and stay observations: no tier words anywhere.

## Notifications

`outbox` rows are the unit of delivery (`dedupe_key` UNIQUE, retry ladder, `gave_up`), the sender
never exceeds 20 messages a minute and honors 429 `retry_after`. Observation batches every 5 minutes
(max 6/hour), fast posts immediately, the digest at 06:10 UTC, health alerts with a 1 h cooldown.
Dutch, facts only, Europe/Amsterdam with UTC in brackets, a fixed footer.

## Measurement

See `docs/METING.md` (Dutch) for definitions. Mechanically: `subjects()` = every `observe`
decision and every graduation of the last 25 h; the tracker samples DexScreener for all of them
every 5 minutes (`mcap_samples`), and once `anchor + horizon <= now` writes one `outcomes` row
per horizon from own trades (curve math times SOL/USD) and/or samples. Unknown stays NULL.

## Configuration

`config/*.json` hold every threshold and external fact; changing one is a commit, which changes
`rules_version`. Secrets live only in `$FART_ROOT/.env` (names in `.env.example`); URLs are
logged as host only.

## What comes after milestone 2

* A measured week with frozen rules (one `rules_version`) before anything reaches a community chat;
  the daily digest and `out/recall/` are the yardstick.
* Reconcile every estimate in `docs/UNVERIFIED.md` on the VPS: CU per method, X prices, the pump-amm
  layouts, the Global account's newer fields.
