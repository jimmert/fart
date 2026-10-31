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

## Milestone 7 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 7 maakt van de X-bevestigingslaag een kostenbewuste planner die binnen jouw
dagplafond van $10 tot $20 de meeste bevestigende informatie oplevert en niets uitgeeft wat geen informatie toevoegt.
Geen regelbestand verandert: de vingerafdruk blijft `sha256:e3bf4320c45c` (dezelfde test bewaakt dat), `config/x.json`
hoort niet bij de regelbestanden, dus het weekrapport blijft deze commit poolen met `150ca73cb354`. (1) **Sleutels en
volgorde.** Bevestigingssleutels komen alleen nog van observaties: per `observe`-beslissing het mint-adres, de `$TICKER`
(≥ 3 tekens, niet generiek) en de exacte naam van ≥ 2 woorden; van thema's alleen de 5 sterkste (`observe_meta`,
gerangschikt op SOL-instroom en kopers; term ≥ 8 tekens, niet generiek). De volgorde is hard: jongste munt-observatie
eerst, dan de oudere munten, dan de thema's; hoogstens 20 sleutels tegelijk (een jongere munt verdringt de laagste
sleutel, reden `displaced`). Een sleutel verlaat de planning zodra hij beslist is, en de nieuwe tabel `x_keys` zegt
waarom: `confirmed` (telling ≥ 10 in het laatste uur, daarna precies één zoekopdracht), `refuted` (drie opeenvolgende
tellingen zonder groei van de 24-uurstelling: na 45 minuten en $0,02 bij een stille munt), `expired` (ouder dan 6 uur),
`graduated`, `dead` (van de watchlist), `displaced`, `exhausted`. Elke sleutel wordt hoogstens één keer per 6 uur
gepland. (2) **Adaptief schema.** Tellingen op 0, 15, 30, 45, 60, 75, 90, 105 en 120 minuten na de beslissing en
daarna elk uur (180, 240, 300): hoogstens 12 tellingen en 1 zoekopdracht per sleutel; de zoekopdracht alleen bij een
piek, hoogstens 10 posts, en de auteursopvraag (`GET /2/users`) voor hoogstens 3 auteurs (de meest gelezen posts
eerst; auteurs van vandaag worden niet opnieuw betaald). Mega-watch elke 60 s in de piekuren (13:00 tot 02:00 UTC) en
120 s daarbuiten, KOL-watch 180 s / 300 s; de verversing blijft 120 s. De droogloop rekent de volle belasting voor: 60
sleutels per dag × (12 × $0,005 + 10 × $0,005 + 3 × $0,010) = 60 × $0,14 = **$8,40 per dag als plafond** voor de
bevestigingslaag (een sleutel kan niet tegelijk al zijn tellingen én een zoekopdracht hebben: een bevestigde stopt met
tellen, een weerlegde zoekt nooit, dus de echte dag blijft eronder; met de helft bevestigd na gemiddeld vier tellingen
circa $4,80), plus $0,49 mega en $1,00 KOL = $9,89 = 66% van $15. De formule staat in `docs/UNVERIFIED.md`. In M6 kostte
dezelfde laag circa $119 per dag (61 sleutels × 96 tellingen, een zoekopdracht met auteursuitbreiding bij 10% van de
tellingen). (3) **Planner met plafond en reserve.** `XBudget` beoordeelt elke aanroep op twee bedragen: het plafondbedrag
(wat hij hoogstens kan kosten) en het verwachte bedrag (het gemiddelde van vandaag). De harde regels (dagplafond,
30%-reserve voor mega + KOL, KOL-plafond $1) toetsen het plafondbedrag, zodat het dagplafond nooit overschreden kan
worden, en weigeren voor de rest van de dag (geboekt in het grootboek, zoals eerder). De uurenvelop toetst het verwachte
bedrag en **stelt uit** in plaats van weigert: de aanroep blijft in het plan (de sleutel blijft due, `since_id` haalt de
posts later op), het uitstel wordt geteld (kv) en eens per uur gelogd. Binnen een ronde gaat de zoekopdracht van een
bevestigde sleutel voor (de meeste informatie per dollar), dan de tellingen (jongste munt eerst), dan de account-watches;
een uitgestelde zoekopdracht houdt de goedkopere tellingen niet op. De standaardinstellingen zijn afgestemd op
`X_DAILY_BUDGET_USD=15.00` (de deployer zet dat in `.env`); de code valt zonder die regel terug op $3,00, waaronder de
volle belasting niet past, en de droogloop zegt dat. `fart social dryrun` en `fart status` tonen per soort het geplande
en het werkelijke bedrag en het aandeel van het plafond. (4) **Kwaliteit per dollar, meetbaar in schaduw.** De sectie
"X-schaduw" van dag- en weekrapport krijgt: de kosten per bevestigde observatie, het aandeel observaties dat binnen 1 uur
na de beslissing bevestigd werd, de tellingen ná de eerste bevestiging (verspilling), de gestopte sleutels per reden met
wat de weerlegde kostten, en de uitkomst (winnaar / geen winnaar / onbekend) van bevestigde tegenover niet-bevestigde en
niet-getelde munt-observaties met dezelfde noemerregels als de trefkans. Het rapport geeft geen advies; dit zijn de
cijfers waarop jij na de meetweek beslist of X live gaat. (5) **Werkelijke prijzen.** `fart social calibrate --day
<dag> --console-usd <N>` zet het bedrag uit de X-developer-console naast de `x_spend`-rijen per soort en eenheid (post,
telling, auteur; schema v7 bewaart de eenheid per rij), drukt de prijs per eenheid af die daarbij hoort (uniforme factor,
of één component als alleen die fout is) en de `prices`-regels die een commit vereist; binnen 5% staan de prijzen vast.
Zodra er echte aanroepen zijn (schaduwstand met sleutel) gebruikt de droogloop de waargenomen posts en auteurs per
zoekopdracht, het bevestigde aandeel en de tellingen per sleutel (vanaf 5 gestopte sleutels en 3 zoekopdrachten) en
meldt dat ("observed rates"). (6) **Uitrol**: `deploy/DEPLOY.md`, sectie "Upgrade to milestone 7": `X_DAILY_BUDGET_USD=15.00`,
`X_MODE` leeg, schema v7, de droogloop als controle vóór de sleutel, de verwachte regels in `fart status`, de ijkstap met de
console.

99. **Branch and PR**: all work on the harness branch `claude/peaceful-fermi-9uh7nu`; the PR targets `main`.
100. **Keys only from observations, in a hard order** (`fart/social/planner.py` → `candidates`): coin keys from the
    latest `observe` decision per mint of the last `confirm.window_s` whose mint is on the active watchlist and has not
    graduated, youngest first; the query is the milestone-2 one (mint, `$TICKER` of >= 3 characters that is not generic
    and not a stopword, the exact name of >= 2 words). Theme keys only for the `max_meta_keys` (5) strongest
    `observe_meta` decisions of the window (the decision's own `sol_inflow`, then `unique_buyers`, the engine's own
    ranking order) whose term is >= `min_meta_term_len` (8) characters and not generic; the milestone-2 allowance for a
    short bigram is gone. A theme that already had its episode still counts among the five, so a weaker theme is never
    promoted in its place. Coins rank above every theme (`prio_class` 0 and 1), within a class by age or strength
    (`prio_value`). `confirm.interval_s` and `max_keys_per_loop` are gone: the schedule is per key.
101. **One episode per key per window** (`x_keys`, primary key (key, decision_id)): a candidate is admitted only when no
    row of that key has a `decision_as_of` within the window before the candidate's decision, whatever its state; a later
    decision of the same mint reuses the running episode and a stopped key is not re-opened by a decision inside the
    window (a refuted coin that gets another observation half an hour later would otherwise be paid for twice). Once the
    window has passed a new decision opens a new episode (a new row), so the report's accounting is per episode.
102. **Admission and displacement** (`planner.sync`, one short write per round): first the keys the world decided are
    stopped (window → `expired`, or `confirmed` when a confirmation was counted but the search never ran; graduation →
    `graduated`; off the active watchlist → `dead`), then the candidates are admitted in order up to
    `max_active_keys` (20). When the plan is full, a candidate that ranks above the lowest active key displaces it
    (`displaced`; its counts stay in the tables) and a lower one waits. With ~60 keys a day and episodes of minutes to
    hours the plan rarely fills; the rule only says who yields when it does: the youngest observation never waits.
103. **Schedule and stop rules** (`planner.schedule_counts`, `next_due`, `record_count`): the first count as soon as a key
    is admitted, then `fast_interval_s` (900) during `fast_phase_s` (7200) after the decision and `slow_interval_s` (3600)
    after that, never beyond the window, at most `max_counts_per_key` (12): nine in the first two hours and three in the
    four after. A key is `confirmed` when the last-hour count reaches `confirm.search_when_count_1h` (10); exactly one
    search is then pending and no count follows. It is `refuted` after `refute_after_flat_counts` (3) consecutive counts
    without growth of the 24 h count (`n_24h`: the last-hour count drops at every hour boundary because its buckets
    roll, the 24 h count only grows for a coin younger than a day); the first count has no predecessor and never counts
    as flat, so a silent coin costs four counts, 45 minutes, $0.02. A count whose body could not be parsed is paid and
    counted but keeps the previous baseline. `exhausted` at the per-key maximum, `expired` when no further count fits in
    the window. `confirmed_at`, `peak_n_1h`, `flat_streak`, `usd` and `usd_search` are kept on the row.
104. **The confirmation search carries no author expansion** (`XClient._confirm_search`): `search/recent` with
    `max_results` 10 and no `expansions`, then one `GET /2/users?ids=` for at most `search_max_authors` (3) distinct
    authors (`pick_authors`: the most engaged posts first by likes + quotes + replies, newest on a tie, authors already
    expanded today skipped), booked as kind `ca_authors` at `author_expansion_usd` per new user (the per-user price of
    the pricing notes; whether the users endpoint bills the same is an UNVERIFIED item). The mega and KOL searches keep
    `expansions=author_id`: their accounts are few and deduplicated per day. The search's money counts against its key
    (`usd_search`); a lookup the planner defers is dropped for that key (the information lost is three follower
    counts), the key still stops as `confirmed`.
105. **Ceiling and expected cost** (`XBudget.admit`): every call carries the most it can cost at the configured prices
    (search: every post and every account new, plus the author lookups for a confirmation search; count: one request;
    refresh: free) and what it is expected to cost (the average actual cost of today's calls of the kind once there are
    three, bounded by the ceiling; one new post plus one new author for the account watches before that, the ceiling for
    the rest). The hard rules test the ceiling: the cap, the account-watch reserve (confirmation kinds together at most
    70% of the cap) and the KOL sub-cap refuse for the rest of the UTC day, ledgered as blocked as before. The envelope
    tests the expected cost and defers: `XVerdict.deferred` with `retry_at` the next hour, a kv counter
    `x.planner.deferred.<kind>.<day>`, a log line once per hour per kind, nothing in `api_ledger`. The generic gate sees
    the ceiling too (`cost=`), so the cap cannot be exceeded whatever a response contains; the milestone-2 "one call can
    exceed the cap by max_results x prices" is gone. The check order is cap, reserve, KOL cap, envelope: a call that
    cannot run today is not called deferred. A test admits random request streams at caps of $10 to $20 and proves the
    spend never passes the cap, the reserve or the KOL cap, and that deferrals are counted while refusals are ledgered.
106. **Admission order inside a round** (`XClient.confirm`, `planner.due_requests`): the pending search of every
    confirmed key first (it completes a confirmation: the most information per dollar), then the due counts in plan
    order, then (in `tick`) the account watches. A deferred count ends the round (the counts behind it cost the same and
    stay due); a deferred search does not hold the counts behind it. The account watches run every 60 s (mega) / 180 s
    (KOL) in the peak hours and 120 s / 300 s outside (`interval_peak_s` / `interval_off_s`; an old config with a single
    `interval_s` keeps it); their money follows the new posts, not the requests, so the slower cadence saves requests
    and latency budget, not dollars. The refresh stays at 120 s.
107. **Ledger units** (`x_spend.unit`, schema v7): a row counts posts, requests or authors; a search books its posts and
    its new authors in separate rows, so `fart social calibrate` can put the console's amount next to the units per price
    component (post, request, author, refresh). Rows from before milestone 7 have no unit (`mixed`): they count in the
    totals, not in the per-component hypotheses. `spend_by_kind()` returns the units apart next to the old `n` and `usd`.
