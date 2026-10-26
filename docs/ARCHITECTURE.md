# Architecture (milestone 7)

`fart` is a read-only consumer of public Solana / pump.fun data. It never signs, never trades.
Milestone 1 delivered complete and redundant ingestion, an event-time data model in which hindsight
is impossible, and honest outcome measurement. Milestone 2 added per-mint trades over the RPC (PumpPortal
serves trades only to funded keys), pump-amm events, the Global-account check, an X confirmation layer
and Telegram notifications to the owner through a durable outbox. Milestone 3 repairs what the VPS
showed: one way to write to SQLite (short `BEGIN IMMEDIATE` transactions with retries), a trade stream
that only follows coins before graduation under an hourly CU envelope, system mints that can never
become coins, a backup script the syscall filter tolerates, and a daily report that says "not covered"
for hours fart was not running. Milestone 4 made the watchlist and the trade stream stable (strict
eviction, a reserve for probation, a frozen and bounded meta_burst choice, subscription hysteresis),
gave the stream a daily CU share instead of 1/24 per hour, calibrated the cost table and books websocket
frames per byte, and keeps the universe complete when PumpPortal reports the graduation of a coin fart
never saw created. Milestone 5 delivers the measurement week's instruments without changing a rule: a week
report with block-bootstrap intervals and the frozen go/no-go test, a tracker-coverage rule in the reports, a
community channel that is off unless two `.env` names are set (with a preview to the owner), and the trim of
the leftover `meta_burst` rows (schema v5). Milestone 6 lets the X layer run during the week without touching
it: with a token the social daemon runs in **shadow mode** (`X_MODE`): the same calls and money, but fast posts
land in `shadow_decisions`, confirmation counts in `shadow_confirmations`, and the engine passes neutral X
features and no mega post to every decision until `live` is allowed by the `x_live` rules key (a new
fingerprint). It also records per day what the engine saw of every candidate (`engine_candidates`, read by
`fart engine explain` and the daily report), verifies every backup by restoring it (`fart backup verify`), and
adds two read-only tools (`fart social dryrun`, `fart budget calibrate`). Milestone 7 turns the X confirmation layer into a
cost-aware planner (`fart/social/planner.py`, `fart/social/xbudget.py`): keys only from observations in a hard order, an
adaptive count schedule with stop rules per key (`x_keys`), a budget planner that defers on the hourly envelope and
never exceeds the cap, quality-per-dollar lines in the reports and `fart social calibrate` for the console prices. Still
no ML, no web UI, no trading, and the only outbound write anywhere is `sendMessage` to the owner chat (and, once Jim
switches it on after the week, to the community chat).

## Processes

```
 PumpPortal wss (creates, migrations) ──┐
 RPC wss: mint authority + migration ───┼─► fart-ingest (asyncio) ──► SQLite (WAL) ◄── fart-engine (15 s)
 RPC wss: one logsSubscribe per mint ───┘      │ raw gz  │ sweep/getBlockTime/Global (RPC http)
                                               ▼         ▼                          ◄── fart-social (15 s)  X API v2
                                        data/raw/...  coins / migrations / trades / coverage / ws_subscriptions
                                                                                    ◄── fart-notify (20 s)  Telegram
                                                                                    ◄── fart-measure-track (5 min), -report (06:00), -week (06:30)
```

* **fart-ingest** (`fart/ingest/service.py`): one PumpPortal connection (`pumpportal.py`; trade
  subscriptions only with `PUMPPORTAL_API_KEY`; a migration is accepted only for a known coin and never
  for a system mint), one RPC `logsSubscribe` connection on the pump.fun mint authority and migration
  account (`rpc_logs.py`), one RPC connection with a `logsSubscribe` per watched mint **before
  graduation** (`rpc_trades.py`: bonding curve; reasons probation / early_buyers / meta_burst; cap 150;
  2,000 notifications per subscription per hour; the stream's own CU capped at `daily_cu_share` / 24 of
  the daily cap per hour (0.35 / 24); probation dropped above 80% of the daily cap; a mint is subscribed
  after two consecutive syncs that want it and unsubscribed 60 s after the last one that did, every closed
  `ws_subscriptions` row carries its `close_reason`; graduated coins are measured by the tracker's
  samples), a periodic signature sweep + bounded transaction backfill (migrations first, CU-based daily
  limit) + `getBlockTime` fill over HTTP RPC (`sweep.py`, `rpc_trades.fill_block_times`), hourly coverage
  rows (`coverage.py`), weekly retention (`retention.py`), and a Global-account check at startup
  (`chain/global_account.py`, logged only). Every raw message is written to
  `data/raw/<source>/YYYYMMDD/HH.jsonl.gz` before normalization (`raw.py`); `fart ingest replay` feeds
  those files back to the handlers after a failure.
