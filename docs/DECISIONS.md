# Decisions (conservative defaults taken during the build)

Format: decision — why — where. Newest at the bottom.

1. **Repository bootstrap**: empty repo → initial commit on `main` (README stub, `.gitignore`,
   `pyproject.toml`), then all work on `m1-ingest-measure-b`. The harness-designated branch
   `claude/gracious-goldberg-pbboq7` is pushed as a mirror of the same head so both names exist;
   the PR is `m1-ingest-measure-b` → `main` as the brief asks.
2. **Standard library + `websockets` only**; `pytest` for dev. No frameworks, no ORM, no Docker.
3. **No network in the sandbox** → synthetic fixtures generated from documented layouts
   (`tests/fixtures/gen_fixtures.py`), everything external listed in `docs/UNVERIFIED.md`, and a
   drop-in path for real recordings (`tests/fixtures/*_real.json`).
4. **As-of predicate** `received_at <= as_of AND (block_time IS NULL OR block_time <= as_of)`
   (`fart/db.py::asof_clause`): the first term makes hindsight impossible, the second is the
   explicit event-time rule; `received_at` is a conservative upper bound of unknown chain time.
5. **Coverage hour bucketing** by `COALESCE(block_time, received_at)`; an hour is `complete` when
   it is over and at least one *configured* create source had no gap in it. Sources that never
   ran do not make an hour complete.
6. **Watchlist probation**: every new pump coin is subscribed for its first 5 minutes so early
   buyer counts exist for all coins (the brief's "≥ N buyers in the first minutes" needs trade
   data for unwatched coins; subscribing to the whole pump.fun program on the RPC would cost far
   more). Cap 200 with priority eviction (probation first, observe last).
7. **Decision kinds**: `observe` (coin) and `observe_meta` (term), verdict `observe`. Cooldown
   30 min per key. Features are snapshotted into the decision row; `features_asof` rows are
   written only when a decision is emitted (not every 15 s for every watched coin).
8. **Schema additions beyond the brief**: `outcomes.subject/mint/anchor_time/sol_usd` (so the
   graduation universe needs no decision), `mcap_samples` (peak needs samples over time),
   `raw_undecoded`, `enrich_queue`, `coins.sources`/`bonding_curve`/`signature`,
   `watchlist.active/last_trade_at`. All documented in `fart/db.py`.
9. **Outcome rows are written even when everything is unknown** so the report can count unknowns;
   `hit_100k` tri-state: peak unknown → NULL; peak < 100k → 0 (a known miss); peak ≥ 100k with
   unknown volume → NULL. `bundled` tri-state: instant graduation or dev buy ≥ 50% → 1; no dev-buy
   information → NULL; otherwise 0 once an instant graduation is ruled out.