108. **The full-load formula** (`dryrun.full_load`): K keys a day × (C counts × p_count + P posts × p_post + A authors ×
    p_author) with the defaults 60 × (12 × 0.005 + 10 × 0.005 + 3 × 0.010) = $8.40 for the confirmation layer, plus the
    account watches (mega at its posts, KOL at its cap) = $9.89 against the cap. It is a ceiling that no real day reaches
    (a confirmed key stops counting, a refuted key never searches); the expected load with the assumptions (half the
    keys confirmed after four counts) is about $6.40. The dry run prints both, says whether they fit the cap and the
    planner's target cap of $15.00, and notes when `.env` differs from the target. Observed rates replace the assumptions
    from five stopped keys and three searches on (one refuted key is not a confirmed share of zero).
109. **Calibration against the console** (`fart/social/calibrate.py`): one console amount cannot fix four prices, so the
    command prints the uniform hypothesis (every price × console / booked) and one hypothesis per component (the price
    that explains the whole gap if the other components are right), the units and calls per kind, and the config lines
    of each; within 5% the prices are confirmed. The console's per-endpoint breakdown decides which hypothesis is true;
    a unit that differs from ours (per request instead of per post) is a code change, not a price. It writes nothing.
110. **Quality per dollar in the reports** (`report.x_quality_section`, `week.aggregate`): the day's spend on
    `ca_counts` + `ca_search` + `ca_authors` per observation confirmed that day; the day's observations with a count,
    those confirmed within one hour of the decision (the first confirming count at most 3600 s after `as_of`) and ever;
    the counts after the first confirmation of the same observation (waste, valued at the day's average count cost: zero
    by construction with the milestone-7 planner, the measure of the milestone-6 behaviour and of any regression); the
    keys stopped per reason with what the refuted ones cost; and the 24 h outcome of the day's coin observations in
    three groups (confirmed, counted but never confirmed, never counted) under the hit-rate rules of item 74 (unknown in
    the denominator, pending apart; theme observations have no outcome of their own). The milestone-6 block keeps every
    field. The report gives no advice: these are Jim's figures for the live decision after the week.
111. **`fart status` lines added**: `x planner` (active keys by kind, searches pending, counts due, started today,
    stopped today per reason with their cost, deferrals per kind today) and `x plan vs spend today` (per kind the planned
    USD of the dry-run model, the actual USD, both as a share of the cap); `x spend today` keeps its line. The plan model
    is read-only and a failure in it never fails the status (`x_plan_error`).
112. **Defaults and `.env`**: `.env.example` says `X_DAILY_BUDGET_USD=15.00`; the code default stays 3.00 (a missing
    line must not raise the spend), and the dry run and DEPLOY.md say that the full load does not fit $3.00. The planner's
    constants live in `config/x.json` → `planner` and `dryrun`, not in a rule file, so the fingerprint test stays green
    and a later tuning is a commit without a new segment in the week report.

## Milestone 8 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 8 zet de website `https://kingdegen.dev` neer, met het dashboard als eerste pagina.
Geen regelbestand verandert (de vingerafdruk blijft `sha256:e3bf4320c45c`; dezelfde test bewaakt dat), er is geen
schema-migratie, geen nieuwe dependency buiten de standaardbibliotheek, en geen van de bestaande daemons wordt
aangeraakt. (1) **Waar en hoe.** De site draait **op de VPS zelf, achter de gedeelde Caddy die er al staat**, als
`fart-web.service` (gebruiker `fart`, dezelfde hardening als de andere units, luistert alleen op `127.0.0.1:8792`, mag
alleen `logs/` schrijven en ziet `data/` en `out/` alleen-lezen); Caddy krijgt één sitebestand
(`deploy/caddy/fart.caddy`) voor `kingdegen.dev` dat naar die poort proxyt. Het alternatief, een push-architectuur die
de cijfers naar een andere host of een statische site duwt, is bewust niet gekozen: de data staat hier, de Caddy met
de certificaten staat hier, er hoeft geen nieuwe poort open en er komt geen derde partij bij; alleen de deployer raakt
de Caddy-configuratie aan (met een bewaarde kopie en `caddy validate` vóór elke herlaad). (2) **Alleen-lezen,
aantoonbaar.** Elke pagina opent de database met `mode=ro` plus `PRAGMA query_only`; een schrijfpoging faalt (een test
bewijst het), en de unit zelf kan de database niet eens beschrijven. Er is geen knop die iets verandert, ook niet voor
jou. (3) **Login.** Eén eigenaarsaccount: je wachtwoord staat alleen als scrypt-hash in `.env`
(`FART_WEB_PASSWORD_HASH`, gemaakt met `fart web hash-password`, dat het wachtwoord onzichtbaar vraagt en alleen de
hash print), naast een geheim voor de ondertekende sessiecookie (`FART_WEB_SECRET`, van `fart web new-secret`). De
cookie is HttpOnly, Secure, SameSite=Strict en 12 uur geldig; uitloggen maakt hem direct ongeldig; het loginformulier
draagt een CSRF-token; vijf foute pogingen binnen 10 minuten, per adres én voor het account, betekenen 15 minuten
wachten (de pagina zegt hoeveel). Zonder geldige sessie geeft elke pagina en elk `/api/`-adres de loginpagina of een
401; alleen het eigen CSS- en JS-bestand zijn vrij, want die bevatten geen gegevens. Zonder beide `.env`-waarden
start de dienst niet (duidelijke melding, geen herstartlus). (4) **Eerlijkheidsregels op elk getal.** De banner
"Observatie, geen advies. Alles wordt achteraf gemeten." staat op elke pagina; een uur of dag NIET GEDEKT blijft leeg
(nooit nul); een uitkomst die niet bekend is blijft "onbekend", een beslissing binnen haar 24 uur is "nog bezig"; er
staat nergens een winst- of rendementscijfer. Tijden in Nederlandse tijd met UTC erachter. (5) **Pagina's en API.**
`/` dashboard (versheid van daemons en timers, dekking per uur van de laatste 24 uur, beslissingen van vandaag, X-stand
met planner en plan tegenover uitgaven, budgetten, kernpunten van het laatste dagrapport, go/no-go van het laatste
weekrapport), `/observaties` (laatste 200 beslissingen met features, rules_version en uitkomst), `/rapporten` (elk
`out/recall/*.md` als pagina, via een veilige Markdown-subset), `/instellingen` (alleen-lezen: versies, vingerafdruk,
bevroren criteria, actieve instellingen zonder geheimen); dezelfde gegevens als JSON onder `/api/status`,
`/api/coverage`, `/api/decisions`, `/api/x`, `/api/report/latest`, `/api/week/latest`; één klein eigen JS-bestand
ververst de cijfers elke 60 seconden. (6) **Veiligheid in de browser.** Elke respons draagt een strikte
Content-Security-Policy (`default-src 'self'`, geen inline script of stijl, niets van buiten), `nosniff`,
`no-referrer`, `X-Frame-Options: DENY`; verzoeken boven 64 KB worden geweigerd; het log bevat nooit een cookie,
wachtwoord of token. (7) **Uitrol**: `deploy/DEPLOY.md`, "Milestone 8: website": de twee `.env`-waarden via jouw helper
(die kent de namen nog niet; de paragraaf zegt wat erbij moet), `fart-web` starten en op de loopback controleren, het
sitebestand naar `/srv/fart/fart.caddy` (Caddy draait als DynamicUser en kan `/opt/fart` niet lezen), `kingdegen.dev`
uit het oude meme-radar-launch-blok, `caddy validate`, herladen via de admin-API (of een korte herstart van de hele
Caddy, met waarschuwing), controle met `curl -sI https://kingdegen.dev`, rollback met de bewaarde kopieën.

113. **Branch and PR**: all work on the harness branch `claude/zen-albattani-rr70yt`; the PR targets `main`.
114. **On the VPS, behind the shared Caddy, no push architecture** (`deploy/caddy/fart.caddy`, `deploy/fart-web.service`):
    the website is a process next to the daemons that reads the same database and the same `out/recall/` files on the
    same host, and the existing Caddy (`larp-scanner-https.service`, ports 80/443, the certificate machinery) proxies
    `kingdegen.dev` to its loopback port. A push of rendered pages or JSON to another host would add a second copy of
    operational data, a credential to push with, a place to keep in sync and a third party to trust; a new public port
    would add an exposed TLS stack to maintain. The only change outside `/opt/fart` is one site file and one `import`
    line in the shared Caddyfile, made by the deployer with a saved copy and `caddy validate` first.
115. **Standard library only** (`http.server.ThreadingHTTPServer`, `hashlib.scrypt`, `hmac`, `sqlite3`, `html`): no web
    framework, no template engine, no JS framework. The site is six endpoints and four pages; the framework would be
    larger than the application, would need updating, and would widen what the CSP has to allow. One connection per
    request keeps SQLite's thread rules simple and the open cheap.
116. **Read-only in three layers** (`fart/web/data.py::open_db`, the unit): `mode=ro` plus `PRAGMA query_only=1` on every
    connection (a write raises `OperationalError`: tested), `ReadWritePaths=/opt/fart/logs` only with
    `ReadOnlyPaths=/opt/fart/data /opt/fart/out` in the unit (even a bug could not write the database or a report), and
    no route that changes state (a POST to a page is 405; the only POSTs are login and logout). Because `ReadOnlyPaths`
    also forbids creating the WAL side files, a read-only open fails at its first query when every daemon is stopped
    (SQLite removes `-wal` and `-shm` at the last clean close); the open then falls back to `immutable=1`, which reads the
    checkpointed file without locks: correct while no writer runs, which is exactly when the files are absent, and
    bounded to one request otherwise. Both paths are documented in `docs/UNVERIFIED.md`.
117. **Login counters in memory, replayed from the log, not in `kv`** (`fart/web/auth.py::LoginGuard.load_from_log`): the
    brief asked for the failed-attempt counters in memory and in `kv`, but the website may not write the database, and
    writing `kv` would need a read-write connection and a path in `ReadWritePaths`. Every failed attempt is logged as
    `web.login_failed` with the client IP and the time (never the password) in `logs/web.log`, the one file the service
    writes; at start the guard reads the tail of that log and replays the failures that can still matter (window plus
    lock, 25 minutes), so a restart does not reset a running lock. The lock is per IP and per account (there is one
    account): an attacker from many addresses can keep the owner out for 15 minutes at a time, which is the price of the
    per-account rule the brief asked for; the log shows the IPs.
