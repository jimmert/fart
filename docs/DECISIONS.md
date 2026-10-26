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

## Milestone 3 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 3 repareert wat de uitrol van mijlpaal 2 op de VPS blootlegde en
voegt geen functies toe. (1) De engine schreef niets meer: elke schrijftransactie begon met een gewone
`BEGIN`, las eerst (features van 200 munten, clustering) en schreef daarna; in WAL-modus geeft SQLite dan
meteen `database is locked` zodra een ander proces intussen heeft gecommit, zonder de busy-timeout te
gebruiken. Nu begint elke schrijftransactie met `BEGIN IMMEDIATE`, blijft kort (rekenwerk en netwerk
erbuiten), wordt bij een lock maximaal drie keer met jitter herhaald, de busy-timeout is 15 s, en een
tick of handler die tóch faalt wordt geteld (`engine.tick_failed`, `ingest.handle_failed.<bron>` in
`fart status` en het dagrapport); een mislukte ruwe regel is herverwerkbaar met `fart ingest replay`.
(2) De CU-vloed van de trade-stroom: PumpPortal-migratieberichten droegen de USDC- en PUMP-mint, die als
"graduatie" op de watchlist belandden (72.770 + 8.142 notificaties in 6 minuten, 2.729 valse rijen in
`trades`), en één hete pool gaf 79.604 notificaties in 2 uur. Nu wordt een migratie alleen aanvaard voor
een bekende pump-coin en nooit voor een quote-/systeemmint (configureerbare lijst), geweigerde berichten
blijven bewaard in `rejected_messages`, pump-amm-trades krijgen hun mint uitsluitend via pool → base-mint,
de valse rijen worden door schemamigratie v3 opgeruimd, en `rpc_trades` abonneert alleen nog vóór
graduatie (probation, early_buyers, meta_burst) met een plafond van 2.000 notificaties per abonnement per
uur en een uurenvelop van 1/24 van het dagplafond voor de hele stroom; gegradueerde munten meet de tracker
via DexScreener. (3) De backup-unit stierf aan `fchown` in `gzip -f`; het script comprimeert nu via
`gzip -n -c`, test het archief en ruimt op. (4) Het dagrapport noemt uren waarin fart niet draaide
"niet gedekt" (kop **NIET GEDEKT**, percentages leeg), rekent de dekking altijd live uit, en toont de
verrijkingslimiet (nu CU-gebaseerd, migraties vóór creates) en de fouten van de dag. (5) De
Global-layout is tegen de officiële IDL geverifieerd: creator-fee 5 bps, dus 95 + 5 = 100 bps
(`config/pumpfun.json`), en `fart events classify` benoemt de resterende discriminators met hun programma
(onder meer `InitBoostEvent` van pump-amm). Deploy: `deploy/DEPLOY.md`, sectie "Upgrade to milestone 3".

44. **Branch and PR**: all work on the harness branch `claude/lucid-cori-l2e105`; the PR targets `main`.
45. **`BEGIN IMMEDIATE` everywhere** (`fart/db.py`): `immediate()` / `write_tx()` are the only way to
    write; `read_tx()` is a closed, read-only snapshot for the heavy reads. A plain `BEGIN` no longer
    occurs outside `db.py`. `write_tx` retries SQLITE_BUSY/LOCKED up to 3 times with 50 ms to 1 s of
    jitter and propagates everything else at once; the function it runs must be database-only because
    it may run more than once. `busy_timeout` 15 s on every connection (read-only ones too).
46. **Engine tick in six steps** (expire, features in a read snapshot, promotions, RPC enrichment, metas
    in a read snapshot, decisions + heartbeat): the two read phases never write, the three write phases
    are milliseconds. A failed tick is counted in kv (`engine.tick_failed`, `.<day>`, `.last`) and the
    loop continues; it is never retried as a whole.
47. **Failure counters have a per-day twin** (`<key>.<YYYY-MM-DD>`) so the daily report can show the
    day's failures; the all-time key is what `fart status` prints. The same shape is used for
    `ingest.handle_failed.<source>`, `rpc_trades.subscription_capped`, `rpc_trades.envelope_paused`
    and `enrich.skipped_budget`.
48. **Raw replay** (`fart ingest replay --source S --since T`): the handler runs in `replay` mode
    (data writes only: no raw copy, no CU booking, no counters, no kv timestamps); `ws_subscriptions.sub_id`
    (schema v3) maps a raw rpc_trades notification back to its mint. Idempotent by construction (upserts
    and the `(signature, mint)` primary key of `trades`).
49. **System mints** (`config/pumpfun.json` -> `system_mints`: USDC, USDT, WSOL, PUMP) are never a coin,
    a migration or a watchlist entry, from any source. A PumpPortal migration also needs a known coin
    (`coins`); an RPC-decoded migration of an unknown coin is stored (the universe must stay complete,
    `grads_without_create` shows it) but never watched. Refused messages land in `rejected_messages`
    with the raw JSON (bounded), the reason and the raw file reference, counted per reason.
50. **pump-amm swaps resolve their mint through the pool only**: a CreatePoolEvent in the same
    transaction (quote mint must be WSOL, else `quote_not_wsol`) or `migrations.pool_address`
    (`pool_unknown` otherwise). The milestone-2 `pool_mismatch` check (compare with the subscribed pool)
    is gone with the pool subscriptions.
51. **Schema v3** is idempotent like v2 and additionally deletes the rows the USDC/PUMP "migrations"
    caused (`trades`, `migrations`, `mcap_samples`, `coins`; `watchlist` and `ws_subscriptions` rows are
    closed) and records the counts in kv `schema.v3.cleanup` (printed by `install.sh`). On the VPS the
    analysis of 26 Oct 2026 counted 2,729 bogus `trades` rows (mint USDC/PUMP, source `pump_amm`); the
    installer prints the number it actually removed. `decisions`/`outcomes` rows that mention those
    mints stay (append-only); they are invisible to the report because the universe is `migrations`.