* **fart-engine** (`fart/engine/service.py`): computes as-of features for watched mints and meta
  clusters (the X features and the latest fast mega post only in `live` mode; otherwise the neutral values of
  empty social tables, so a shadow day's decisions equal a day's without X), promotes watchlist entries,
  writes `features_asof` and `decisions` (kind `observe` / `observe_meta`), and since milestone 6 the per-day
  candidate maxima (`engine_candidates`, `fart/engine/candidates.py`) that `fart engine explain --day` and the
  daily report's "Waarom wel of geen observaties" read. Observations only.
* **fart-social** (`fart/social/service.py`): X mega-account watch (60 s in the peak hours 13:00-02:00 UTC, 120 s
  outside), velocity refresh (120 s), KOL watch (180 s / 300 s), the confirmation planner and `fast_post` decisions;
  idles without `X_BEARER_TOKEN` or with `X_MODE=off`. Since milestone 6 the default with a token is shadow mode
  (`fart/social/shadow.py`): `shadow_decisions` instead of `decisions`, `shadow_confirmations` instead of
  `social_counts`; `live` needs `X_MODE=live` and `"x_live": true` in config/engine.json. Since milestone 7 the
  confirmation layer is a planner (`fart/social/planner.py`): keys only from observations (the mint, a non-generic
  ticker, the exact multi-word name; the 5 strongest themes), youngest coin first, at most 20 at once; a count as soon
  as a key is admitted, every 15 min for two hours, hourly after that, at most 12; one search of 10 posts with a user
  lookup of at most 3 authors when a count reaches the threshold; a key stops as `confirmed`, `refuted` (three flat
  counts), `expired`, `graduated`, `dead`, `displaced` or `exhausted`, recorded in `x_keys` with what it cost. Money:
  `fart/social/xbudget.py` (the budget planner); `fart social dryrun` estimates it without HTTP (`fart/social/dryrun.py`,
  with the full-load formula), `fart social calibrate` compares the ledger with the console (`fart/social/calibrate.py`).
* **fart-notify** (`fart/notify/service.py`): builds Dutch messages into the `outbox` (observation
  batches, fast posts, the daily digest, health alerts) and sends what is due to the owner chat
  (`notify/telegram.py`); idles unless `TELEGRAM_NOTIFY_ENABLED=1`.
* **fart-measure-track** (`fart/measure/outcomes.py`): samples DexScreener for every subject under
  measurement, writes due `outcomes` rows (+1 h / +6 h / +24 h) and the paper trade per decision.
* **fart-measure-report** (`fart/measure/report.py`): the daily recall report to `out/recall/`; since
  milestone 5 with the tracker-coverage rule (a graduation without a sample at least every 10 minutes is
  "uitkomst onbekend", never "geen winnaar") and the per-rules_version blocks the week report aggregates.
* **fart-measure-week** (`fart/measure/week.py`): the rolling 7-day report (`week-<end>.md|json`, 06:30 UTC,
  the Monday run is the calendar week): daily reports rebuilt per method_version, pooled per rules fingerprint
  (`fart/version.py`: identical rule files = one segment, every rules_version listed), hit rate with unknown in
  the denominator, recall and lead time on the earliest coin-or-theme decision, block bootstrap with days as
  blocks (9,999 draws, fixed seed; "te weinig data" under 3 covered days or 20 winners), costs, and the frozen
  go/no-go criteria of `config/measure.json` → `community_gate` printed as voldaan / niet voldaan / te weinig
  data. It decides nothing and sends nothing.