118. **Static files without a session** (`/static/app.css`, `/static/app.js`): the brief says every route needs a session,
    and every route that carries data does. The two static files carry no data, and the login page cannot be styled
    without them (the CSP forbids inline styles, which was the brief's other requirement). They are the only exception;
    a test lists the routes that answer 401 without a session, `/api/*` included.
119. **401 with the login page, no redirect** (`App.handle`): a page requested without a session gets the login form in
    the body with status 401 (an API route gets `{"error": "login required"}`); after a login the browser always lands on
    `/`. No `next` parameter, so no open-redirect surface; `curl -sI https://kingdegen.dev` shows `401` with the security
    headers, which DEPLOY.md calls the correct answer.
120. **Sessions are signed tokens, revocation in memory** (`auth.Signer`, `auth.Revocations`): `<issued>.<nonce>.<HMAC>`
    under `FART_WEB_SECRET` with a 12 h life measured from issue, no server-side session table (the database is
    read-only, and a session file would be a second thing to write). Logout revokes the nonce in memory until the token
    would have expired anyway; a restart forgets the revocations but keeps the 12 h bound; a new secret in `.env` logs
    every browser out at once. The CSRF token of the login form is the same signed shape under another domain string
    (`csrf.`), as a cookie scoped to `/login` and a hidden field that must match (double submit plus signature, 1 h);
    the logout form carries an HMAC of the session nonce. `hmac.compare_digest` everywhere.
121. **The password hash format** (`scrypt:n:r:p:<salt hex>:<key hex>`, n=2^14, r=8, p=1, 16 salt bytes, 32 key bytes):
    chosen so the value survives the `.env` parser (no `=` inside), a shell helper (no `$`, no quotes) and a copy-paste by
    eye; `parse_hash` bounds n, r and p so a garbled value cannot cost seconds or gigabytes per attempt. `fart web
    hash-password` asks twice, invisibly, requires 12 characters and prints only the hash; `fart web new-secret` prints
    32 random bytes as hex. Neither writes anything: the deployer (or Jim's helper) puts the lines in `.env`.
122. **One data model, two mechanical renderers** (`fart/web/data.py`, `fart/web/pages.py`, `static/app.js`): the API
    returns raw values next to Dutch display strings (`*_label`), and both the server-side HTML and the 60 s refresh in
    the browser fill the page from the same JSON by dotted path with the same three rules (None is "–", True is "ja",
    False is "nee"). The JS builds table rows with `textContent` only: no HTML from data, nothing to escape twice, and
    the page the browser refreshes is the page the server rendered.
123. **NIET GEDEKT stays empty on the site too** (`data.coverage_view`, `data.report_latest_view`): an hour without an
    active create source shows no counts (the gap that caused it is shown), and the key points of a daily report whose
    day was not covered are all "–" with the universe called unknown, exactly like the report itself (milestone 3,
    item 51). An unknown outcome of a decision stays "onbekend" with an empty peak (never 0); a decision inside its 24 h
    is "nog bezig"; a theme observation has "geen eigen uitkomst" (item 110). No paper-trade PnL on the dashboard.
124. **The freshness table is observation, not a verdict** (`data.FRESHNESS`): per daemon and timer the last heartbeat or
    run with its age and "vers" / "oud (ouder dan ...)" / "nooit gezien" against an expectation per row (3 minutes for
    the daemons, 15 for the streams and the 5-minute timer, 26 hours for the daily timers and the backup check). The
    thresholds are display rules, not alarms; the Telegram health alerts (milestone 2) keep their own.
125. **Markdown subset instead of a Markdown library** (`fart/web/markdown.py`): the reports use headings, lists, pipe
    tables, bold and code; the renderer escapes everything first and knows no links, images or raw HTML, so a coin name
    with `<script>` in it stays a coin name on the page (tested). File names are matched against the two report shapes
    only (`<day>.md`, `week-<day>.md`), never joined from the URL.
126. **Caddy: a copy in `/srv/fart`, not an import from `/opt/fart`** (DEPLOY.md, step 5): the shared Caddy runs as a
    `DynamicUser` that cannot traverse `/opt/fart` (`root:fart 0750`). An `import /opt/fart/app/deploy/caddy/fart.caddy`
    line would make `caddy reload` from a root shell succeed (the CLI adapts the file as root) and the next
    `systemctl restart larp-scanner-https` fail for every site on it. The installer refreshes `/srv/fart/fart.caddy`
    when the directory exists and never reloads Caddy itself; DEPLOY.md validates as `nobody` to see what the
    DynamicUser sees. HSTS, `-Server` and the request-body limit live in the site file; the application's own headers
    are repeated there only as defaults (`?`, deferred).
127. **The unit may not talk to the network** (`IPAddressDeny=any`, `IPAddressAllow=localhost`, `After=network.target`):
    the website needs the loopback and nothing else; `Restart=on-failure` with `RestartPreventExitStatus=2` makes a
    missing `.env` value a stopped unit with a clear journal line instead of a restart loop. The installer installs the
    unit on every run and enables and starts it only when both values exist, so the upgrade can go first and the
    password later.
128. **Request limits in the application too** (`server.Handler._request`): 64 KB for the request line, headers and body
    together (431 or 413, the body never read), `Content-Length` required on a POST (411 for chunked), a 30 s socket
    timeout, HTTP/1.1 with explicit lengths and `Server: fart` (no Python version on the wire). Caddy's `request_body`
    limit is the first line of defence; the application does not rely on it.
129. **`.env` names and `fart status`**: `FART_WEB_BIND` (default `127.0.0.1:8792`), `FART_WEB_PASSWORD_HASH`,
    `FART_WEB_SECRET` join `ENV_KEYS`; `Settings.describe()` reports them as configured / not configured and `fart
    status` prints a `web:` line; the test that pins `.env.example` to `ENV_KEYS` covers the new names with empty values.

## Milestone 9 (26 oktober 2026)

**Samenvatting voor Jim.** Mijlpaal 9 bouwt de website opnieuw op als openbaar product: `https://kingdegen.dev` is het
gezicht van de radar naar buiten, Telegram blijft het kanaal voor alerts aan de community. Geen regelbestand verandert (de
vingerafdruk blijft `sha256:e3bf4320c45c`; dezelfde test bewaakt dat), er is geen schema-migratie, geen nieuwe dependency,
de daemons en de meting blijven onaangeraakt en de webdienst blijft alleen-lezen.
De keuze: **openbaar dashboard, eigenaarssectie achter login**. (1) **Het dashboard is verwerkte data, live.** Jims aanwijzing halverwege de bouw: het
dashboard moet laten zien wat de radar nú ziet, niet hoe de machines lopen. `/` toont daarom waar het geld heen gaat (de
gevolgde munten met de meeste netto SOL-instroom in de laatste 5 minuten, met kopers, trades, marktkapitalisatie uit de
curve en het aandeel van de grootste koper), de nieuwste munten met hun tempo, de thema's die nu opkomen in de muntnamen
(dezelfde clustering en drempels als de engine, alleen lezend), de laatste graduaties met de nieuwste prijssample en de
24-uursuitkomst volgens de trackerregel van de rapporten, wat er rondgaat op X (eerlijk over de stand: uit, schaduw of
live), de observaties van vandaag en één regel meting; de pagina ververst zichzelf elke 15 seconden. De meting zelf
(meetweek met go/no-go-toets, dekking van de laatste 24 uur, laatste rapporten, hoe dit werkt) staat op `/meting`; de
machines op `/eigenaar` achter de login. (2) **Eerlijkheid in elk getal.** Een uur of dag NIET GEDEKT blijft leeg en is in
de grafieken gearceerd (nooit een nul-balk); onbekend blijft onbekend; een interval staat er pas als de bootstrap er is,
anders "te weinig data"; de X-kaart zegt "staat uit" zolang er geen sleutel is; de instroom-tabel zegt erbij dat de radar
alleen de gevolgde munten ziet; nergens een winst-, rendement- of signaalclaim, geen koopknop, geen link naar een
handelsplaats, het mint-adres hoogstens als tekst. (3) **Eén ontwerpsysteem** (`fart/web/static/app.css`): kleurtokens,
donker standaard en licht via `prefers-color-scheme`, één accentkleur, een typografische schaal, een 8-puntsraster,
kaarten met kerncijfer, uitlegregel en statuspil, tabellen met vaste kolommen die op een telefoon stapelen, grafieken als
server-side inline SVG met een tekstsamenvatting voor schermlezers, contrast WCAG AA. Naam, productwoord, slogan en
accentkleur staan in `config/web.json` (geen regelbestand); de accentkleur bereikt het stylesheet via een toegevoegd
tokenblok, nooit als inline stijl, zodat de CSP `'self'` blijft. (4) **Openbaar en toch veilig en snel.** `/api/public/*`
met alleen openbare velden (een test platwalst elk openbaar JSON-document en bewijst dat geen budget, sleutel, pad, adres
of eigenaarsveld erin voorkomt), een in-geheugen-cache per onderdeel (15 s live, 30 s de rest) die gelijktijdige
bezoekers één berekening laat delen, `Cache-Control` en `ETag` op elk openbaar antwoord (304 bij een match), 120 verzoeken
per minuut per adres op de openbare routes en daarna 429, de eigenaarssectie met login, CSRF en lockout zoals in M8.
`FART_WEB_PUBLIC=0` zet alles weer achter de login. (5) **Caddy en uitrol**: `www.kingdegen.dev` uit het sitebestand zolang
er geen DNS-record is; `deploy/DEPLOY.md`, "Milestone 9: website v2": `config/web.json`, de schakelaar, de `curl`-controles
en de rooktest dat `/api/public/status` geen eigenaarsvelden bevat.

130. **Branch and PR**: all work on the harness branch `claude/wizardly-pascal-rs131r`; the PR targets `main`.
131. **Public dashboard, owner section behind the login** (`fart/web/server.py`): with `FART_WEB_PUBLIC=1` (the default,
    also without the line) the dashboard, `/observaties`, `/meting`, `/rapporten` and its pages, `/methode`, `/tools` and
    `/api/public/*` need no session; `/eigenaar`, the raw Markdown reports (`/rapporten/<file>.md`), `/instellingen`
    (a redirect to `/eigenaar`) and the milestone-8 JSON under `/api/*` keep the login. A visitor on an unknown path gets
    404, never a login prompt; an owner route without a session gets 401 as before. `FART_WEB_PUBLIC=0` is milestone 8:
    everything behind the login; the milestone-8 test suite runs in that mode and stays the regression test for it.
132. **The dashboard is processed data, live; the measurement has its own page** (`fart/web/public.py`, `pages.dashboard`,
    `pages.measurement_page`): Jim's redirect during the build ("het dashboard is verwerkte data: wat is nu hot, waar komt
    het meeste volume binnen, live"). `/` carries the live hero (tempo of new coins, buyers and net SOL inflow on the
    watched coins in the last five minutes, graduations of the last hour, the SOL price the figures are converted with),
    the hot coins, the newest coins, the themes, the latest graduations, the X card, today's observations and a one-line
    measurement strip; `/meting` carries the week card, the coverage card, the reports card and "hoe dit werkt". The live
    views refresh every 15 s, the rest every 30 s (`config/web.json` → `refresh_s`, `live_refresh_s`).
133. **Hot coins only from the watched coins, and the page says so** (`public.hot_coins_view`): the radar has every trade of
    the coins on its watchlist (every new coin for its first five minutes, then the coins the engine keeps) and of no other,
    so "where the money goes" is computed over the active watchlist with one grouped query per request (the `(mint,
    received_at)` index serves it) and the card's explain line says exactly that. Net inflow = buy SOL minus sell SOL over
    five minutes, buyers = distinct buying wallets, the largest buyer's share over fifteen minutes as in the engine, the
    market cap from the last trade's virtual reserves through `fart/chain/curve.py` (converted to USD with the SOL price the
    tracker cached, when it is less than an hour old; otherwise SOL only). The row's status (observatie, nieuw, gevolgd,
    gegradueerd) is a fact about the watchlist, never a verdict.