52. **The trade stream is pre-graduation only** (`config/ingest.json` -> `rpc_trades.reasons`:
    probation, early_buyers, meta_burst; a migrated coin is dropped at the next sync; `subscribe_graduations`
    is false). Graduated coins are measured by the tracker's 5-minute DexScreener samples; the optional
    `getMultipleAccounts` pool-reserve sampling from the brief was not built (DexScreener is free and
    already there, and nothing in the measurement needs on-chain reserves).
53. **Two hard limits on the stream**: 2,000 notifications per subscription per UTC hour (then the
    subscription is dropped for the rest of that hour and counted) and an hourly envelope for the whole
    stream of 1/24 of the daily CU cap (its own ledger methods `ws_trade_notification`/`_subscribe`/
    `_unsubscribe`, including the unflushed pending counts); above it every subscription is dropped until
    the next hour. The milestone-2 rules (cap 150, probation dropped above 80% of the daily cap, closed at
    the cap) stay as the outer guard. Expect the envelope to pause the stream for part of each hour as
    long as a notification is estimated at 10 CU (12,500 notifications per hour at the 3M cap); whether
    Alchemy bills them at all is the open item in `docs/UNVERIFIED.md`.
54. **`rpc_trades.enabled` stays true** in the repo because (a) and (b) ship with tests in the same
    release and the envelope bounds the damage to 1/24 of the daily cap per hour; the VPS override that
    disabled the stream is overwritten by the rsync (documented in DEPLOY.md with the way to keep it off).
55. **Backup compression** never lets gzip own the output: `gzip -n -c > file.gz.tmp`, `mv`, `gzip -t`,
    then remove the plain copy and run the retention. The syscall filter of the unit is unchanged.
56. **Covered versus complete** (`fart/ingest/coverage.py`): a source counts from its first run
    (`ingest.<source>.first_started`, else its earliest gap row; the old `started` key is overwritten at
    every restart and was the cause of the "complete" day before the first run); a period without a
    running process becomes a closed `down` gap at the next start (from the previous heartbeat, when it is
    older than 60 s and no shutdown gap is open); an hour is covered when a create source was active
    during part of it, complete only when a source was active for the whole hour without an unrecovered
    gap. The report recomputes the hours live and carries `coverage_status` (complete / incomplete /
    not_covered); NIET GEDEKT blanks every percentage.
57. **Enrich queue**: migrations before creates, oldest first; the daily limit is
    `max_cu_fraction_per_day` (15%) of the daily CU cap divided by the getTransaction estimate (7,627 at
    3M CU and 59 CU) instead of a fixed 1,500; without a cap (public RPC) the fixed number remains.
    Used/limit/skipped per day live in kv and in the report's "Verrijking en fouten" section.
58. **Event layouts follow the official IDLs** (26 Oct 2026, `idl/pump.json`, `idl/pump_amm.json`):
    the trailing fields were appended (the decoder reads what is there, so an older payload decodes as
    `partial` and a current one as `full` with zero extra bytes), every IDL event name is known (so
    `fart events classify` labels `InitBoostEvent`, `MigrateBondingCurveCreatorEvent`,
    `DistributeCreatorFeesEvent` with their program), and the Global account layout past offset 113 is
    verified: `creator_fee_basis_points` at offset 154 is the 5 the VPS read. Fees in config: 95 + 5 = 100.
    The synthetic fixtures were left at the milestone-2 field counts on purpose (they are what an older
    program version emitted).

## Milestone 4 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 4 repareert wat de uitrol van mijlpaal 3 op de VPS liet zien
(10:33 tot 11:25 UTC) en voegt geen functies toe. (1) De trade-stroom wisselde zijn abonnementen elke
engine-tick (5.298 abonnementen voor 238 munten in 52 minuten, mediane levensduur 15 s, 86 trades): de
watchlist verdrong rijen van gelijke prioriteit en zette `since` opnieuw, en de engine bood per tick
circa 840 meta_burst-kandidaten aan voor 200 plekken. Nu verdringt `watch.add()` alleen strikt lagere
prioriteit, wordt `since` nooit meer gereset (een heractivering houdt de oorspronkelijke volgorde; de
idle-verloop telt vanaf `activated_at`), zijn 100 van de 200 plekken gereserveerd voor probation (de
nieuwe munten die de stroom juist moet meten; ze kregen 17 van de 5.298 abonnementen), en kiest de engine
per burstende meta de 5 jongste niet-gegradueerde munten, hoogstens 40 in totaal, bevroren zolang de meta
burst en pas 10 minuten na het einde van de burst vrijgegeven (bewaard in kv, dus ook na een herstart
dezelfde set). De stroom zelf heeft hysterese: aanmelden pas na twee opeenvolgende syncs die de munt
willen, afmelden pas na 60 s niet-gewenst, en de logregel `rpc_trades.subscriptions` komt alleen bij een
wijziging of eens per minuut. De reproductie (cap 10, 30 kandidaten, drie ticks) geeft nul
af-/aanmeldingen. (2) De uurenvelop van 1/24 liet de stroom het hele dagplafond opmaken; nu heeft de
stroom een dagaandeel `daily_cu_share` 0,35 (uurenvelop 0,35/24 = 1,46%, de 0,015 van de VPS-override),
zodat rpc_logs, verrijking en engine altijd 65% houden. De kostentabel volgt de gepubliceerde
Alchemy-cijfers (getTransaction 40, getSignaturesForAddress 40, getTokenLargestAccounts 20, getAccountInfo
10) en websocket-frames worden per byte geboekt (`cu_per_byte` 0,0002; het alternatief 0,04 uit de
subscription-sectie staat als commentaar) met de bytes in het grootboek, apart zichtbaar in `fart
status`; de ijkprocedure tegen het Alchemy-dashboard staat in UNVERIFIED en niets heet bevestigd zonder
dashboardcijfers. `fart status` toont alleen nog open abonnementen, met een aparte regel voor wat vandaag
sloot en waarom (`close_reason`). (3) Een PumpPortal-migratie van een munt van vóór de start van fart
(`Cv934yS1kxVtW3Y6vS8jhk6APKpdtWbzpMcs6VHHpump`) wordt bewaard met `coin_unknown=1`, nooit gevolgd, en
telt mee onder `grads_without_create`; `rejected_messages` houdt alleen echte afwijzingen. (4)
Schemamigratie v4 sluit de 35 `graduation`-rijen uit mijlpaal 2 (die stroom bestaat zonder betaalde
PumpPortal-sleutel niet) en `rpc_trades.enabled` staat weer op `true` in de repo; de VPS-overrides
vervallen door de rsync. Deploy: `deploy/DEPLOY.md`, sectie "Upgrade to milestone 4"; na de upgrade
`RPC_DAILY_BUDGET_CU` terug naar `3000000`.