All processes share `data/fart.db` (SQLite, WAL, 15 s busy timeout, schema `PRAGMA user_version` 7) and
the budget lock file. Cross-process coordination happens through tables only: the engine writes the
`watchlist` (and its frozen meta_burst choice in kv `engine.meta_burst_sets`), the ingest daemon diffs
the watchlist against its subscriptions every 5 s with hysteresis; the social daemon writes `decisions`,
the notify daemon reads them.

## Writing to SQLite (milestone 3)

There is one way to write: `db.write_tx(conn, fn)` runs `fn` inside `BEGIN IMMEDIATE ... COMMIT` and
retries a busy/locked error up to three times with a short jitter; `db.read_tx()` is a closed read-only
snapshot for the heavy reads. No code outside `fart/db.py` issues a plain `BEGIN` any more: in WAL mode a
deferred transaction that reads first and writes later fails at once with SQLITE_BUSY_SNAPSHOT when any
other connection committed in between, and the busy handler is not consulted for that error. That is what
stopped the engine on the VPS (the tick read features of 200 coins inside its transaction, then wrote).
Rules: compute (features, clustering, decoding) and talk to the network outside every transaction, write
in milliseconds, count what still fails (`engine.tick_failed`, `ingest.handle_failed.<source>`, with a
per-day twin for the report) and keep the raw line replayable.

## The budget gate (`fart/net/budget.py`)

Every external call passes `Budget.acquire(source, method, cost)`:

1. backoff check (`health.backoff_until`, set on 429/5xx/network errors, honoring Retry-After),
2. daily caps per source: requests and cost units (for `rpc` the cost is estimated compute units
   from the table in `config/budget.json`, since milestone 4 at the figures of Alchemy's compute-unit
   page for the HTTP methods and per byte for websocket frames; the cap comes from `RPC_DAILY_BUDGET_CU`),
3. token bucket (`rate_buckets`, persisted, consumed inside `BEGIN IMMEDIATE` under a file lock so
   several processes share one bucket).

A refused call is written to `api_ledger` with `blocked_reason` and logged; cap warnings are
throttled to once per hour. `Budget.record()` writes the ledger row and updates `health`
(last_ok/last_err/counts/latency EWMA/consecutive errors). `fart status` prints all of it.

Websocket connects go through the same gate (`pumpportal.connect` is limited to 12 per hour; RPC
`ws_connect` is a flat estimate). The frames of the two RPC subscription connections are booked per byte
(`rpc.cu_per_byte`, 0.0002 by the Solana section of Alchemy's page, 0.04 by its subscription-API
section: the open calibration item of `docs/UNVERIFIED.md`): `ws_notification`/`ws_subscribe`/
`ws_unsubscribe` for the log stream and `ws_trade_notification`/`ws_trade_subscribe`/`ws_trade_unsubscribe`
for the trade stream, aggregated per minute with the byte count kept in `api_ledger.bytes`, so
`fart status` shows bytes and CU per stream. The trade stream has its own hourly envelope
(`daily_cu_share` / 24 of the daily cap on its own methods, unflushed frames included, so the other RPC
users always keep the remaining share of the day) and a per-subscription cap; at 80% of the RPC daily cap
it keeps only the early_buyers / meta_burst subscriptions; at the cap the log streams disconnect and the
sweep pauses until the next UTC day; PumpPortal keeps running (graceful degradation).