134. **Themes through the engine's own functions, read-only** (`public.themes_view`): `features.recent_coins`,
    `meta.cluster`, `features.meta_features`, `features.rank_metas` and `decide.is_bursting` are called on the read-only
    connection with the engine's config, so the dashboard shows the clusters the engine sees with the same thresholds and
    never a second definition of a theme. The rule files are imported, not changed (the fingerprint test proves it). A theme
    with no baseline is "nieuw woord" (the engine's infinite burst), a cluster below the burst threshold is "rustig".
135. **Graduation outcomes under the reports' tracker rule** (`public.graduation_outcome`): a stored "no winner" whose
    `mcap_samples` are not dense enough (`report.tracker_coverage`) is shown as "onbekend", exactly as the daily and week
    report count it; a winner stays a winner; no row inside the 24 h is "nog bezig". The market cap and 24 h volume of a
    graduation come from the newest tracker sample and carry its age; a sample older than two hours is "oud", a coin
    without a sample says "geen sample" (never a zero).
136. **The X card is honest about its state** (`public.social_view`): with the X layer off the card says so and shows
    nothing; in shadow mode it shows the keys with the most mentions in the last hour and the fast posts of the big
    accounts (`x_keys`, `social_posts`) with the sentence that nothing of it reaches the observations; in live mode the
    same without that sentence. No count is invented: a key without a count yet says "nog niet geteld".
137. **Only public fields, proven** (`tests/test_m9_public_site.py::test_public_json_carries_no_owner_fields`): the public
    views are a separate module; the test flattens every `/api/public/*` document (the day and week report documents and
    the day filter included) and asserts that no key contains the owner vocabulary (budget, caps, USD spend, CU, secret,
    hash, password, token, bind, planner, daemon, heartbeat, shadow, failures, gaps, paths, logs, schema, cost, ...), that
    no value carries a path, the fixture root, an IP address, the port or `X_MODE`, and that none of the owner's budget
    values and none of the secrets occur in the text. The page test does the same for the rendered HTML with the Dutch
    owner words (budget, plafond, planner, sleutel, daemon, heartbeat, logboek, ...).
138. **Cache, ETag and rate limit in the application, not in Caddy** (`server.Cache`, `server.RateLimiter`): a view is
    computed at most once per TTL (15 s live, 30 s the rest), concurrent misses wait for one computation, the oldest entries
    go when there are more than 128; an anonymous public answer carries `Cache-Control: public, max-age=<ttl>`, `Vary:
    Cookie` and an `ETag` of its bytes (304 on `If-None-Match`); an answer to a session is `no-store` and carries no ETag,
    so a shared cache can never serve the owner's navigation to a visitor. The limiter keeps a sliding window of request
    times per client address (the last `X-Forwarded-For` entry Caddy appends, trusted only from the loopback), 120 per 60 s,
    bounded to 10,000 addresses with eviction of idle ones; static files, the login page and every request with a session
    are outside it. Caddy keeps its request-body limit and HSTS; nothing else moved there, so the behaviour is tested.
139. **One renderer, swapped by region** (`static/app.js`): the server renders the whole page; the script fetches the same
    page every 15 or 30 s (not while the tab is hidden), parses it in an inert document and replaces the elements that
    carry `data-live` with their fresh twins (`replaceWith` + `importNode`, no `innerHTML`, no evaluation, no focus theft);
    the footer line says when the page was last updated and when a refresh failed. The milestone-8 JSON-path refresh is
    gone: two renderers of one page were one too many.
140. **Charts without a library** (`fart/web/charts.py`): inline SVG with a `viewBox` and `preserveAspectRatio="none"`
    so it scales to the card, colours through CSS classes (the CSP forbids inline styles; `fill="url(#hatch)"` is a
    presentation attribute, not a style), a hatch pattern for NIET GEDEKT (a hatched cell has no height: it is not a
    zero), whole-number ticks for counts, a line that breaks at a day without a value, a horizontal band for a bootstrap
    interval only when the week report has one, and a visually hidden paragraph with the figures in words for screen
    readers (the sentence a test checks).
141. **The design system** (`static/app.css`): tokens for colours, spacing (4/8/16/24/32/48/64) and type (13/15/17/20/26
    px plus fluid sizes for the hero and the key figures), dark by default, light under `prefers-color-scheme: light`, one
    accent (`--accent`, with `--accent-dark` for text on a light background and an RGB triple for the tints), status pills
    with a leading dot (an outlined dot for the unknown states), tables with `table-layout: fixed`, `<colgroup>` widths and
    a stacked layout under 640 px (each cell shows its header through `data-label`, so nothing scrolls sideways), visible
    focus rings, reduced-motion respected, a skip link, a print stylesheet. Contrast of every text colour pair against
    its background is at least 4.5:1 in both themes (`theme.contrast`, checked by a test for the accent and its dark twin).
142. **The brand in `config/web.json`, the accent through `theme.py`** (`Settings.web_site`, `theme.accent_override`):
    site name, product word, tagline, domain, accent and the two refresh intervals; missing or empty keys fall back to the
    defaults in `fart/config.py`, so an edited file never breaks a page. A valid `#rrggbb` accent becomes a `:root` block
    appended to the served stylesheet with the colour, its RGB triple, a darkened twin (same hue, lightness 0.27, which
    keeps 4.5:1 on the light background) and the text colour for an accent surface; an invalid value is ignored. The
    file is not a rule file: `RULE_FILES` is unchanged and the fingerprint test is the proof.
143. **The structured report pages come from the JSON, the raw Markdown stays with the owner** (`public.day_report_view`,
    `public.week_report_view`): the public pages show the coverage, the universe, the observations, the recall, the winners
    (mint as text), the unknowns and the versions from `out/recall/*.json`, with the NIET GEDEKT rule (a day without a
    covered hour shows only the hours and says the universe is unknown) and never the operational sections of the Markdown
    (budget and costs, enrichment and failures, the X shadow economics, the paper trades); `/rapporten/<file>.md` renders
    the complete Markdown for the owner as in milestone 8.
144. **The log summary is a tail, counted** (`data.logs_view`): the last 256 KB of every daemon log (JSON lines) give the
    size, the time of the last line, the warnings and errors counted and the last error named with its time; never a line's
    fields, so no address and no secret reaches the page (the logs carry none by design, the view adds a second guard).
145. **`www.kingdegen.dev` out of the Caddy site file**: the subdomain has no DNS record; Caddy would keep trying to obtain a
    certificate for it and log the failures (the deployer already removed it from the installed copy at the M8 rollout).
    The comment in the file says when to add it back; DEPLOY.md and UNVERIFIED.md carry the note.

## Milestone 10 (26 oktober 2026)

**Samenvatting (Nederlands).** Direct na de uitrol van M9 (26 oktober 2026, 21:50 tot 22:00 UTC; 174.305 trades, 34.635
coins, 884 decisions) deed `GET /` op de VPS twee keer een time-out na 60 s; koud duurde `themes` meer dan 150 s en `status`,
`live` en `coverage` zo'n 7 s. M10 maakt het dashboard snel en robuust zonder ontwerp of inhoud te veranderen, stdlib-only,
webdienst alleen-lezen, regelbestanden ongewijzigd (vingerafdruk `sha256:e3bf4320c45c`). (1) **Thema's van de engine**: de
engine schrijft elke tick de top 50 van `features.rank_metas` met de velden van de kaart als JSON in kv
(`engine.metas_latest`, in dezelfde beslissingstransactie als `engine.meta_burst_sets`; `fart/engine/themes.py`); de kaart
leest alleen die waarde en zegt bij een waarde ouder dan 5 minuten "Thema's worden nu niet berekend" met de leeftijd, nooit
een verouderd cijfer zonder vermelding. (2) **Indexen voor tijdvensters**: schemamigratie v8 (`trades(received_at)`,
`trades(block_time)`, idempotent); elk tijdvenster van de openbare views begrenst eerst op de geïndexeerde `received_at` (een
minuut ruimer) en legt daarna `COALESCE(block_time, received_at)` over die rijen; geen `COALESCE` meer in een WHERE zonder
indexbereik; een test bewijst met `EXPLAIN QUERY PLAN` dat geen tijdvensterquery een grote tabel scant. De dekking van de
laatste 24 uur komt uit de `coverage`-tabel die de ingest elke minuut herberekent; de observaties halen namen en uitkomsten
in batches op. (3) **Cache die nooit laat wachten**: één achtergrondthread in `fart web` (`server.ViewStore`) berekent de
openbare views bij de start en daarna elke 15 s (live) en 30 s (de rest) met een eigen alleen-lezen verbinding en korte
transacties; een verzoek leest alleen en wacht nergens op; nooit berekend = "wordt berekend" op de pagina en
`{"state": "computing"}` (200, `Cache-Control: public, max-age=2`) in de JSON; een berekening langer dan 10 s wordt gelogd
met naam en duur; de laatste duur per view staat op `/eigenaar` en, via `logs/web-views.json`, in `fart status`
(`web views: themes 0.02 s, live 0.4 s, ...`). (4) **Tests**: een fixture-database van 200.000 trades, 40.000 coins, 2.000
graduaties en 1.000 decisions waarop elke openbare view onder 300 ms blijft en elke tijdvensterquery een index gebruikt; een
verzoek aan `/` binnen 1 s terwijl een view kunstmatig 30 s duurt; de thread stopt netjes bij SIGTERM; de M8/M9-tests en de
vingerafdruktest blijven groen. (5) **Uitrol**: `deploy/DEPLOY.md`, "Milestone 10: performance": migratie v8, herstart van
fart-engine en fart-web, de `curl`-controle koud en warm onder 1 s en de `fart status`-regel met de viewtijden.

146. **The engine writes its themes, the website reads them** (`fart/engine/themes.py`, `Engine.tick` step 6,
    `public.themes_view`): the ranked metas the engine computes every tick anyway (the top `max_terms` of
    `features.rank_metas`, with the fields the card shows, three example names, whether the term is in the frozen
    meta_burst choice and whether it had an observation today) go as one JSON document into kv `engine.metas_latest`,
    inside the decision transaction (one row, overwritten each tick, about 20 KB). The website's card reads that value
    and computes nothing: the clustering ran minutes per visit on the VPS (7,486 coins in the window, one baseline scan
    per term) and belongs to the engine, which has the data in memory. A snapshot older than `MAX_AGE_S` (5 minutes) is
    "Thema's worden nu niet berekend" with its age and no rows: an old figure is never shown as current. The rule files
    are imported, not changed; the daily report and `fart engine explain` keep their own path.
147. **Time windows as index ranges, the event time on the range** (`public._count_since`, `public._newest`, schema v8):
    `trades(received_at)` and `trades(block_time)` are the two new indexes; every window of the public views is written
    as `received_at >= t - ET_MARGIN_S AND COALESCE(block_time, received_at) >= t` (a row never reaches us more than a
    minute before its chain time, so the range contains every row of the window and the event time keeps the exact
    semantics), and "the newest N by event time" is "three times N newest by the indexed received_at, sorted by event
    time" (the index walks them in order and stops). A test records every statement of every public view on the big
    fixture database and runs `EXPLAIN QUERY PLAN` on the ones with a time predicate: none may scan `trades`, `coins`,
    `migrations`, `decisions`, `outcomes`, `mcap_samples`, `coverage` or `social_posts`. The owner's live coverage view
    and the daily report keep `compute_hour` (they must count, not read).
148. **The public coverage comes from the `coverage` table** (`public.coverage_view`, `_hour_covered`, `_day_covered`):
    the ingest recomputes the current and the last three hours every minute (`coverage.recompute_recent`), so every
    hour of the last 24 was current once and has its row; the view reads 24 rows instead of counting 24 hours with
    three table scans each (6.9 s on the VPS). An hour without a row was never counted: NIET GEDEKT, no number. In the
    first minute of an hour, before its row exists, the previous hour's row says whether the counting was running a
    moment ago (`computed_at` within `COVERAGE_RECENT_S`); an old row says nothing about now. The fixture world writes
    the rows the way the ingest does, so the tests see the same 4 hours NIET GEDEKT as before.
149. **A view store with one thread instead of a cache that waits** (`server.ViewStore`): the milestone-9 `Cache.get`
    computed on a miss and made concurrent requests wait for one computation; with a 15 s TTL and computations of seconds
    to minutes every request waited. The store keeps one entry per view (value, when it was made, the duration, errors);
    one daemon thread computes the due views every second's poll (the live views every `live_refresh_s`, the rest every
    30 s, the report pages riding along with the reports list, everything once at start) on one read-only connection per
    cycle in autocommit (every statement its own short read, so the WAL checkpoint is never held back); the request
    threads call `get()` and nothing else. A failed computation keeps the previous value and its timestamp, is counted
    and retried at its next due time (never every second); a slow one is logged (`web.view_slow`); `stop()` interrupts
    the running query (`Connection.interrupt`) and joins the thread, so SIGTERM ends the service within a second. The
    request-triggered path survives only for the day filter of the observations (`request()`: an on-demand key the thread
    computes next, refreshed while asked for, dropped after ten minutes of silence, bounded to 128 keys); its first
    answer is "computing" too.
150. **"Wordt berekend" is a state, not a number** (`public.computing`, `pages.computing_card`): until the thread's first
    cycle finished a view, the page shows the same card with the pill "wordt berekend" and one sentence (every
    `data-live` region exists, so the page script keeps swapping), the JSON answers `{"state": "computing", ...}` with
    HTTP 200 and `Cache-Control: public, max-age=2` (never 503: the site is up, the figure is not there yet; never a
    cached placeholder for 15 s). A report that exists on disk but has no entry yet is "computing", an unknown name 404.
151. **The view times in `fart status` through a file, not the database** (`logs/web-views.json`, `cli.web_views_line`):
    `fart status` runs in another process and the website must never write the database, so the store writes the last
    duration per view (and the errors) atomically to the one file next to its log after every cycle; `fart status` prints
    `web views: themes 0.02 s, live 0.4 s, ... [written N s ago]` with `FAILED` on a failing view, or says that no cycle
    ran yet. `/eigenaar` shows the same table from the live store ("Weergaven van de website"). The unit's
    `ReadWritePaths=/opt/fart/logs` already allows it; nothing else changes in the unit.
152. **The big fixture database is built, not shipped** (`tests/test_m10_performance.py::build_big_world`): 200,000
    trades, 40,000 coins, 2,000 graduations, 1,000 decisions, tracker samples, 200 watched coins with trades in the last
    five minutes, the coverage rows and the engine's snapshot, deterministic from one seed, a few seconds to build; every
    public view must stay under 300 ms on a fresh connection and the whole first cycle under 3 s. The 30 s view in the
    "never waits" test is a computation blocked on an event, released by the test: the request must come back within a
    second with the placeholder the first time and with the last value afterwards.