59. **Branch and PR**: all work on the harness branch `claude/zealous-pascal-yvm41x`; the PR targets `main`.
60. **Strict eviction and an immutable `since`** (`fart/ingest/watch.py`): `add()` evicts only an entry of
    strictly lower priority (the milestone-1 rule let equal priority through, which is what made every
    tick's last candidates displace the previous tick's), a repeated add of an active mint only extends its
    expiry, a reactivation keeps the original `since` and sets `activated_at` (schema v4). Every ordering
    by `since` (eviction order, `rpc_trades.desired()`'s cap, `active_mints()`) is therefore stable across
    ticks and restarts. A probation overflow beyond the free slots is refused (never evicts an older
    probation entry): the reserve of 100 covers about 20 creates per minute over the 5-minute window,
    the VPS saw 8.
61. **Probation reserve** (`config/engine.json` → `watchlist.probation_reserve` 100 of `cap` 200): the
    non-probation reasons share `cap - reserve` slots; probation may also use free shared slots, and such
    an overflow slot is the first thing a higher priority reclaims. An active row that is upgraded in place
    (probation → early_buyers → meta_burst) is never re-checked against the caps, so a promotion always
    succeeds; the next non-probation add then needs a victim.
62. **Stable, bounded meta_burst choice** (`decide.meta_burst_sets`, kv `engine.meta_burst_sets`):
    `meta_burst_per_meta` 5 youngest (by event time) not yet graduated coins of every bursting meta,
    strongest metas first, at most `meta_burst_max_total` 40 in all; a set is frozen while its meta bursts
    (a newer coin never displaces a chosen one), kept for `meta_burst_release_s` 600 after the meta stopped
    bursting (a meta flickering around the threshold changes nothing) and then released, which deactivates
    the rows that are still `meta_burst`. A graduated member leaves its set and is not replaced; an emptied
    set is chosen afresh. 40 is deliberately small: the stream has 150 subscriptions, probation needs most
    of them, and a meta is observed through its strongest members, not all of them. Changing the numbers is
    a commit.
63. **Hysteresis in the stream** (`config/ingest.json` → `rpc_trades.unsubscribe_after_s` 60,
    `subscribe_after_syncs` 2): a mint is subscribed only after it was desired in two consecutive 5-second
    syncs and unsubscribed only after 60 s of not being desired, so a watchlist flicker of one engine tick
    (15 s) never reaches the websocket. The counters survive a reconnect (an immediately re-subscribed set)
    but a fresh start waits one sync. The envelope pause still empties the stream, 60 s later.
64. **Close reasons** (`ws_subscriptions.close_reason`, schema v4): unwatched, graduated, capped, cu_policy,
    envelope, stream_cap, address_changed, error, disconnect, shutdown, schema_v4; `desired()` names the
    first filter that removed a subscribed mint. `fart status` lists only open subscriptions under "top
    open subscriptions" and prints "closed today: <reason> N, ..." separately (the milestone-3 line showed
    the morning's closed USDC/PUMP/pool rows as if they were live). The `rpc_trades.subscriptions` log line
    is written on a change and otherwise once per `log_interval_s` 60 (123 lines in 40 minutes before).
65. **Daily CU share instead of 1/24 per hour** (`rpc_trades.daily_cu_share` 0.35; `hourly_cu_fraction`
    is still read as 24 x the fraction for an old config): the hourly envelope is share / 24 of the daily cap
    (43,750 CU at 3,000,000), so the stream can never take more than 35% of a day and the log stream, the
    sweep, the enrichment and the engine lookups always keep 65%. The value is the VPS override of 26 Oct
    2026 (0.015 per hour = 36% per day) rounded; whether the split is right is an UNVERIFIED item for the
    first calibrated day.
66. **Cost table from the published page, websocket frames per byte** (`config/budget.json`):
    getTransaction 40, getSignaturesForAddress 40, getTokenLargestAccounts 20, getAccountInfo 10 as Jim read
    them on Alchemy's compute-unit page on 26 Oct 2026 (getSlot, getBlockTime, getMultipleAccounts not read:
    estimates); the six `ws_*` methods are `byte_priced` at `cu_per_byte` 0.0002 (the page's Solana section)
    with 0.04 (its "Webhooks and Subscription APIs" section, a factor 200) named in the comment as the
    alternative; `cost_of()` of a byte-priced method is 0 and the streams book `len(frame.encode())` x the
    rate, aggregated per minute, bytes kept in `api_ledger.bytes` and shown next to the CU. The flat 10 CU
    per notification of milestones 1-3 was never published and is gone. Nothing is confirmed without the
    dashboard: the procedure (one whole UTC day, dashboard CU per method against our CU and bytes, the
    factor decides `cu_per_byte`) is in `docs/UNVERIFIED.md`. The enrich limit follows the table (11,250
    getTransaction per day at 3M); `cap_reached()` of the streams means "within one CU of the cap" because
    they book after the fact.
67. **The universe stays complete without watching** (`migrations.coin_unknown`, schema v4): a PumpPortal
    migration of a coin not in `coins` is stored with the flag instead of refused (milestone 3 refused it as
    `unknown_coin`; two cases on 26 Oct 2026, coins created before fart started), the RPC stream's
    migrations of missed creates get the same flag, nothing ever puts such a coin on the watchlist, the
    flag clears when the create arrives (`upsert_coin`: sweep, backfill, replay), the coverage counts it
    under `grads_without_create`, the daily report shows the day's count, `fart status` the totals.
    `rejected_messages` keeps only real refusals (system mints, messages without a mint).
68. **Schema v4** is idempotent like v2 and v3: four guarded columns (`watchlist.activated_at`,
    `migrations.coin_unknown`, `ws_subscriptions.close_reason`, `api_ledger.bytes`), the index
    `ws_subscriptions(until, close_reason)`, and a cleanup that closes every active `graduation` watchlist
    row with its open subscription row (`close_reason` `schema_v4`; 35 on the VPS), flags migrations
    without a coin row and fills `activated_at` from `since`; counts in kv `schema.v4.cleanup`, printed by
    `install.sh` and `fart status`. `subscribe_graduations` (false) already guarded both the PumpPortal and
    the RPC path since milestone 3; a test now pins it.
69. **The idle expiry counts from the last activation** (`watch.expire`: `MAX(last_trade_at, activated_at)`
    instead of `since`): a coin the engine re-adds after a stream pause gets a full `dead_after_idle_s`
    (2 h) again instead of being expired on the next tick, so a stable set is never emptied tick by tick;
    the engine simply re-adds (reactivates) what it still wants, at most once per idle window.
70. **`rpc_trades.enabled` is `true` in the repository** and the VPS overrides (`enabled=false`,
    `hourly_cu_fraction=0.015`, `RPC_DAILY_BUDGET_CU=4500000`) end with the upgrade: the rsync replaces the
    config files, the deployer puts the cap back to 3,000,000 (DEPLOY.md, "Upgrade to milestone 4"). The
    expected `fart status` lines after 15 minutes and the dashboard calibration step are written down there.

## Milestone 5 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 5 levert wat de meetweek nodig heeft en bereidt de community-stap
voor zonder hem te zetten. Niets aan de engine-regels, de winnaarsdefinitie of de meetmethode verandert
(`method_version` blijft m1.0; de vingerafdruk van de regelbestanden is gelijk aan die van `150ca73cb354`,
een test bewaakt dat). (1) **Weekrapport** `fart measure week --end <dag> --days 7` (timer dagelijks 06:30
UTC; de maandagrun is de hele kalenderweek), per segment van gelijke regels × meetmethode: universum per
dag (creates per bron, graduaties, `coin_unknown`, winnaars organisch/gebundeld/onbekend, uren NIET GEDEKT,
budgetpauzes), trefkans van munt-observaties (raak binnen 24 u gedeeld door de beslissingen waarvan de 24 u
verstreken zijn; onbekend telt mee in de noemer, nog bezig staat apart), recall (winnaars met munt- óf
thema-beslissing vóór eerste trade en vóór graduatie), lead time (mediaan, p25, p75, bins), blokbootstrap per
dag (9.999 trekkingen, seed 20261026, alpha 0,05; minder dan 3 gedekte dagen of 20 winnaars = "te weinig
data"; een NIET GEDEKT-dag is geen blok en nooit een dag met nul winnaars), kosten per dag (CU-schatting met
de kanttekening dat de ijking met Alchemy open staat; X in dollars) en de go/no-go-toets. (2) **Go/no-go-
criteria** vooraf bevroren in `config/measure.json` → `community_gate` (hieronder in het Nederlands). Het
rapport toont alleen voldaan / niet voldaan / te weinig data; het beslist niets en verstuurt niets.
(3) **Community-kanaal** in `fart-notify`, standaard uit: pas met `TELEGRAM_COMMUNITY_CHAT_ID` én
`TELEGRAM_COMMUNITY_ENABLED=1` gaat er ooit iets naar een andere chat dan die van de eigenaar (de uitgaande
poort weigert elke andere chat, ook een rij die al in de outbox stond); de community-opmaak in gewone taal
(wat gezien is, waarom het opvalt, de bevroren regels, een vaste waarschuwing, de footer, het mint-adres als
tekst, geen link of koopknop); `TELEGRAM_COMMUNITY_PREVIEW=1` stuurt die tekst als extra bericht naar Jim met
de kop "VOORBEELD community-bericht", binnen zijn eigen limieten; de community-regels (1 per munt en per
thema per 24 u, 6 per uur, stille uren 02:00-08:00 Amsterdam) en het wekelijkse verantwoordingsbericht
bestaan al, ook zolang het kanaal uit staat. (4) **Meetfidelity**: het dag- en weekrapport tonen het aandeel
graduaties met minstens elke 10 minuten een sample; een graduatie zonder voldoende samples telt als
"uitkomst onbekend", nooit als "geen winnaar"; de tracker telt wat hij overslaat (`measure.track.skipped`
in `fart status`). (5) **M4-restpunt**: de engine sluit elke tick `meta_burst`-rijen boven 40 die niet in de
bevroren sets zitten (oudste eerst, nooit met een trade in de laatste 10 minuten) en schemamigratie v5 doet
dat eenmalig bij de upgrade; de test met 195 oude rijen plus reserve 100 geeft na één tick 40 `meta_burst`
en probation krijgt plek. Deploy: `deploy/DEPLOY.md`, sectie "Upgrade to milestone 5".

**Go/no-go-criteria voor community-alerts (bevroren op 26 oktober 2026, vóór de meetweek).** Startwaarden
gekozen door de bouwer; Jim kan ze bijstellen vóór het einde van de meetweek, nooit erna. Ze staan in
`config/measure.json` → `community_gate`; het weekrapport toetst ze en drukt per criterium "voldaan",
"niet voldaan" of "te weinig data" af, plus één oordeel (GO / NO-GO / TE WEINIG DATA). Niets in het rapport
beslist of verstuurt iets.

1. Minimaal **5 complete dagen** in het venster: een dag telt als compleet wanneer geen enkel piekuur
   (12:00 tot 02:00 UTC, in het rapport de uren 12 tot en met 23 en 0 en 1 van die UTC-dag) NIET GEDEKT was.
2. Minimaal **30 winnaars** in het universum (gemeten graduaties met piek ≥ $100.000 én volume ≥ $20.000
   binnen 24 u, gedekte dagen).
3. De **ondergrens van het bootstrapinterval van de trefkans** van munt-observaties is **≥ 0,15**. Trefkans =
   raak binnen 24 u gedeeld door de observaties waarvan de 24 u verstreken zijn; een onbekende uitkomst
   telt mee in de noemer (een niet-meetbare observatie is geen aangetoonde treffer), een nog lopende staat
   apart en komt in de noemer zodra haar 24 u voorbij zijn.
4. De **ondergrens van het bootstrapinterval van de recall vóór graduatie** is **≥ 0,25**: het aandeel
   winnaars met een munt- óf thema-beslissing vóór hun graduatie.
5. De **mediane lead time vóór graduatie** (graduatie minus de vroegste munt- of thema-beslissing, over de
   winnaars die een beslissing hadden) is **≥ 10 minuten**.
6. Gemiddeld **hoogstens 12 observaties per gedekte dag** (munt- plus thema-beslissingen).

Bij minder dan 3 gedekte dagen of minder dan 20 winnaars tonen de criteria 3 en 4 "te weinig data" in plaats
van een interval; de intervallen komen uit een blokbootstrap met de gedekte dagen als blokken (9.999
trekkingen, seed 20261026, alpha 0,05). Een regelwijziging tijdens de week (nieuwe vingerafdruk) opent een
nieuw segment in het rapport; de criteria worden per segment getoetst.

71. **Branch and PR**: all work on the harness branch `claude/hopeful-hamilton-6b9ld0`; the PR targets `main`.
72. **No rule change, provably** (`fart/version.py` → `rules_fingerprint()`, `RULE_FILES`): every deploy is a
    new commit and therefore a new `rules_version`, including this one, while the engine rules did not change.
    The fingerprint is a sha256 over the rule-defining files (config/engine.json, config/pumpfun.json,
    config/stopwords.json, fart/chain/curve.py, fart/engine/decide.py, fart/engine/features.py,
    fart/engine/meta.py; orchestration such as the watchlist mechanics is deliberately not part of it). The
    engine records it next to its rules_version in kv (`engine.rules_fingerprint.<rv>`), the frozen value of
    `150ca73cb354` lives in `config/measure.json` → `community_gate.frozen_rules`, and
    `tests/test_m5_week_report.py::test_the_working_tree_still_runs_the_frozen_rules` fails as soon as a rule
    file changes without that entry being updated on purpose. The week report pools rules_versions with the same
    fingerprint into one segment and always lists them; a rules_version without a known fingerprint is a segment
    of its own.
73. **Week report = aggregation of daily reports** (`fart/measure/week.py` over `report.build_report(...,
    method_version=mv)` per day and per method present in `outcomes`): no second implementation of the universe,
    the coverage or the outcome logic. The daily report gained the blocks the week needs (decisions and
    observations per rules_version, the earliest coin and theme decision per rules_version for every winner with
    numeric event times, the peak-hour coverage, the day's budget pauses and estimated costs) and keeps every
    field it had. The week report is always built fresh from the database, never from the daily JSON files (those
    were written at 06:00 with the day's late graduations still `nog bezig`).
74. **Hit-rate denominator**: decisions whose 24 h have elapsed (known + unknown outcome); unknown stays in the
    denominator because an observation we could not measure is not a demonstrated hit, pending is excluded and
    printed next to it (it joins once its 24 h have passed), and the rate over known outcomes is printed as well.
    Counting pending as a miss would have called "not yet known" zero, which the honesty rules forbid; counting
    unknown as a hit would invent successes. The gate uses this conservative statistic.
75. **Recall and lead time use the earliest of coin and theme decision** of the segment's rules: the community
    would receive both kinds, so "did we have it before graduation" counts either; coin-only and theme-only are
    printed next to it. Lead time = event minus that earliest decision; the bins come from
    `config/measure.json` → `report.lead_time_bins_s`.
76. **Block bootstrap with days as blocks** (`week.bootstrap_ratio`): the covered days of the window are
    resampled with replacement 9,999 times with `random.Random(20261026)`, the pooled ratio per draw, percentile
    interval at alpha 0.05; a draw with an empty denominator is undefined and skipped (never zero). A NIET GEDEKT
    day is not a block (its universe is unknown) and never counts as a day with zero winners; `test_not_covered_days_
    are_no_blocks_and_never_count_as_zero_winners` pins both. Fewer than 3 covered days or fewer than 20 winners
    prints "te weinig data" (`week.bootstrap`).
77. **Complete day for the gate** = every peak hour (12:00-02:00 UTC as the hours 12..23 and 0..1 of the same UTC
    day) covered; the 00:00-02:00 part of a calendar day belongs to the window that started the day before, but
    a per-day block needs per-day hours, so the shift of two hours is accepted and documented.
78. **Observations per day** counts coin and theme decisions together (both would become community messages).
79. **Tracker coverage, not a method change** (`report.tracker_coverage`, `config/measure.json` → `tracker.
    max_sample_gap_s` 600): a graduation is covered when its `mcap_samples` rows in [graduation, +24 h] are never
    more than 10 minutes apart, the gaps to the window edges included. A stored `hit_100k = 0` of an uncovered
    graduation is reported as "uitkomst onbekend" with reason `sparse_samples` (a peak between sparse samples may
    have been missed); a winner stays a winner (sparse samples cannot invent a peak); `hit_100k = NULL` stays
    unknown. The `outcomes` rows and `method_version` m1.0 are untouched: the report layer reclassifies, as the
    brief asked; the rule applies to the universe (graduations), decisions keep their `data_quality` in the
    outcome json. The daily report test moved its synthetic "no winner" coin to dense samples so the known answer
    stays a known miss.
80. **`measure.track.skipped`** (`Tracker.run`): mints beyond `max_mints_per_run` and mints whose DexScreener batch
    was refused by the gate (the rest of the run) or failed are counted per run in kv (all time, per day, the
    last reasons); `fart status` prints the line; the daily report shows the day's count. Nothing else in the
    tracker changed.
81. **Community channel off by default, enforced at the gate** (`fart/notify/service.py`): `Notifier.allowed_chats`
    is the owner chat plus the community chat only while `settings.community_enabled` (chat id set, flag 1, owner
    channel enabled, chat id not the owner's). `_enqueue()` refuses every other chat before the outbox and the
    sender marks a pending row for a chat that is not allowed `gave_up` without calling the API, so switching the
    channel off also stops what was already queued. `tests/test_m5_community.py` proves that with either name
    missing nothing reaches another chat, and that a forged row is never sent.
82. **Community format** (`format.community_message`): what was seen (coin, theme when the coin's name or ticker
    carries a currently observed theme term, buyers in 5 minutes, net SOL inflow, market cap, age; for a theme:
    coins, makers, inflow, buyers, burst), why it stands out (the decision's own numbers in words), the rules (the
    decision's `rules_version` plus the thresholds of config/engine.json in plain Dutch), the fixed warning line
    (`notify.community.warning`: no advice, high risk, everything measured and published afterwards), the footer.
    The mint address as `<code>` text, no link, no button. No tier or advice words.
83. **Preview** (`TELEGRAM_COMMUNITY_PREVIEW=1`): the same text with the header "VOORBEELD community-bericht" as
    kind `community_preview` to the owner through the same outbox; previews count towards
    `observe.max_batches_per_hour` (kinds observe + community_preview) and share the sender's 20 per minute; the
    community rules apply to previews exactly as to real messages, so what Jim sees is what the community would
    have received, no more. The weekly accountability message has a preview twin (`community_weekly_preview`).
84. **Community rules** (`config/notify.json` → `community`): one message per coin and per theme per 24 h (kv
    `notify.community.key.<coin|theme>:<key>`), at most 6 distinct decisions per hour (real and preview of one
    decision count once), nothing during `quiet_hours` (02:00-08:00 Europe/Amsterdam by default; start == end
    disables; may wrap midnight), skipped decisions are counted (`community_skipped_*` in the tick summary) and
    never sent late. The weekly accountability message (Monday 06:45 UTC, after the 06:30 week report) summarizes
    the go/no-go section in plain words, says how many community messages went out that week or that the channel
    is off, and carries the warning line; it exists and is tested while the channel is off.
85. **Watchlist trim** (`watch.trim`, engine step 1, schema v5): `meta_burst` rows above `meta_burst_max_total`
    that are not in the frozen sets are closed oldest-first, never a row that traded in the last
    `TRIM_RECENT_TRADE_S` 600 s (a module constant, not a config key: config/engine.json is a rule file and stays
    byte-identical). The migration reads the frozen sets from kv `engine.meta_burst_sets`, uses the bound 40
    (`SCHEMA_V5_META_BURST_MAX_TOTAL`, pinned to the config by a test) and records the counts in kv
    `schema.v5.cleanup`; the trade stream unsubscribes the closed mints itself. The watchlist mechanics are not
    part of the rules fingerprint on purpose (item 72): this is a repair of the M4 intent, not a change of what
    becomes an observation.
86. **Timer** `fart-measure-week.timer` runs daily at 06:30 UTC (after the 06:00 daily report); the Monday run is
    the Monday-Sunday calendar week and the JSON says so (`calendar_week`). One timer instead of two: the same
    command, the same file name `week-<end>`.

## Milestone 6 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 6 maakt de X-laag veilig in gebruik te nemen zonder de meetweek te raken,
maakt zichtbaar waarom de munt-tak weinig observaties geeft, en sluit twee operationele gaten. Geen regelbestand
verandert: de vingerafdruk blijft `sha256:e3bf4320c45c` (dezelfde test bewaakt dat), dus het weekrapport blijft
deze commit poolen met `150ca73cb354`. (1) **X in schaduwstand.** Nieuwe omgevingsvariabele `X_MODE`
(`off` zonder sleutel, `shadow` zodra `X_BEARER_TOKEN` gezet is, `live`). In `shadow` draait `fart-social`
volledig binnen het bestaande budget (mega-watch elke 60 s, KOL-watch elke 180 s, verversing, bevestigings-
tellingen, snelle-postdetectie), maar niets ervan bereikt de beslissingen: een snelle post wordt een rij in de
nieuwe append-only tabel `shadow_decisions` (dezelfde kolommen als `decisions` plus `would_kind`), een
bevestigingstelling een rij in `shadow_confirmations` (gekoppeld aan de observatie die ze bevestigd zou hebben,
met de drempel van dat moment en of het laatste uur / de laatste 24 uur die haalde), en de engine krijgt geen
mega-post-feature en neutrale X-velden, wat dan ook in de sociale tabellen staat. Een test vergelijkt een
schaduwdag met een dag zonder X: de `decisions`-rijen zijn byte voor byte gelijk; zonder sleutel blijft alles
zoals het was. `live` is de M2-werking en kan alleen samen met `"x_live": true` in `config/engine.json`, een
regelwijziging met een nieuwe vingerafdruk (nieuw segment in het weekrapport): de overgang shadow → live gebeurt
pas ná de meetweek; een `X_MODE=live` zonder die sleutel draait als shadow en zegt dat in `fart status`. Dag- en
weekrapport krijgen een sectie "X-schaduw": hoeveel beslissingen X zou hebben toegevoegd, hoeveel bestaande
observaties X binnen 1 u / 24 u bevestigde (telling ≥ `search_when_count_1h`), en de uitkomst van de
schaduwbeslissingen (winnaar / geen winnaar / onbekend) met dezelfde noemerregels als de echte trefkans.
(2) **Droogloop zonder HTTP**: `fart social dryrun [--hours 24]` rekent uit `config/x.json`, de huidige
beslissingen en de watchlist uit welke aanroepen per soort zouden volgen, wat dat per dag kost volgens `prices`,
en loopt de komende uren door de uurenvelop, de 30%-reserve en het KOL-plafond heen; het toont de uren (en de
piekuren) waarin aanroepen geweigerd zouden worden, tegenover `X_DAILY_BUDGET_USD`. De prijzen zelf zijn niet
geverifieerd; `docs/UNVERIFIED.md` zegt hoe Jim ze tegen de X-developer-console controleert. (3)
**Beslisdiagnostiek, alleen lezen**: de engine schrijft per UTC-dag één rij per gevolgde munt en per gerangschikt
thema met de hoogste drempel-features die hij zag (`engine_candidates`); `fart engine explain --day <dag>` en het
dagrapport ("Waarom wel of geen observaties") tonen per drempel uit `config/engine.json` hoeveel kandidaten
eraan voldeden, de verdeling (p50, p90, max) per feature, het aandeel probation-munten dat überhaupt trades had
(per uur, naast de stroompauzes van die dag) en hetzelfde voor thema's. Het commando verandert niets, de
drempels blijven wat ze zijn, en er is geen advies over nieuwe drempels. (4) **Backupcontrole en
hersteloefening**: `fart backup verify` pakt het nieuwste archief uit onder `data/`, draait `quick_check`,
vergelijkt de rijaantallen van `coins`, `migrations`, `decisions`, `outcomes` met de live database (live ≥
archief) en ruimt op; `deploy/backup.sh` roept het na elke backup aan en een mislukte controle maakt de unit
`failed`; DEPLOY.md beschrijft de handmatige offsite-kopie (`scp` naar een tweede locatie) en de
hersteloefening stap voor stap. (5) **Kostenijking**: `fart budget calibrate --day <dag> --dashboard-cu <N>`
leest het grootboek van die dag, berekent de impliciete CU per byte van de websocketmethoden (dashboardtotaal
minus de HTTP-methoden tegen de gepubliceerde tarieven, gedeeld door de bytes) en drukt de waarde van
`config/budget.json` → `rpc.cu_per_byte` af die daarbij hoort en wat dat per dag betekent; het schrijft niets.
(6) **Uitrol**: `deploy/DEPLOY.md`, sectie "Upgrade to milestone 6": `X_MODE`, schema v6, de verwachte regels in
`fart status` en de rooktests `fart social dryrun`, `fart engine explain --day 2026-10-27`, `fart backup verify`.

87. **Branch and PR**: all work on the harness branch `claude/youthful-darwin-y08qkv`; the PR targets `main`.
88. **`X_MODE` is resolved in `Settings.x_mode`** (`fart/config.py`): `off` without a token or with `X_MODE=off`;
    `shadow` with a token and `X_MODE` unset or `shadow`; `live` only with `X_MODE=live` AND `"x_live": true` in
    `config/engine.json` (`Settings.x_live_allowed`). An unknown value or a refused `live` falls back and explains
    itself (`x_mode_note`, printed by `fart status` and logged by both daemons). The social daemon and the engine
    each write the mode they run into kv (`social.x_mode`, `engine.x_mode`) so `fart status` shows whether both
    read the same `.env`.
89. **Why `live` is gated by a rules key and not by `.env` alone**: X data changes which rows become decisions
    (the mega-post feature, the `fast_post` rows), so switching it on is a change of the rules of the measurement
    week. The fingerprint is a hash of the rule files; the only way to make the week report open a new segment is a
    change in one of them. `config/engine.json` is the file that says what becomes an observation, so the switch
    lives there (absent = not allowed; the key cannot be added now without changing the frozen fingerprint). After
    the week the commit that adds `"x_live": true` also updates `community_gate.frozen_rules` on purpose, as the
    existing test demands.
90. **The engine guarantee, not the social daemon's discipline, is what protects the week** (`fart/engine/service.py`):
    in `off` and `shadow` the engine never calls `features.mega_post_feature()` and overwrites the X fields of
    every coin's features with `X_NEUTRAL`, the values `features.x_features()` returns on empty tables (a test pins
    the two to each other). So even a `ca` post the shadow search stored, or a `social_counts` row from an earlier
    live run, cannot reach a decision. The rule files (`features.py`, `decide.py`) are untouched; the neutralization
    is orchestration. `tests/test_m6_x_shadow.py::test_shadow_mode_leaves_decisions_byte_for_byte_as_without_x`
    builds the same world twice and compares the `decisions` rows.
91. **Shadow tables** (`fart/social/shadow.py`, schema v6): `shadow_decisions` has the columns of `decisions` plus
    `would_kind` (`fast_post`); `kind` is always `shadow` so a reader never mistakes the row for a decision. The
    fast-post dedupe looks at `shadow_decisions` in shadow mode and at `decisions` in live mode (a post judged in
    shadow is judged again if live is ever switched on; that is the point of a new segment). `shadow_confirmations`
    keeps every count with the decision it belongs to, the threshold of the moment and the two confirmation flags, so
    the report never depends on a later config value. In shadow mode `social_counts` is not written; the `ca`
    search still runs (same calls, same money) and its posts are stored as data (`social_posts`, kind `ca`), which
    the engine neutralizes (item 90). Both tables are append-only (triggers) and never pruned.
92. **Shadow outcome rule** (`report.shadow_decision_outcome`): a shadow `fast_post` decision has no mint of its own,
    only the coins its rare words matched. Its outcome rests on the graduation outcomes the tracker already measures:
    winner when one matched coin graduated within 24 h of the decision and that graduation's 24 h outcome is a winner;
    not a winner when every matched coin either did not graduate within those 24 h (a winner is a graduation by
    definition, docs/METING.md) or was measured "no winner" with tracker coverage; unknown otherwise, and unknown stays
    in the denominator exactly as in the hit rate (item 74); pending while the 24 h of the decision or of such a
    graduation have not elapsed. No `outcomes` row is written for a shadow decision (`method_version` m1.0 stays).
93. **`engine_candidates` instead of per-tick `features_asof` rows** (`fart/engine/candidates.py`): `features_asof`
    is a snapshot per decision (item 7); a row per watched coin per 15 s tick would be over a million rows a day. One
    UPSERT per coin and per ranked meta per tick keeps, per UTC day, the highest value of every threshold feature,
    the lowest and highest largest-buyer share, the last and the "ever" value of `bundled`, the highest
    `trades_total` (data availability), whether both momentum thresholds were met in one tick and whether the whole
    verdict passed in one tick, plus the watchlist reason at first sight. The rows are written in the engine's
    decision transaction after the verdicts and are never read by the engine; retention prunes them after
    `candidates_days` (30, config/ingest.json). `fart engine explain` opens the database read-only.
94. **No advice in the explain output**: the command and the report section show counts per threshold and the
    distribution (nearest-rank p50, p90, max) and say so in a fixed line; a change of thresholds, if any, is a
    decision after the week with a new rules_version.
95. **Dry-run model** (`fart/social/dryrun.py`): requests per day from the intervals, billed units from the posting
    volume (observed in `social_posts` over the last 7 days when the daemon ran, else the `dryrun` assumptions of
    config/x.json: 80 mega posts/day, 200 KOL posts/day, 10% of the counts triggering a search), prices from
    `prices`, the day's volume arriving uniformly (`--arrival envelope` follows the envelope weights instead). The
    hour walk applies the same four refusal rules as `XBudget.allow` in the same order; a refused mega/KOL search is
    carried to the next hour (since_id), a refused count is lost. `ca_search` is an upper bound (every post and
    author new); `refresh` is assumed free, as the daemon assumes. The command creates the database when none exists
    (an empty one) and never opens a network connection (a test replaces `urlopen` with an assertion).
96. **Backup verify under `data/`** (`fart/backup.py`): the temporary directory is `data/backup/.verify-<pid>/`
    (inside the unit's `ReadWritePaths`, outside `PrivateTmp`), removed in a `finally`; the archive copy is opened
    read-only (`mode=ro`) so no `-wal` file is created; a table missing in both databases is `absent`, not a
    failure (the M3 test's bare database); missing only in the archive is a failure. The result is kept in kv
    `backup.verify.last` (best effort) so `fart status` has a `backup:` line; the unit fails on exit 1 by the
    script's own check, after the archive was written, so a failed verify never loses the backup itself.
97. **Calibration arithmetic** (`fart/net/calibrate.py`): HTTP methods are valued at the published rates of the
    cost table (a method outside the table at `cost_default`, marked), `ws_connect` included as a flat HTTP-side
    estimate; the websocket bytes are the six byte-priced methods; the suggestion is the documented candidate (0,
    0.0002, 0.04) within 15% of the implied factor, else the factor rounded to two significant digits. The command
    prints the day's CU at the implied factor against the cap and whether the trade stream's bytes still fit its
    daily share. It writes nothing; `config/budget.json` is not a rule file, so the commit keeps the fingerprint.
98. **`fart status` lines added**: the X mode with its requested value and note, the shadow counters (totals and
    today, confirmations with the two flags, the modes the daemons report), today's `engine_candidates` rows, and the
    last backup verification. The reports add "X-schaduw" (day and week) and "Waarom wel of geen observaties" (day).