10. **Websocket connect semantics in the budget**: a session that connected and later dropped is a
    successful connect (the client's own 20–180 s backoff applies); a failed handshake is an error
    and also arms the budget backoff. A stable session (> 60 s) resets the client's backoff ladder.
11. **RPC CU accounting of notifications** is aggregated once per minute into the ledger; the
    daily cap closes the log stream and pauses the sweep until the next UTC day, PumpPortal stays.
12. **Trades only for watched mints**; the creator's dev buy from the create transaction is stored
    when the mint is on probation (always, for pump coins).
13. **SOL/USD**: DexScreener deepest USD-quoted WSOL pair, fallback GeckoTerminal, 60 s cache,
    stale cache returned and flagged when both fail; recorded on every sample and outcome.
14. **Paper trade**: entry on the reserves *after* the first trade at/after `as_of + 20 s`
    (conservative), flat 1% curve fee each way plus a network fee, trailing stop 30% from the peak
    or 24 h max hold, no post-graduation path (exit reason `no_exit_data`), partial fill flagged.
    Computed once per decision when its 24 h outcome is written.
15. **Volume for sample-based outcomes** uses DexScreener's trailing `volume.h1/h6/h24` of the
    sample nearest the horizon end (approximation; recorded as `volume_src`).
16. **Retention**: raw gz 30 days; trades 14 days for non-watched mints, samples/ledger 30 days,
    features 30 days — archived to `data/archive/*.jsonl.gz` then deleted, weekly. `decisions`,
    `outcomes`, `paper`, `coins`, `migrations`, `coverage*` are never pruned in milestone 1.
17. **Report `complete`** = 24 complete coverage hours AND every graduation of the day has a 24 h
    outcome row. Dutch headings, JSON alongside, atomic writes.
18. **Deployment**: no `pip install` of the package; `WorkingDirectory=/opt/fart/app` and
    `python -m fart`. Code copied by `rsync --delete`; `VERSION` file carries the commit for
    `rules_version` where `.git` is absent. A backup timer was added (cheap, not in the brief).
19. **Public RPC fallback** when `RPC_HTTP_URL` is empty, with its own stricter budget source.
20. **Engine RPC enrichment** limited to 3 holder lookups + 3 creator lookups per 15 s loop, only
    for candidates at or near the observe thresholds.
21. **Tests run on Python 3.13** (sandbox); code written for 3.12 (no 3.13-only syntax).

## Milestone 2

22. **Branch and PR**: all work on `m2-rpc-trades-social-telegram` from `main`; the harness branch
    `claude/determined-ptolemy-fowcar` mirrors the same head; the PR is `m2-rpc-trades-social-telegram` → `main`.
23. **PumpPortal trades only with a funded key**: without `PUMPPORTAL_API_KEY` no `subscribeTokenTrade` is
    ever sent (PumpPortal refuses them anyway and the client used to re-send batches every 5 s); the
    connection stays creates + migrations. With a key the URL gets `?api-key=` (never logged) and the
    milestone-1 trade subscriptions come back. The mode is logged once at startup (`pp.mode`).
24. **Per-mint trades over the RPC websocket** (`fart/ingest/rpc_trades.py`): a second connection next to the
    creates/migrations stream, one `logsSubscribe` per watched mint. Subscription address: the bonding
    curve before graduation (from the CreateEvent, else derived as the PDA `["bonding-curve", mint]`) and
    the pump-amm pool after it (from the migration event; the mint itself when the pool is unknown).
    `config/ingest.json` → `rpc_trades.mention = "mint"` switches to the mint everywhere if the VPS shows
    that TradeEvents do not arrive on the curve address. Only a keyed RPC runs this client.
25. **Subscription policy** (`config/engine.json` → `watchlist`): at most `rpc_trades_cap` (150) subscriptions,
    chosen by watchlist priority (observe > graduation > meta_burst > early_buyers > probation) and
    recency; above `rpc_trades_cu_drop_fraction` (80%) of the daily CU cap only observe/early_buyers/
    meta_burst keep a subscription. Probation stays the default for every coin's first 5 minutes
    (`early_window_s`) because that is where the unique-buyer counts come from. Every notification,
    subscribe and unsubscribe is booked as estimated CU (aggregated per minute, as in milestone 1).
26. **PDA derivation in stdlib** (`fart/chain/pda.py`): the Ed25519 on-curve check follows RFC 8032 /
    curve25519-dalek decompression; verified offline because the pump.fun Global, mint-authority and
    event-authority PDAs reproduce their known addresses. The pump-amm pool derivation is a guess and is
    only exposed through `fart chain pda`.
27. **pump-amm events**: `BuyEvent`/`SellEvent`/`CreatePoolEvent` layouts from the PumpSwap IDL as remembered,
    TO VERIFY. A swap whose `pool` differs from the pool we subscribed to is rejected and counted
    (`decode.pump_amm_pool_mismatch`) instead of stored: a cheap, permanent layout self-check. Swaps are
    stored with `source='pump_amm'`, pool reserves in `virtual_sol/virtual_token` so the curve price/mcap
    math applies unchanged (an AMM price, one trade's worth of error if the reserves are pre-swap).
28. **Program attribution of `Program data:` lines** from the invoke nesting; stored in
    `raw_undecoded.program`. Unknown discriminators from Jupiter/Orca/Raydium in the same transaction are
    no longer mistaken for new pump.fun events (`fart events classify`).
29. **Fees**: `fee_bps` 95 (Global account), `creator_fee_bps` 30 (TO VERIFY), `total_curve_fee_bps` 125 used
    by every quote and therefore by the paper trade (round trip ≈ 2.5%). `fart chain global` exits 1 on a
    config/chain mismatch; the ingest daemon runs the same check at startup and only logs.
30. **`global` is a Python keyword**, so the module is `fart/chain/global_account.py`.
31. **Duplicate trades** (PumpPortal with key + RPC stream): the second arrival is not inserted again but fills
    slot/block_time/reserves/buyer where the first row lacked them.
32. **Coverage after a restart**: gaps left open by the previous process are closed at startup with reason
    `restart`; a complete, successful signature sweep (not a first run, not truncated) marks every closed
    stream gap that ended before it as `recovered_by='sweep'`; budget-cap gaps are never recovered because
    the sweep shares the cap. The sweep opens its own `rpc_sigs` gap while it fails. An hour is complete
    when a configured create source has no *unrecovered* gap.
33. **Schema v2** is one idempotent migration (`CREATE IF NOT EXISTS` plus guarded `ALTER TABLE`), applied
    automatically by any process that opens the database; milestone-1 rows are untouched
    (`tests/test_m2_ingest.py::test_migration_v1_to_v2_preserves_milestone1_data`).
34. **X is confirmation, never the trigger** (`fart/social/`): a separate daemon (`fart-social`, 15 s loop) that
    idles with a heartbeat without `X_BEARER_TOKEN`. Posts keep `created_at` (platform) and `received_at`
    (ours) apart; the X features in `fart/engine/features.py` use `received_at <= as_of AND created_at <= as_of`.
35. **X money**: one budget source `x` whose cost is estimated USD per call (hard cap `X_DAILY_BUDGET_USD`,
    default $3.00, enforced by the generic gate on the actual costs) plus `XBudget` pacing: an hourly
    envelope weighted 1.0 for 13:00-02:00 UTC and 0.4 elsewhere, 30% of the cap reserved for the account
    watches (confirmation calls may use at most 70%), a $1.00/day KOL sub-cap. A refused call is ledgered
    with its reason (`hourly_envelope`, `account_watch_reserve`, `kol_daily_cap`, `daily_cost_cap`).
    The per-kind USD ledger is `x_spend`; posts and author expansions are billed once per UTC day
    (`x_dedupe`). The estimate before an incremental search is one new post + one new author; a single
    call can therefore exceed the cap by at most max_results x ($0.005 + $0.010).
36. **Prices** (`config/x.json`) are the previous builder's notes of 24 Oct 2026 and are TO VERIFY;
    `GET /2/tweets?ids=` re-reads are booked as `refresh` at $0 (assumed free same-day, TO VERIFY).
37. **Fast post** = likes/min >= 150 with >= 3,000 likes, or quotes/min >= 3, within 2 h of posting; velocity
    from the last two metric samples (`social_post_metrics`), else the average since posting. A `fast_post`
    decision needs a rare word of the post (not in the meta stopwords nor in the extra English list in
    `config/x.json`) to appear in >= 3 pump.fun coin names created within 30 min before or after the post by
    >= 2 creators. One decision per post (`decisions.term = 'x:<post id>'`, mint NULL, matched words and
    mints in `features_json`); verdict stays `observe`, no tier words.
38. **Confirmation keys are unambiguous only**: the mint itself always; `$TICKER` only when >= 3 characters,
    not in the generic list and not a stopword; the exact coin name only when it has >= 2 words that are
    not all stopwords; a meta term only when it is a bigram or >= 8 characters. Only mints with an
    `observe` decision in the last 6 h that are still on the active watchlist are confirmed, most recent
    first, at most 4 keys per 15 s tick and one `counts/recent` per key per 15 min; a 10-post search follows
    only when the last hour counted >= 10 posts. `n_last_hour` = the hourly buckets ending within the last
    60 minutes (so 60-120 minutes of data); `n_24h` = the total with `start_time` 24 h back.
39. **Follower weighting** of mentions = sum of log10(1 + followers) over the posts of the last hour.
40. **Telegram goes through a durable outbox** (`fart/notify/`): a message is a row first (`outbox`,
    `dedupe_key` UNIQUE), the 20 s sender delivers what is due with at most 20 messages per minute, honors
    `retry_after` on 429, retries after 15/60/300/900/3600 s and then marks `gave_up`; a 4xx other than
    429 (bad HTML, wrong chat id) gives up at once because retrying cannot help. The gate's source
    `telegram` adds its own 20/min bucket and backoff. Only the owner chat, only when
    `TELEGRAM_NOTIFY_ENABLED=1` plus token and chat id; otherwise the daemon idles and builds nothing.
41. **Message policy**: Dutch, facts only, Europe/Amsterdam with UTC in brackets, HTML-escaped chain/X
    text, fixed footer on every message. Observations are batched every 5 minutes (max 6 batches per hour,
    8 items per message, the rest counted); a deferred batch rides the next one, nothing is dropped. The
    first run starts at the current newest decision so history is never dumped on enabling. Fast posts go
    out one per decision. The digest goes out once per day at 06:10 UTC from `out/recall/<day>.json` (or a
    fresh build). Health alerts have a 1 h cooldown per kind; a stream is "silent" only while the ingest
    heartbeat itself is fresh (a dead daemon is systemd's job, not Telegram's).
42. **"Link to the raw data id"** is the decision id (`data: beslissing #<id>`) plus the mint in monospace;
    no external links, so a message never looks like a call to action.
43. **Failed pay-per-use calls cost nothing**: when a call's price depends on its response (X: per post
    returned), a transport error, 429 or 5xx books $0 in the ledger (the estimate stays the gate's check
    before the call). RPC calls keep booking their estimate on failure as in milestone 1.