153. **Branch and PR**: all work on the harness branch `claude/eloquent-mayer-acz0cg`; the PR targets `main`.

## Milestone 11 (29 oktober 2026)

**Samenvatting (Nederlands).** Gemeten op de VPS op 26 oktober 2026 tussen 22:58 en 23:19 UTC (199.000 trades, 37.500 coins,
7.100 coins in het venster van 3 uur, circa 1.200 nieuwe coins per uur) duurde de engine-tick 166 tot 337 s (`tick_s` in de
`engine.tick`-logregel; om 14:00 UTC nog 4 s): het engine-proces zat op 97% CPU, de heartbeat liep op tot 565 s, beslissingen en
`engine.metas_latest` kwamen elke drie minuten in plaats van elke 15 s en de thema-kaart van de website stond meestal op "nu niet
berekend". `features_s` was 0,35 s; de tijd zat in stap 5 (de metas): `meta_features` haalde per term alle coins van 6 tot 48 uur
vóór `as_of` op (tienduizenden rijen) en tokeniseerde ze opnieuw om te tellen hoe vaak de term voorkwam, honderden keren per
tick. M11 brengt de tick terug naar seconden zonder dat één uitkomst verandert.
**De regels zijn niet veranderd, alleen de rekentijd.** (1) **Basislijn één keer per tick** (`features.meta_baseline`, `MetaBaseline`): de basislijncoins worden één keer
per tick opgehaald als indexbereik op de eventtijd (`coins_block_time`; beide gevallen van `COALESCE(block_time, received_at)`
uitgeschreven, exact dezelfde rijen als de oude query), één keer getokeniseerd en per term geteld (`collections.Counter`);
`meta_features` leest `base_n` uit die teller. Dezelfde tijdgrenzen op dezelfde eventtijd, dezelfde `tokenize` met dezelfde
stopwoorden en `min_token_len`, dezelfde `base_hours`, dezelfde afronding; `recent_coins`, de clustering, de tradesaggregatie
per term (via de mint-index) en de rangschikking zijn ongewijzigd. Op de fixture van het bewijs (20.000 coins, 50.000 trades)
gaat stap 5 van 13 s naar 0,3 s. (2) **Tick-begroting en zichtbaarheid**: elke tick logt de duur van elke stap
(`engine.tick_steps`: `expire_s`, `features_s`, `promote_s`, `rpc_s`, `metas_s`, `decisions_s`, `tick_s`), een tick langer dan
het loopinterval (15 s) wordt geteld (kv `engine.tick_slow`, `engine.tick_slow.<dag>`, de laatste in `engine.tick_slow.last`)
en één keer per uur gelogd, en de duren van de laatste voltooide tick staan in kv `engine.tick_last`, geschreven in de
beslissingstransactie van de volgende tick (geen schrijfactie buiten de bestaande); `fart status` toont ze
(`engine tick: ...`) en `/eigenaar` heeft de kaart "Engine-tick (stappen en budget)". Er wordt nooit werk overgeslagen: een
trage tick wordt zichtbaar, niet verborgen. (3) **Bewijs en registratie**: `features.py` is een regelbestand, dus de
vingerafdruk verandert (van `sha256:e3bf4320c45c` naar `sha256:d45c41793c64`). Dat mag alleen met bewijs:
`tests/test_m11_engine_performance.py::test_meta_step_old_and_new_implementation_are_byte_equal` draait de M10-implementatie
(byte voor byte bewaard als `tests/reference/features_m10.py`, sha256 vastgepind) naast de nieuwe op een fixture-database van
20.000 coins over 48 uur en 50.000 trades en bewijst dezelfde metas (elke term, elk veld, de afronding, de volgorde), dezelfde
coin-features en dezelfde `decide.coin_verdict`/`meta_verdict`; de overige regelbestanden zijn byte-gelijk aan de meetweek
(hun deelvingerafdruk is vastgepind). De nieuwe vingerafdruk staat in `config/measure.json` →
`community_gate.frozen_rules_equivalents` met datum, PR-nummer en de naam van die test; het weekrapport neemt die vingerafdruk
samen met de bevroren en zet onder de segmenttitel dat het om een bewezen gelijkwaardige berekening gaat; de website blijft
"regels bevroren" tonen en benoemt de gelijkwaardigheid op `/methode` en `/eigenaar`. (4) **Website**: met een snelle tick is
`engine.metas_latest` weer vers (≤ 30 s oud) en toont de thema-kaart "fresh"; de ouderdomsgrens van 5 minuten blijft.
(5) **Uitrol**: `deploy/DEPLOY.md`, "Milestone 11: engine performance": herstart van fart-engine, `tick_s` in het engine-log
(< 15 s), de stapduren in `fart status`, de nieuwe vingerafdruk in `rules fingerprints (engine)` en de regel over de
gelijkwaardigheid in het weekrapport. Geen schemamigratie, geen nieuwe dependency, stdlib-only. Terzijde: de openbare kopregel
(`public.headline`) faalde op een dag zonder dekking (drie delen verwacht, twee aanwezig); hij voegt nu de delen samen die er
zijn.

154. **The baseline once per tick, by construction the same count** (`features.meta_baseline`, `MetaBaseline`,
    `BASELINE_SQL`; `themes.compute_metas` builds it once and hands it to every `meta_features`): the milestone-10 `base_n`
    was `sum(1 for r in base_rows if term in tokenize(r.name, r.symbol, stopwords, min_len))`, a membership test in a set per
    row; a `Counter` updated with every row's token set gives, for every term, exactly that sum (a set counts a term once per
    coin, whether it occurs in the name, the symbol or both). The window query is rewritten as
    `(block_time IS NULL AND received_at > X AND received_at <= Y) OR (block_time IS NOT NULL AND block_time > X AND block_time <= Y)`,
    the two cases of `COALESCE(block_time, received_at)` spelled out: logically the same rows, served by `coins_block_time`
    (a MULTI-INDEX OR: the NULL rows and the range) instead of a walk over every coin through `coins_received_at`. A
    baseline of another tick or configuration is refused (`ValueError`), never used silently; without a baseline
    `meta_features` computes one for the call (the same value, the old cost), so direct callers and the milestone-5 tests
    keep working. `recent_coins` is deliberately untouched, SQL included: its row order feeds `sample_mints` and the tie
    order of the youngest-first sorts, and an identical statement on the same database gives an identical order.
155. **Why only the baseline, and no shared trade aggregate** (the per-term `migrations` and `trades` queries stay as
    they were): on the proof fixture the per-term queries of 131 terms take 0.14 s and the clustering milliseconds; the
    baseline was 12.7 of the 12.9 s. Combining per-mint sums into per-term sums in Python would change the order of
    floating-point additions (SQLite sums the rows of the IN-list in one pass) and could move a value across a rounding
    boundary: not byte-equal by construction, and not needed. The step is linear in the coins of the baseline and of the
    window, plus a cheap index lookup per member mint.
156. **The tick budget is visible, never enforced by skipping** (`Engine.record_tick`, `STEPS`, `engine.tick_steps`,
    `engine.tick_slow`, kv `engine.tick_last` and `engine.tick_slow*`): the durations are measured per step with the
    monotonic clock, logged every tick in one compact line (about 300 bytes; the `engine.tick` summary keeps its cadence
    and carries them too), and written to kv in the next tick's decision transaction: the engine has no write outside its
    existing transactions, and the previous tick is the last one whose write step has a duration. A tick longer than the
    loop interval is counted the moment it ends (in memory, then kv at the next write) and logged at most once per hour
    with the number of slow ticks since the previous line, so a slow night is one line per hour in the log and a counter
    in `fart status`, never silence and never a skipped step. A failed tick is recorded with the steps it completed. The
    counters are not failures (`engine.tick_failed` stays what it is): they are a budget signal.
157. **A changed rule file with unchanged rules is registered, not hidden** (`config/measure.json` →
    `community_gate.frozen_rules_equivalents`, `version.pooled_fingerprint`, `version.runs_frozen_rules`,
    `week.segment_equivalents`, `week.equivalence_line`): the fingerprint mechanism of milestone 5 stays exactly what it
    is (the hash of the rule files; the engine records it per rules_version). What is new is a declared, bounded exception:
    a fingerprint listed in the config with a date, a PR and the name of the test that proves the equivalence is pooled
    under the frozen one by the week report (which prints the pooling under the segment title and in "Versies") and
    counts as "regels bevroren" on the website (which names it on `/methode` and `/eigenaar`). Without the entry the same
    rules_versions are two segments, as before; a real rules change never goes into the list, and the fingerprint test of
    milestone 5 now checks both the pooled equality and that every entry carries its proof (the test file and function
    exist). The frozen entry itself (`150ca73cb354`, `sha256:e3bf4320c45c`) is untouched.