The X source costs estimated USD per call (`X_DAILY_BUDGET_USD` hard cap, actual cost booked from the
response: posts and author expansions billed once per UTC day, every `x_spend` row with its unit) and adds a planner on
top (`XBudget.admit`, milestone 7): every call carries a ceiling cost (every post and author new) and an expected cost
(today's average for the kind); the hard rules (the cap, 30% of it reserved for the account watches, the $1/day KOL
sub-cap) test the ceiling and refuse for the day, so the cap can never be exceeded; the hourly envelope (weighted to
13:00-02:00 UTC) tests the expected cost and defers to the next hour instead of refusing (counted in kv, never a blocked
row). Inside a round the search of a confirmed key goes first, then the counts, then the account watches. The Telegram
source is a 20-per-minute bucket. Refusals are rows in `api_ledger` with a reason; `fart status` shows spend per RPC
method and per X kind, the planner's keys and the planned versus actual USD per kind.

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
| RPC signature sweep (every 5 min) | chain time for everything known by signature; the list of signatures we never saw | 40 CU per `getSignaturesForAddress` call (up to 1000 signatures) |
| RPC `getTransaction` backfill (bounded) | the missed creates/migrations themselves | 40 CU each, max 100 per run, 15% of the daily CU cap per day (11,250 at 3M) |

`coins.sources` is the JSON list of sources that saw a mint; `coverage` counts `creates_pp`,
`creates_rpc`, `creates_union`, `grads`, `grads_without_create` per UTC hour, and `coverage_gaps`
records every interval a stream was down (startup, disconnect, watchdog, budget cap, shutdown, and `down`
for a crash or host reboot, written at the next start from the previous heartbeat). A source counts from
its first run; an hour is *covered* when a create source was active during part of it and *complete* only
when a source ran through the whole hour without an unrecovered gap. A migration message naming a system
mint (USDC, USDT, WSOL, PUMP) is refused into `rejected_messages`; a migration of a coin fart never saw
created (from PumpPortal: a coin created before fart started; from the RPC stream: a missed create) is
stored with `migrations.coin_unknown=1`, is part of the universe (`grads_without_create`), is never
watched, and loses the flag when the create arrives later (sweep, backfill, replay).

## Watchlist and trades

Trades are stored only for watched mints (watchlist cap 200). Reasons in priority order (lowest first):
`probation` (every new pump coin for its first 5 minutes, so early-buyer counts exist for every coin),
`early_buyers` (promoted by the engine), `meta_burst`, `graduation` (only with `subscribe_graduations`,
off by default since milestone 3), `observe`. Since milestone 4 (`fart/ingest/watch.py`): an entry is
evicted only by a *strictly* higher priority (equal priority never displaces), `since` is written once
(a repeated add and a reactivation keep it, so every ordering by `since` is stable; the idle expiry counts
from `activated_at`), and `probation_reserve` (100 of the 200 slots) can only be used by probation while
the other reasons share the rest. The engine's meta_burst entries are a frozen, bounded choice
(`decide.meta_burst_sets`: the 5 youngest not yet graduated coins of every bursting meta, at most 40 in
all, strongest metas first; a set is kept while its meta bursts and released 10 minutes after it stopped;
persisted in kv so a restart continues with the same set). Entries expire by TTL or by idleness. Since
milestone 5 the engine also trims `meta_burst` rows above the bound of 40 every tick (oldest first, never a
row of the frozen sets or one that traded in the last 10 minutes), because the 195 rows milestone 3 had
written kept their slots after the M4 upgrade and left probation 5 of its 100 reserved slots; schema v5 did
the same once at the upgrade.
Sources of trades:
`rpc_trades` (TradeEvent on the bonding curve, decoded from the per-mint RPC subscription), `pump_amm`
(BuyEvent/SellEvent whose pool maps to a known base mint, e.g. the first pool buy inside the migration
transaction), `rpc_logs` (the dev buy inside the create transaction) and `pumpportal` (only with a funded
key). A trade seen twice keeps one row and fills chain time where the first arrival lacked it. After
graduation the tracker's 5-minute DexScreener samples carry the measurement.

## Social confirmation

`social_posts` keeps the platform time and our clock apart; `social_counts` (live) or `shadow_confirmations` (shadow)
hold the per-key counts; `x_spend`/`x_dedupe` the money; `x_keys` the planner's state per key episode (milestone 7).
Confirmation runs on unambiguous keys only (the mint, a non-generic `$TICKER`, an exact multi-word name; a theme term of
eight or more characters), only for observations, in a hard order, with a per-key schedule and stop rules so that every
dollar can be traced to an observation it confirmed or refuted; the reports show what a confirmation costs. `fast_post`
decisions tie a fast mega post to a burst of coin names and stay observations: no tier words anywhere.

