# Architecture (milestone 1)

`fart` is a read-only consumer of public Solana / pump.fun data. It never signs, never trades,
never posts. Milestone 1 delivers three things: complete and redundant ingestion, an event-time
data model in which hindsight is impossible, and honest outcome measurement. No social layer, no
Telegram, no ML, no web UI.

## Processes

```
 PumpPortal wss ──┐                                   ┌── fart-engine (15 s loop)
                  ├─► fart-ingest (asyncio daemon) ──► SQLite (WAL) ◄─┤
 Solana RPC wss ──┘        │  raw gz   │ sweep (RPC http)             └── fart-measure-track (5 min)
                           ▼           ▼                                   fart-measure-report (daily)
                    data/raw/...   coins / migrations / trades / coverage
```

* **fart-ingest** (`fart/ingest/service.py`): one PumpPortal connection (`pumpportal.py`), one RPC
  `logsSubscribe` connection on the pump.fun mint authority and migration account (`rpc_logs.py`),
  a periodic signature sweep + bounded transaction backfill over HTTP RPC (`sweep.py`), hourly
  coverage rows (`coverage.py`), weekly retention (`retention.py`). Every raw message is written to
  `data/raw/<source>/YYYYMMDD/HH.jsonl.gz` before normalization (`raw.py`).
* **fart-engine** (`fart/engine/service.py`): computes as-of features for watched mints and meta
  clusters, promotes watchlist entries, writes `features_asof` and `decisions` (kind `observe` /
  `observe_meta`). Observations only; nothing leaves the machine.
* **fart-measure-track** (`fart/measure/outcomes.py`): samples DexScreener for every subject under
  measurement, writes due `outcomes` rows (+1 h / +6 h / +24 h) and the paper trade per decision.
* **fart-measure-report** (`fart/measure/report.py`): the daily recall report to `out/recall/`.

All processes share `data/fart.db` (SQLite, WAL, 5 s busy timeout) and the budget lock file.
Cross-process coordination happens through tables only: the engine writes the `watchlist`, the
ingest daemon diffs it against its PumpPortal subscriptions every 5 s.

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
RPC `ws_connect`/`ws_subscribe`/`ws_notification` are accounted as estimated CU, notifications
aggregated per minute). When the RPC daily cap is hit the log stream disconnects and the sweep
pauses until the next UTC day; PumpPortal keeps running (graceful degradation).

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

Trades are stored only for watched mints (PumpPortal `subscribeTokenTrade`, cap 200). Reasons in
eviction order: `probation` (every new pump coin for its first 5 minutes, so early-buyer counts
exist for every coin), `early_buyers` (promoted by the engine), `meta_burst`, `graduation`
(24 h), `observe`. Entries expire by TTL or by idleness.

## Measurement

See `docs/METING.md` (Dutch) for definitions. Mechanically: `subjects()` = every `observe`
decision and every graduation of the last 25 h; the tracker samples DexScreener for all of them
every 5 minutes (`mcap_samples`), and once `anchor + horizon <= now` writes one `outcomes` row
per horizon from own trades (curve math times SOL/USD) and/or samples. Unknown stays NULL.

## Configuration

`config/*.json` hold every threshold and external fact; changing one is a commit, which changes
`rules_version`. Secrets live only in `$FART_ROOT/.env` (names in `.env.example`); URLs are
logged as host only.

## Milestone 2 hooks

* A social confirmation layer plugs in as another feature source behind `asof_clause()` (store
  `received_at` and the platform's own timestamp separately, as here).
* Telegram delivery consumes `decisions` rows; nothing in milestone 1 delivers anywhere.
* The engine's thresholds and the meta tokenizer are the first things the measured week should
  move; the daily report is the yardstick.