158. **The proof is a frozen copy, not a re-derivation** (`tests/reference/features_m10.py`, `tests/reference/README.md`,
    `tests/test_m11_engine_performance.py`): the milestone-10 rule file is kept verbatim (its sha256 is pinned in the
    test; it is loaded as a module of the `fart.engine` package so its relative imports resolve) and run next to the new
    implementation on a fixture of 20,000+ coins over 48 hours (vocabulary names, camelCase, stopwords, unicode, digits,
    no name, 5% received-only, clock skew, rows at every window boundary, late backfills) and 50,000+ trades (the themes
    window, the watched coins' windows, whales, creator sells, boundary rows, late rows), with graduations, the watchlist
    and X rows. Compared as JSON without sorting: the metas of every term (values, rounding, field order, list order and
    ranking) at three ticks (a full tick, a sampled one four hours earlier, one where the baseline is empty), the coin
    features of 300 mints at two ticks with their verdicts and promotions, `x_features` and `mega_post_feature`. The
    other rule files are pinned by a partial fingerprint, so the proof covers exactly the file that changed. The test also
    proves one baseline query per tick as an index range, the step under 5 s and at least five times faster than the
    reference, the tick summary, the slow-tick accounting, and the fresh themes card.
159. **The public headline joins what it has** (`public.headline`): the dashboard's sentence assumed three parts, but the
    graduations are only counted on a covered day, so a day without coverage gave an `IndexError` and the status view
    failed (the page then said "wordt berekend" forever). Found by the test suite on a day after the fixture date; fixed by
    joining the parts present ("a, b en c." stays "a, b en c."). Not a rule and not a design change.
160. **Branch and PR**: all work on the harness branch `claude/loving-davinci-3rsdh3`; the PR targets `main`.

## Milestone 12 (29 oktober 2026)

**Samenvatting (Nederlands).** Mijlpaal 12 bouwt twee dingen naast de bevroren engine, niet erin. **Deel A, de Terminal**
(`/terminal`): één live tabel van alle munten in beeld (de watchlist plus de creates van de laatste 30 minuten), gevoed door
één server-sent-events-stroom (`/api/public/stream`, één verbinding per tabblad, hartslag elke 15 s, hervatten met
`Last-Event-ID`, een update binnen een seconde na een nieuwe trade), met per munt leeftijd, naam/ticker, marktwaarde,
graduatievoortgang (SOL van 85), unieke kopers en netto SOL in 5 minuten, kopers/verkopers, grootste koper, dev-buy en
dev-verkoop, bundelvlag, makerstatistiek (B.4), thema en burst, X-vermeldingen waar gemeten, laatste trade, status en de
plays die vuren; sorteren, kolommen, dichtheid, toetsen (j/k, enter, c, f, 1 tot 5, /), presets ("nieuw < 5 min", "momentum",
"bijna gegradueerd", "organisch", "thema brandt", "makers met track record"), eigen filters in de URL en in de browser, een
volglijst en notities per bezoeker (localStorage), geluid bij een nieuwe play (standaard uit) en een Plays-paneel. Elke actie is
kopiëren of een link in een nieuw tabblad naar pump.fun, DexScreener, Solscan of een handelsterminal uit `config/web.json`
(Axiom, Photon, BullX, GMGN; de linkpatronen zijn niet handmatig geverifieerd, `docs/UNVERIFIED.md`). Er is geen koopknop,
geen wallet, geen order. **Deel B, de edge-fabriek** (`fart research`): een backtest-harnas dat vooraf vastgelegde hypotheses
toetst op onze eigen trades met de kosten die een mens betaalt, een autopsie van de organische winnaars, makerstatistieken en
minuutcandles na graduatie als nieuwe, begrensde dataverzameling, en een Plays-laag (`fart plays`, `fart-plays.service`) die
alleen **bewezen** regels uit `config/plays.json` elke tick tegen de live stroom toetst, papierposities bijhoudt, `/plays`
vult en de eigenaar waarschuwt (Telegram, soort `play`, hoogstens 6 per uur; de community pas na de go/no-go). Bij oplevering
is `config/plays.json` leeg: niets is bewezen, dus niets vuurt. De bevroren regelbestanden zijn byte-gelijk gebleven
(vingerafdruk `sha256:d45c41793c64`, de bewezen gelijkwaardige van `sha256:e3bf4320c45c`); alles van M12 leeft in eigen
modules (`fart/web/terminal.py`, `fart/research/`, `fart/plays/`, `fart/measure/creators.py`, `fart/measure/candles.py`,
`fart/measure/paper.py` → het uitvoeringsmodel) en eigen tabellen (schema v9: `creator_stats`, `pool_candles`, `candle_jobs`,
`plays`). **Het eerste echte rapport moet op de VPS draaien**: de sandbox heeft geen kopie van de database; de tests draaien
het harnas op een synthetische wereld met bekende uitkomsten.

**De vooraf vastgelegde hypotheses (B.2), getoetst in deze volgorde:** **H4** maker met track record (de maker had eerder een
graduatie of winnaar in onze data, zonder bundel en zonder vroege dev-verkoop; groepen goed / slecht / geen / neutraal);
**H5** organische vroege kopers (minstens 10 unieke kopers in de eerste 3 minuten; de drie bundeldefinities als filters: zonder
dev-bundel, zonder slot-bundel, zonder top-2-bundel, zonder elke bundel, alleen gebundeld); **H1** vroege-kopersmomentum
(N unieke kopers én M netto SOL in 5 minuten, N 15/25/40, M 3/5/10; het raster van de engine); **H3** themaleider (de eerste
of sterkste munt van een opkomend thema tegenover de copycats); **H7** dev-verkoop (wat een verkoop van de maker binnen 10
minuten voorspelt; met en zonder; verkopen zodra de maker verkoopt); **H2** graduatiemoment (munten die met netto minstens
M SOL in 5 minuten van 75 naar 85 SOL in de curve gaan, M 5/10/20, houdtijd 5/10/15 minuten; de instap is de tick waarop de
curve 75 SOL passeert, en alleen die); **H6** na graduatie (volume in het eerste half uur op PumpSwap boven 10k/25k/50k USD).
Samen 145 varianten (instap × uitstap); elke variant wordt geteld en gerapporteerd, en het rapport zegt hoeveel er bij louter
toeval een positieve ondergrens zouden tonen (de multipliciteitsnoot). Uitstappen: vaste doelen +40/60/100% met halve
verkoop en trailing voor de rest, trailing 20/30/50% onder de piek, tijdstop 5/10/15/60 minuten, harde stop −35%, verkoop bij
graduatie, verkoop bij de eerste dev-verkoop, uiterlijk na 24 uur.

**Bewezen** betekent, en niets minder: de variant is gekozen op de **keuzehelft** (de eerste helft van de dagen: de hoogste
verwachte waarde onder de varianten met minstens 20 gevallen daar) en op de **toetshelft** (de tweede helft) is de ondergrens
van het 95%-interval (blokbootstrap met de UTC-dagen als blokken, 9.999 trekkingen, vaste seed 20261029) van de **verwachte
waarde na kosten** groter dan 0, met minstens **100 gevallen**, een aandeel **onbekende** uitkomsten onder **20%** en een
mediane **lead time** (signaal tot piek binnen 24 uur) van minstens **2 minuten**. Onbekend (het prijspad eindigt vóór een
uitstap) en geen vulling (geen trade binnen 10 minuten na het signaal) tellen apart en nooit als resultaat. De instap vult
20 s na het signaal (ook 5/60/120 s als gevoeligheid) op de eerste trade daarna tegen de curvequote van 0,5 SOL (ook 0,1 en
1 SOL); kosten: curvefee 1% per kant, priority fee 0,0005 SOL per transactie, de prijsimpact van de curve (0,5 SOL verplaatst
een verse curve 3,3%); na graduatie de PumpSwap-fee 0,25% en de impact uit de liquiditeit van DexScreener (zonder
liquiditeit: 85 SOL aangenomen en zo gemarkeerd); de uitstap via `sell_quote` zolang er trades zijn, daarna via
`mcap_samples` of minuutcandles. Per variant: verwachte waarde in SOL en %, trefkans, mediaan/p10/p90, maximale drawdown, n,
het interval, de lead time, de capaciteit (SOL tot 5% impact), de gevoeligheid voor de instapvertraging en de grootte, het
aandeel onbekend, en de conditionering op tijd van de dag (piek 13:00 tot 02:00 UTC), SOL-trend en munten per uur.
Promotie (`fart research promote <H> <variant>`) weigert een onbewezen variant, een andere variant dan de vooraf gekozen en
een hypothese zonder rapport; een promotie schrijft een nieuwe regelversie en zet de vorige van dezelfde hypothese op
inactief.

**Wat verder vastligt.** De Terminal leest alleen bestaande tabellen (`watchlist`, `coins`, `trades`, `decisions`, `outcomes`,
kv `engine.metas_latest` en `sol_usd.cache`, `x_keys`, `creator_stats`, `plays`) via één feed-thread met één berekening voor
alle kijkers; de rij per munt herberekent de engine-definities (unieke kopers, netto SOL, grootste koper, bundel) in het
geheugen uit dezelfde trades, zonder de engine aan te raken. Onbekend blijft onbekend: zonder trade geen marktwaarde, zonder
genoeg kopen geen bundeloordeel, zonder eerdere munt geen makercijfer, zonder meting geen X-aantal, zonder SOL-prijs geen
dollar. De dataverzameling van M12 is begrensd en zichtbaar in `fart status`: makerstatistieken per stap hoogstens 2.000 makers
met cursors en een backlog-vlag (hoogstens 50 munten per maker gescand); candles hoogstens 20 per run, één uur na graduatie,
achter de budgetpoort `geckoterminal` (30 per minuut, gedeeld met de tracker), met wachtrij, pogingen en backlog; het
Alchemy-dagplafond blijft 3.000.000 CU en M12 doet geen enkele RPC-aanroep. De retentie van `trades` gaat van 14 naar 30 dagen
voor het harnas; `fart status` schat de schijfgroei (180 bytes per rij, een schatting om te kalibreren) en waarschuwt onder
20 GB vrij; de VPS-schijf is voor 91% vol door andere projecten, dus die regel is geen versiering. De webdienst blijft
alleen-lezen; er komt geen nieuwe naam in `.env`.

161. **Everything of milestone 12 lives next to the engine, not in it** (`fart/web/terminal.py`, `fart/research/`,
    `fart/plays/`, `fart/measure/creators.py`, `fart/measure/candles.py`, the execution model appended to
    `fart/measure/paper.py`; schema v9 adds `creator_stats`, `pool_candles`, `candle_jobs` and `plays`): the frozen rule files
    of `fart/version.py` → `RULE_FILES` are byte-identical, the fingerprint test of milestone 5 still pins
    `sha256:d45c41793c64` as the registered equivalent of the frozen `sha256:e3bf4320c45c`, and the engine's tables are only
    read. The terminal and the research code import the rule modules read-only (`curve.py` for the quotes, `meta.py` for the
    tokenizer, `decide.py` nowhere) and never write a decision, a watchlist row or a kv the engine reads.
162. **One execution model for research and live plays** (`paper.Position`, `PricePoint`, `ExitRules`, `Costs`,
    `pool_sell`, `pool_buy`, `capacity_sol`): the harness and the plays daemon run the same code point by point, so a promoted
    rule behaves live exactly as it was measured. A position fills at the first point at least `entry_delay_s` after the
    signal (the quote of `buy_quote` on the curve: the fee and the impact of that size), waits at most 600 s for a point (then
    no fill), and sells at the first point at least `exit_delay_s` after its trigger (a human needs the same seconds to act on
    an exit as on an entry). Trigger order per point: dev sell, graduation, hard stop, target (partial), trailing stop, time
    stop, max hold. After the graduation point the venue is the pool: PumpSwap fee 0.25% and a constant-product impact on the
    liquidity of the sample (`assumed_85_sol` when none is known, flagged in the fill). The state serializes to JSON
    (`to_json`/`from_json`) so the plays daemon stores it per tick and continues to the same result.
163. **Features are replayed from `trades` in memory, not read from `features_asof`** (`research/features.py::replay_features`,
    `tick_times`): the engine only stores feature snapshots at decisions, so the harness rebuilds the engine's momentum
    definitions (unique buyers, net SOL, buys, sells for 1/5/15 min, largest buyer share over 15 min, trades total, curve
    state, market cap, graduation progress) at 15 s ticks from the create, as-of each tick (`knowable`: event time and
    `received_at` both at or before the tick). The same function feeds the live terminal rows and the plays daemon's contexts.
164. **Three bundle definitions, each True / False / None, never a guess** (`features.bundle_flags`): the dev buy above 5% of
    the 85 graduation SOL; at least three buyers in the create's slot buying together more than 15% of it; two wallets with
    more than 40% of the buy SOL in the first ten trades, judged only once those trades hold at least five buys (two buys are
    always above 40%). The aggregate is True when any flag is True, False only when the dev buy is known and the top-2 verdict
    is False (a create without a known slot cannot have a slot flag; that absence does not block the verdict), otherwise None.
    A coin with three trades stays "onbekend" forever rather than "gebundeld"; the terminal's "organisch" preset requires an
    explicit False. `dev_sell` gives the creator's first sell, whether it fell within 10 minutes, and the fraction sold.
165. **Walk-forward with one pre-chosen variant** (`harness.Harness.split_days`, `evaluate`, `proven_verdict`): the days are
    split in two halves (the first half plus one when odd); on the selection half the variant with the highest expected value
    among those with at least `min_select_cases` (20) is chosen; only that variant's test-half figures count as the test. Every
    variant's full, selection and test metrics are reported anyway, with a multiplicity note that says how many of them would
    show a positive lower bound by chance (`variants × alpha / 2`). "Bewezen" is the conjunction of four criteria on the
    test half: the lower bound of the 95% block bootstrap of the mean pnl after costs above 0, at least 100 cases, an unknown
    share under 20%, and a median lead time of at least 120 s. The bootstrap resamples UTC days with replacement, 9,999 draws,
    seed 20261029 (`bootstrap_mean`; the same seed gives the same interval, a test proves it).
166. **Unknown and no fill are counted, never scored** (`Case.status` closed | unknown | no_fill; `metrics`): a signal whose
    price path ends before an exit (a dead coin, the end of the data) is unknown and lands in the unknown share; a signal
    without a trade to fill on within 600 s is no fill and lands apart; neither enters the expected value, the hit rate, the
    quantiles or the drawdown, and `signals == cases + unknown + no_fill` holds everywhere (a test checks it per half). A
    position's `pnl_sol` is None while it is open: the plays page shows "onbekend", never a mark-to-market as a result.
167. **Costs as the human pays them, and a capacity per signal** (`RunConfig.from_settings`, `config/measure.json` →
    `research`): curve fee 1% per side (the frozen `trade_fee_bps`), priority fee 0.0005 SOL per transaction (each fill counts
    one), the curve's price impact of the size (0.5 SOL moves a fresh 30 SOL curve by 3.3%), after graduation the PumpSwap fee
    0.25% and the pool impact from DexScreener's liquidity (half the USD liquidity as the SOL side). The capacity of a signal
    is the SOL that moves the curve's spot by 5% at the signal (`capacity_sol`; 0.74 SOL on a fresh curve); the report shows
    its median per variant so a rule's size is a measurement, not a wish. The sensitivity section repeats the chosen variant
    with entry delays 5/20/60/120 s and sizes 0.1/0.5/1 SOL.
168. **H2 fires on the observed crossing only** (`hypotheses._h2_fires`, `H2_CROSS_SOL` = 75 real SOL in the curve, the
    virtual reserve minus 30): the previous tick under 75 and this one at or above, once per coin; a coin first seen above 75
    (the engine watched it late) never fires, in the harness and live alike, so the live layer cannot buy what the backtest
    never saw. The candidates of H2 are the coins whose virtual reserve passed 105 SOL inside the window (an index range on
    `trades`), not every coin.
169. **Promotion is a gate, not a copy** (`report.promote`, `PromoteError`): it refuses an unknown variant, a variant that is
    not the report's pre-chosen one (promoting another variant is choosing on the test half), an unproven pre-chosen variant
    (the failed criteria named), and a hypothesis without a report; it writes a rule `<H>-v<n>` with the test-half figures,
    the exit rules and their Dutch description, the report name and the promotion time into `config/plays.json`, and sets the
    previous active rule of the same hypothesis to inactive with `superseded_by`. `config/plays.json` ships empty.
170. **The plays layer is its own daemon** (`fart/plays/service.py::PlaysEngine`, `fart plays [--once]`,
    `deploy/fart-plays.service`): every 15 s it loads the active rules, takes the candidates per family (the watchlist and the
    recent creates; for H6 the recent graduations), replays the features as-of now, keeps the previous tick's features for
    the crossing predicates, opens one play per (rule, version, mint) with the signal's features and entry state, feeds every
    open position the price points that arrived since its last one, closes as unknown 24 h and 10 min after the signal without
    an exit and as no fill 10 min after the entry delay without a trade, and enqueues the alerts: kind `play`, dedupe
    `play:<id>`, the owner chat only, at most `alerts.max_per_hour` (6) in a sliding hour; a play over the bound is marked
    alerted and counted (`plays.alerts_over_limit`), never sent late. The message names the rule, its measured hit rate and
    expected value, the coin, the signal, the entry state, the exit rule and our own Terminal page; no venue link, no buy
    word. Heartbeat and the last tick's summary are in kv (`plays.heartbeat`, `plays.rules_active`, `plays.last_tick`) for
    `fart status` and the pages. The community chat is not a destination until the go/no-go.
171. **The terminal is a feed with one computation for every viewer** (`terminal.TerminalFeed`): one thread cycles every
    second (`stream_poll_s`), reads the new `trades` rows by `received_at` from 5 s behind its cursor (a row whose transaction
    committed after a cycle had passed its received_at still arrives; a seen set of (mint, signature) keeps it from counting
    twice; `trades` is WITHOUT ROWID, so there is no rowid cursor), keeps per coin the last 16 minutes of trades, the first ten,
    the creator's buys and sells and the last curve state, recomputes every dirty row at once and every row every 5 s or when
    the engine's heartbeat moved (the sliding windows age), and emits `rows` deltas (`upd`, `del`, `n`), a `snapshot` at most
    every 300 s, `plays` when the firing plays change and a `heartbeat` every 15 s (engine age, coins, viewers). The last 400
    delta events form a ring: a `Last-Event-ID` inside it gets the missed deltas, outside it a fresh snapshot. The universe
    is the watchlist, the creates of the last 30 minutes and the mints the open connections follow (`?follow=`, at most 50
    each); at most 800 rows; at most 64 connections (a 65th gets 503 with Retry-After 30); a client 200 events behind gets
    `event: dropped` and is disconnected. `/api/public/terminal` is the same snapshot as JSON (max-age 1) for a browser
    without EventSource and for the tests.
172. **Server-sent events over our own HTTP/1.1 server** (`server.StreamResponse`, `Handler._stream`): the response is
    chunked, each event its own chunk flushed at once, `Cache-Control: no-cache, no-store` and `X-Accel-Buffering: no`, a
    `: keep-alive` comment when nothing happened for the heartbeat interval plus 5 s, and the handler checks its socket every
    second (`select` plus a peek) so a closed tab is unsubscribed within seconds instead of at the next write. The Caddy
    site file compresses only the listed content types (an `encode` response matcher; `text/event-stream` is not among them)
    and proxies `/api/public/stream` with `flush_interval -1`; both are Caddy 2.6 core directives (the allow-list test of
    milestone 8 knows them). Through Caddy the first event must arrive within a second: `docs/UNVERIFIED.md`.
173. **The browser builds every cell with DOM calls and keeps the content-security policy** (`static/terminal.js`): no HTML
    is built from data (a test forbids innerHTML, insertAdjacentHTML, document.write, eval), the page keeps
    `default-src 'self'` with no inline script or style (the progress bar's width is set through the CSSOM), the configuration
    (terminals, presets, columns, link patterns, limits) travels in a `data-config` attribute as escaped JSON, every external
    link opens with `noopener noreferrer nofollow` in a new tab, the copy action uses the clipboard API with a textarea
    fallback. One tab is one EventSource; a changed follow list reconnects with `?follow=` and `?last=`; a hidden tab closes
    its stream after 90 s and resumes when shown; without EventSource, or while the stream fails, the snapshot is polled every
    3 s and the pill says so. Filters live in the URL (`?preset=`, `?q=`, `?maxage=` ... shareable) and, on request, in
    localStorage; follows, notes, columns, density, the sound switch and the sort order live in localStorage only (per
    visitor, never on the server). The ages tick client-side from the server's clock offset, so the table moves between
    events without a request.
174. **The public vocabulary grows by one measured word** (`pages.terminal_page`, `pages.plays_page`, `public.plays_view`,
    `tests/test_m12_terminal.py`): the Terminal and Plays pages may say "signaal" and "trefkans" because a play is a measured
    rule with its hit rate and expected value after costs next to it, but never "winst", "rendement", "voorspelling" or a buy
    word (the m9 word test still holds for the older pages, and the new pages are tested against the same owner lists); the
    public JSON keeps the owner-key rule (the plays layer's state is `layer`, the stream interval `pulse_s`). The tools page
    keeps its "geen koopknoppen" sentence next to the links to the Terminal and Plays.
175. **Creator statistics and candles are bounded steps of the track timer** (`measure/creators.py`, `measure/candles.py`,
    `outcomes.run_extra_steps`): `creator_stats` is recomputed per creator when a coin, graduation or outcome of theirs arrives
    (three cursors in kv, at most 2,000 creators per run and 60 s of work, a backlog flag when either bound hit, at most 50 of a
    creator's coins scanned for the bundle and dev-sell flags), with a track record good / bad / neutral / none that the
    terminal shows and H4 uses through `creator_history` (as-of, so the harness never sees a later coin). `pool_candles`
    holds the GeckoTerminal 1-minute candles of the hour after each graduation, fetched once per graduation by a job queue
    (`candle_jobs`: due an hour after the migration, at most 20 fetches per run, 3 attempts, the error kept per job, the gate
    `geckoterminal` asked first and a refusal counted). Both steps run isolated after the outcomes: a failure is logged and
    recorded (`measure.creator_stats_failed`, `measure.candles_failed`) and never stops the tracker. Both are lines in
    `fart status`.
176. **Retention 30 days and the disk line** (`config/ingest.json`, `cli.disk_status`, `DISK_WARN_FREE_GB`, `TRADE_ROW_BYTES`):
    the harness needs a month of trade paths, so `trades_days` goes from 14 to 30 (the retention test moved its fixture to 35
    days old); `pool_candles` are pruned after 60 days into the archive like the other pruned rows. `fart status` shows the
    free space of the data disk, the database size, the trades rows, the rows of the last 24 hours and the projection at the
    retention (rows × days × 180 bytes, an estimate to calibrate on the VPS) and warns under 20 GB free, because the shared
    disk is at 91%.
177. **Branch and PR**: all work on the branch `claude/magical-lovelace-vqukei`; part B (schema, research, plays, data steps)
    and part A (the Terminal) are two commits in that order because the Terminal shows the plays and the maker statistics;
    the PR targets `main`.

## Milestone 13 (31 oktober 2026)

**Samenvatting (Nederlands).** Mijlpaal 13 maakt elk signaal meetbaar. De eerste echte onderzoeksrun op de VPS (31 oktober
2026, 09:48 tot 09:58 UTC, venster 24 tot en met 30 oktober, 224.658 munten waarvan 53.711 met trades) bewees niets, en de
cijfers zeiden waarom: van de signalen van H4 bleef 83,5% onbekend, van H5 54,5%, van H7 59,9%, van H3 96,7%; alleen H6 (na
graduatie) had 0% onbekend. Het prijspad brak af zodra de trade-stream een munt losliet (`rpc_trades_cap` 150, watchlist 200
met 100 proeftijdplekken; op 31 oktober sloot de stream 2.379 abonnementen om `stream_cap`), en een positie zonder punt tot de
uitstap is onbekend. De bekende gevallen waren daardoor een overleversselectie: de munt bleef handelen, dus bleef gevolgd. De
tracker haalde zijn onderwerpen niet (600 per run tegenover ongeveer 1.100 in het venster; dekking 29 tot 34% van de
graduaties). En de autopsie vond 0 organische winnaars en 266 gebundelde, met een mediane leeftijd bij graduatie van 0,00
minuten: vóór de graduatie was er voor een mens niets te doen, ná de graduatie wel, en daar is de data compleet.

**Wat M13 bouwt, naast de bevroren engine.** (1) Het **volgen van signalen** (`fart/plays/follow.py`, een stap van de
plays-daemon): elke munt die op het ruimste niveau een instapvoorwaarde van een hypothesefamilie haalt
(`fart/research/hypotheses.py` → `FOLLOW_TRIGGERS`: minstens 10 unieke kopers in 5 minuten voor H1/H4/H5/H7, een thema-munt
met trades voor H3, een curve van 60 SOL voor H2), elke munt waarop een actieve play-regel vuurt en elke munt met een
observe-beslissing van de engine wordt 24 uur gevolgd (tabel `followed`). Buiten de trade-stream en vóór de graduatie wordt
het BondingCurve-account gepolld via `getMultipleAccounts` (hoogstens 100 adressen per aanroep, 20 CU per aanroep volgens de
Alchemy-pagina van 31 oktober 2026), elke 60 s in de eerste 2 uur na het eerste signaal en daarna elke 300 s, gedecodeerd
tegen de IDL (`fart/chain/bonding_curve.py`) en opgeslagen in `path_points` (bron `poll`). Een poll die `complete` ziet is het
graduatiepunt als de stream de migratie miste. Na graduatie geven de tracker en de candles de punten. Begrenzingen: een eigen
dagaandeel van de CU-cap (0,10 van 3.000.000 CU; daarboven geweigerd en als `follow_daily_share` in het ledger geboekt),
hoogstens 1.500 gelijktijdig gepollde munten (nieuwste signaal eerst), een degradatiestand die het snelle interval verlengt
(60 → 120 → 300 s) als het uurvenster van het aandeel wordt overschreden of de poort weigert, het trage interval voor een
curve waarvan de reserves 2 uur niet veranderden, en `closed` voor een account dat ontbreekt of niet te decoderen is. Een poll
faalt nooit stil: aanroepen, weigeringen, fouten en ontbrekende accounts staan per dag in kv en in `fart status` (regel
`follow:`). (2) **Trackercapaciteit**: `max_mints_per_run` 600 → 2.000, de DexScreener-poort 100 → 200 per minuut (officieel
300), nieuwste onderwerp eerst, een tijdsbudget per stap binnen de `TimeoutStartSec` van 240 s van de unit (sampling 150 s, de
hele run 220 s; de makerstatistieken en de candles krijgen wat overblijft), de duur per stap in kv `measure.track.last` en in
`fart status`, met de dekking van de graduaties van de laatste 24 uur ernaast. Doel ≥ 90%; `method_version` m1.0 blijft (meer
samples is trouw aan de methode, geen methodewijziging, het M5-precedent). (3) **Het harnas en de plays-laag lezen de
polls**: `price_path()` neemt poll-punten op als `PricePoint(kind="poll")` met de reserves; de curve is altijd tegenpartij, dus
een instap of uitstap mag erop vullen; een dev-verkoop is alleen op een trade-punt zichtbaar en `devsell`-uitstappen vuren dus
nooit op een poll (het rapport zegt dat). Elk rapport telt per variant op welke bron de instappen en uitstappen vulden
(trade / poll / sample / candle), de gevoeligheidstabel krijgt de rij "alleen trade-punten (zonder polls)", H2 ziet een
kruising van 75 SOL ook op poll-punten (gemarkeerd `curve_source`; live hetzelfde via `features.apply_poll_state`), en het
rapport splitst de signalen van vóór en ná de start van het volgen (kv `follow.started_at`): oudere signalen krijgen geen pad
met terugwerkende kracht. De betekenis van onbekend en geen vulling verandert niet. (4) De **tweede ronde hypotheses**, vooraf
vastgelegd in volgorde van verwachting: **H8** Dev houdt vast na graduatie (instap N minuten na graduatie, N 2/5/10, als de
maker op de curve niet verkocht, de prijs boven de graduatieprijs staat en het volume sinds de graduatie ≥ $10.000/$25.000;
tegenproef: de maker verkocht wél; twaalf varianten), **H9** Terugval na graduatie (≥ 20/30/40% onder de piek sinds de
graduatie binnen 30 minuten, volume van de laatste 5 minuten ≥ $2.500; uitstap tr20/tr30/ts15/ts60; twaalf varianten), **H10**
Themaleider na graduatie (de eerst gegradueerde munt van een brandend thema tegenover de kopieën, instap 2 minuten na
graduatie, uitstap tr30/ts60; vier varianten). Samen 28 varianten erbij (173 in totaal), alle geteld in de multipliciteitsnoot.
De autopsie krijgt een deel "na graduatie". De zeven bestaande hypotheses zijn onveranderd. (5) Een **wekelijkse onderzoeksrun**
(`deploy/fart-research.timer`, maandag 07:00 UTC, `fart research run --hypothesis all --days 30` en `fart research autopsy
--days 30`; zolang er minder dagen zijn: zoveel als er zijn, en het rapport zegt dat), de onderzoeksstand per hypothese op
`/plays`, de volledige `fart research list` en een volgkaart op `/eigenaar`, en in de Terminal per munt of hij gevolgd wordt
en de bron van het laatste punt. Schema v10 (`followed`, `path_points`) is additief; de bevroren regelbestanden zijn byte-gelijk
(vingerafdruk `sha256:d45c41793c64`); geen wallet, geen order; geen nieuwe `.env`-naam (de polls gebruiken de bestaande
`RPC_HTTP_URL`); de webdienst blijft alleen-lezen.

178. **Elk signaal krijgt 24 uur prijspad uit de goedkoopste toereikende bron** (`fart/plays/follow.py`, tabel `followed`,
    `path_points`): (a) de trade-stream zolang die het abonnement heeft (ongewijzigd; een munt met een open abonnement krijgt
    alleen een waakpoll elke 300 s, zodat een tijdstop ook vult als de trades stoppen), (b) daarbuiten en vóór graduatie polls
    van het BondingCurve-account elke 60 s in de eerste 2 uur en elke 300 s daarna, (c) na graduatie de samples van de tracker
    en de candles. De reden is de meting van 31 oktober: het onbekend-aandeel van de curvefase-hypotheses lag tussen 54,5 en
    96,7% omdat het pad afbrak zodra de stream losliet; een pad dat eindigt vóór een uitstap blijft onbekend, en dat moet zo,
    dus moet het pad langer worden. Een poll kost 20 CU voor honderd munten en de curve heeft altijd een prijs; dat is de
    goedkoopste bron die er is.
179. **De triggers liggen vast naast de hypotheses, niet in de daemon** (`hypotheses.FOLLOW_TRIGGERS`): het ruimste niveau van
    elke familie (10 unieke kopers in 5 minuten voor H1/H4/H5/H7, een thema-munt met trades voor H3, 60 SOL in de curve voor H2),
    plus elke munt waarop een regel vuurt en elke observe-beslissing. Zo bepaalt de daemon elke tick op dezelfde `Ctx` wat
    gemeten wordt, en nooit wat gehandeld wordt. H4's eigen instap (3 trades in 5 minuten) is bewust niet de trigger: die zou
    39.000 munten per dag volgen; H4 wordt beoordeeld op de munten die de kopersdrempel haalden.
180. **Het BondingCurve-account wordt gelezen zoals de IDL het geeft** (`fart/chain/bonding_curve.py`): negentien velden in de
    volgorde van `idl/pump.json` van 31 oktober 2026 (de SOL-kant heet daar `quote`: `virtual_quote_reserves`,
    `real_quote_reserves`), tolerant tot de bytes op zijn, nooit geraden. Gecontroleerd in de sandbox tegen een echt account
    naast de laatste TradeEvent van dezelfde munt (de reserves gelijk, `tests/fixtures/bonding_curve_accounts.json`) en tegen
    een gegradueerd account (`complete` true, alle reserves nul: het graduatiepunt krijgt de tijd van de poll en de prijs van de
    laatste bekende stand, nooit de nullen). Het veld `quote_mint` is op de echte accounts de nulsleutel (de standaard: SOL);
    WSOL geldt ook als SOL; een andere quote-munt wordt nooit als SOL-punt opgeslagen.
181. **Een eigen dagaandeel, een bovengrens en degradatie in plaats van stoppen** (`config/measure.json` → `follow`): 0,10 van de
    dagcap per aanroep geboekt onder `getMultipleAccounts` (de ledgerrij per aanroep); boven het aandeel wordt elke aanroep
    geweigerd en als `follow_daily_share` geboekt; hoogstens 1.500 munten per tick, nieuwste signaal eerst, de rest telt als
    `over_cap`; als het laatste uur het uurvenster van het aandeel (12.500 CU, 625 aanroepen) overschrijdt of de poort weigert,
    gaat het snelle interval een stap omhoog (60 → 120 → 300 s, hoogstens één stap per 5 minuten) en onder de helft van het
    venster weer omlaag (hoogstens één stap per 15 minuten); een curve waarvan de reserves 2 uur niet veranderden gaat naar
    300 s; een account dat drie polls ontbreekt of niet te decoderen is sluit de rij met de reden. Bij 1.500 munten op 60 s zou
    de stap 432.000 CU per dag kosten; met het aandeel landt hij op 120 s.
182. **De polls vragen de gekeyde RPC** (`follow.require_keyed_rpc`): met de publieke fallback (geen `RPC_HTTP_URL`) worden de
    rijen wél geschreven maar wordt niets gepolld, en de reden staat in `fart status` en in `plays.started`; dezelfde regel als
    de trade-stream van M3. De tests injecteren een nep-RPC door de echte budgetpoort.
183. **De tracker samplet elk onderwerp, nieuwste eerst, binnen een tijdsbudget** (`fart/measure/outcomes.py`,
    `fart/measure/prices.py`): cap 2.000 bij ongeveer 1.100 onderwerpen, de poort 200 per minuut (67 verzoeken van 30 munten
    duren ongeveer 40 s), `sampling.time_budget_s` 150 en `tracker.run_budget_s` 220 tegenover de 240 s van de unit; de munten
    die het budget niet haalt tellen als `time_budget` in `measure.track.skipped` (en de reden in `.last`), de makerstatistieken
    krijgen hoogstens 60 s van wat overblijft, de candles de rest (een poll die niet meer past wacht op de volgende run en
    telt). Elke run schrijft de duur per stap in kv `measure.track.last`; `fart status` toont die naast de dekking van de
    graduaties van de laatste 24 uur (de regel van de rapporten: een sample minstens elke 10 minuten). Meer samples onder
    dezelfde regels is `method_version` m1.0.
184. **Het harnas vult op poll-punten, telt de bron van elke vulling en laat zien wat de polls veranderen**
    (`paper.PricePoint` kind `poll`, `Fill.point_kind`, `Case.entry_kind` / `exit_kind`, `metrics()["fill_sources"]`,
    `Harness.sensitivity()["sources"]`, `Harness.follow_split()`): een instap of uitstap mag op een poll vullen (de curve is
    tegenpartij, de quote is dezelfde `buy_quote` / `sell_quote`), een dev-verkoop is alleen op een trade-punt zichtbaar en
    vuurt dus nooit op een poll (het rapport zegt dat bij elke variant). Elk rapport toont per variant het aandeel van de
    instappen en uitstappen per bron, de gevoeligheidstabel krijgt de rij "alleen trade-punten (zonder polls)" (dezelfde
    signalen op het pad van de stroom alleen: zonder polls zijn de cijfers voor trade-punten exact die van M12, een test bewijst
    het), en de signalen worden gesplitst in vóór en ná `follow.started_at`. De uitvoeringsregels van M12 zijn onveranderd: een
    uitstap wordt gezien op het punt dat haar toont en telt haar vertraging vanaf dat punt; op een raster van 60 s landt een
    tijdstop daardoor hoogstens twee intervallen na het moment dat volledige trades getoond hadden (de test op de synthetische
    wereld laat 120 s toe). Een geprobeerde variant die de tijdstop op haar bekende moment liet vuren veranderde acht uitkomsten
    van de M12-lab en is daarom niet doorgegaan: de betekenis van onbekend mag niet verschuiven.
185. **H2 ziet de kruising ook op polls, in het harnas en live hetzelfde** (`features.apply_poll_state`, `Harness._features_at`,
    `PlaysEngine.context`): de curvestand van een tick komt van de nieuwste van de laatste trade en de laatste poll
    (`curve_source` trade | poll), de vensterkenmerken (kopers, SOL, aantallen, grootste koper) blijven wat de trades zeggen,
    want een poll ziet geen trade. De ticks van H2 lopen door tot het laatste trade- óf poll-punt, en de kandidaten van H2
    komen uit de trades én uit `path_points` boven 105 SOL virtueel.
186. **De tweede ronde toetst na graduatie, waar het pad compleet is** (`hypotheses.H8`, `H9`, `H10`, `Harness._signals_post_grad`,
    `theme_grad_roles`): één tick per minuut na de graduatie op het pad van candles en samples; de prijs op een tick is het
    laatste punt op of vóór de tick (nooit geïnterpoleerd), het volume sinds de graduatie de som van de candlevolumes van de
    verstreken minuten (kenbaar op de tick) of anders het uurvolume van de laatste sample, het 5-minutenvolume alleen uit
    candles (zonder candles geen H9-signaal, het rapport zegt het), "de maker heeft niet verkocht" is: geen verkoop van de
    maker-wallet op de curve tot de graduatie (verkopen op de pool ziet de radar niet; dat staat in H8's noten en in de
    autopsie als "niet gemeten"). H8's raster is twaalf varianten: N 2/5/10 bij $10k met tr30 en ts60, bij N 5 ook de ladder en
    ts30, N 5 bij $25k, en de tegenproef bij N 5 / $10k; H9: X 20/30/40% × tr20/tr30/ts15/ts60 bij één Y van $2.500 (een pool die
    nog leeft); H10: leider tegenover kopie bij graduatie + 2 minuten, naar graduatievolgorde binnen de munten van het thema. De
    nieuwe uitstap `ts30` (tijdstop 30 min) komt erbij; de zeven hypotheses van M12 veranderen niet.
187. **De wekelijkse run neemt wat er is en zegt dat** (`deploy/fart-research.service`, `.timer`, `cli.research_window`): maandag
    07:00 UTC, `--days 30`; met een jongere database wordt het venster beperkt tot de dagen met trades en staat dat boven in het
    rapport ("Gevraagd venster 30 dagen; de database heeft trades vanaf …"). De run schrijft per hypothese een samenvatting in
    `out/research/latest.json` (gekozen variant, oordeel, onbekend-aandeel, datum, het onbekend-aandeel ná het volgen), zodat
    `/plays` en `/eigenaar` de stand lezen zonder de rapport-JSON met duizenden gevallen te openen. De timer verstuurt niets en
    promoveert niets.
188. **Zichtbaarheid zonder nieuwe woorden**: `/plays` toont onder de regels de onderzoeksstand per hypothese (ronde 1 en 2),
    `/eigenaar` de volledige `fart research list` en de volgkaart (gevolgde munten, polls, CU tegenover het dagaandeel,
    degradatiestand, laatste punt), de Terminal per munt "gevolgd" met de redenen en "laatste punt" met de bron (trade / poll /
    sample / candle). De openbare JSON houdt de eigenaarsregel (de test van M9 draait over de nieuwe velden); de pagina's zeggen
    geen nieuw koopwoord. Photon en GMGN zijn op 31 oktober in een browser bevestigd (`verified: true` in `config/web.json`);
    Axiom (botcontrole) en BullX (Telegram-login) blijven onbevestigd.
189. **Branch en PR**: alle werk op de branch `claude/compassionate-gates-zj3lle`; de meting eerst (schema, decoder, tracker,
    volgen, harnas en plays op de polls) en de tweede ronde daarna (H8 tot en met H10, de autopsie na graduatie, de wekelijkse
    run en de pagina's) in twee commits; de PR richt zich op `main`. Klaar is: PR open, tests groen, en binnen 48 uur na de
    uitrol een onderzoeksrun waarin het onbekend-aandeel van de curvefase-hypotheses onder 20% ligt voor signalen van ná de
    uitrol.