## Notifications

`outbox` rows are the unit of delivery (`dedupe_key` UNIQUE, retry ladder, `gave_up`), the sender
never exceeds 20 messages a minute and honors 429 `retry_after`. Observation batches every 5 minutes
(max 6/hour), fast posts immediately, the digest at 06:10 UTC, health alerts with a 1 h cooldown.
Dutch, facts only, Europe/Amsterdam with UTC in brackets, a fixed footer.

Milestone 5 adds the community channel behind a gate: `Notifier.allowed_chats` is the owner chat plus the
community chat only while `TELEGRAM_COMMUNITY_CHAT_ID` is set and `TELEGRAM_COMMUNITY_ENABLED=1`; every
builder queues through `_enqueue()` (refuses any other chat) and the sender marks a pending row for a chat
that is not allowed `gave_up` without calling the API. The community text (`format.community_message`: what
was seen, why it stands out, the frozen rules, a fixed warning, the mint as text, no link) goes to the
community chat when enabled and, with `TELEGRAM_COMMUNITY_PREVIEW=1`, to the owner headed "VOORBEELD
community-bericht" within the owner limits. Rules for both: one per coin and per theme per 24 h, 6 per hour,
quiet hours, a weekly accountability message (Monday 06:45 UTC) that puts the week report's go/no-go section
into plain words.

## Measurement

See `docs/METING.md` (Dutch) for definitions. Mechanically: `subjects()` = every `observe`
decision and every graduation of the last 25 h; the tracker samples DexScreener for all of them
every 5 minutes (`mcap_samples`), and once `anchor + horizon <= now` writes one `outcomes` row
per horizon from own trades (curve math times SOL/USD) and/or samples. Unknown stays NULL.

## Configuration

`config/*.json` hold every threshold and external fact; changing one is a commit, which changes
`rules_version`. Secrets live only in `$FART_ROOT/.env` (names in `.env.example`); URLs are
logged as host only.

## Backups (milestone 6)

`deploy/backup.sh` (timer 05:30 UTC) writes the online backup, compresses it, and then runs `fart backup verify`
(`fart/backup.py`): the newest archive is unpacked under `data/backup/.verify-<pid>/`, `quick_check`ed, its row
counts of `coins`, `migrations`, `decisions` and `outcomes` compared with the live database (live >= archive),
the copy removed; a failure exits 1 and fails the unit; the result lands in kv `backup.verify.last` for
`fart status`. The offsite copy (scp from a second machine) and the restore drill are in `deploy/DEPLOY.md`.

## What comes after milestone 6

* The measurement week itself (frozen rules `150ca73cb354`, pooled with every later commit that keeps the
  rules fingerprint; first full day 27 Oct 2026). Its verdict is the week report's go/no-go section of
  `fart measure week --end 2026-11-02 --days 7`; only then does Jim decide about `TELEGRAM_COMMUNITY_ENABLED`.
  The preview mode shows him during the week what the community would have received.
* X during the week: the token may go in after a day of watching without X; the watch then runs in shadow
  mode under the milestone-7 planner (within `X_DAILY_BUDGET_USD` 15.00) and the reports' "X-schaduw" section shows
  what it would have added and what a confirmation cost. The step shadow → live is a rules change (`"x_live": true` in
  config/engine.json, a new fingerprint) after the week, never during it, taken on the quality-per-dollar figures.
* Reconcile every estimate in `docs/UNVERIFIED.md` on the VPS, first of all the CU per byte of the
  websocket frames against the Alchemy dashboard (`fart budget calibrate --day D --dashboard-cu N`: 0.0002 or
  0.04 decides how much of the trade stream the daily share lets through), then the HTTP methods, the X prices
  (`fart social dryrun` before, `fart social calibrate --day D --console-usd N` after) and the real recordings that
  prove the event layouts byte for byte.
* Why the coin branch gives few observations: `fart engine explain --day D` and the daily report show the
  distribution; a change of thresholds, if any, is a decision after the week (a new rules_version), not a
  conclusion of this milestone.
