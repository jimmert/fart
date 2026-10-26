# Unverified assumptions (verify at deploy, then tick off)

The build environment had no outbound network (the sandbox proxy refused every data source), so
everything below is implemented from documentation and memory, made configurable, and tested
against synthetic fixtures. Each item names the file to adjust when reality differs.

## Chain / pump.fun

- [~] **Event byte layouts** of CreateEvent, TradeEvent, CompletePumpAmmMigrationEvent,
  CompleteEvent (`fart/chain/events.py` LAYOUTS; `docs/PUMPFUN-EVENTS.md`). Field lists taken from the
  official IDL on 26 Oct 2026 (`idl/pump.json`, with the newer trailing fields); they decode on mainnet
  (VPS, 26 Oct 2026: creates and migrations flow in through the RPC stream); the byte-level proof of
  step 3 in `docs/PUMPFUN-EVENTS.md` (a real recording decoding `full` with zero extra bytes) is still
  open. Watch `decode.truncated_or_drift`.
- [~] **pump-amm event layouts** BuyEvent / SellEvent / CreatePoolEvent: field order confirmed against
  the official `idl/pump_amm.json` (26 Oct 2026; the milestone-2 lists were the exact prefix). Still
  open: a real recording decoding `full`, whether the pool reserves in the event are pre- or post-swap.
  A pool quoted in something other than WSOL is rejected (`decode.pump_amm_quote_not_wsol`), a swap on
  a pool we cannot map to a mint too (`decode.pump_amm_pool_unknown`); both should stay small.
- [ ] **Which address carries per-mint trade notifications** before graduation:
  `logsSubscribe {mentions:[bonding curve]}`. If `trades` with `source='rpc_trades'` stay empty while
  `ws_subscriptions.notifications` grows, switch `config/ingest.json` → `rpc_trades.mention` to `"mint"`.
  (Pool subscriptions after graduation were removed in milestone 3: too expensive.)
- [x] **Global account layout** (`fart chain global`): verified against the official IDL on 26 Oct 2026
  for the whole account (1,088 bytes): the fields past offset 113 (withdraw_authority, enable_migrate,
  pool_migration_fee, `creator_fee_basis_points` at 154, fee_recipients, set/admin_set_creator_authority)
  are exactly where the VPS read them, so the 5 at offset 154 is the real creator fee: 95 + 5 = 100 bps
  = the public 1% curve fee (`config/pumpfun.json`: `creator_fee_bps` 5, `total_curve_fee_bps` 100).
  The newer fields from offset 450 on (create_v2_enabled ... max_curve_depth) decode tolerantly and are
  labelled "IDL (newer field)" until the VPS shows them.
- [ ] **pump-amm pool PDA** (`fart chain pda <mint>`): `["pool", index 0, pool-authority PDA, mint, WSOL]`
  under the pump-amm program is a guess; compare with `migrations.pool_address`. Never used blindly.
- [ ] **Events appear as `Program data:` log lines** (not only as self-CPI inner instructions). If
  pump.fun emits only via CPI, the `logsSubscribe` stream stores nothing decodable and the sweep +
  `getTransaction` backfill carries the RPC side alone (`fart/ingest/rpc_logs.py`).
- [ ] **`logsSubscribe {"mentions":[mint authority]}` sees every create** and
  `{"mentions":[39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg]}` every graduation
  (`config/pumpfun.json`: `mint_authority`, `migration_account`).
- [x] **Bonding curve parameters** (`config/pumpfun.json`): 30 SOL / 1.073e9 virtual tokens,
  793.1M real tokens, 1e9 supply, ~85 SOL to graduate: match the Global account (VPS, 26 Oct 2026);
  the protocol fee is 0.95% and the creator fee 0.05% (Global account + IDL layout, milestone 3), so
  the paper trade uses the flat `total_curve_fee_bps` 100. Still open: whether a per-coin
  `creator_fee_bps` (CreateEvent field 18, BondingCurve account) ever differs from the global 5 for the
  coins we measure (`creator_fee_configurable` in Global); the TradeEvent carries the fee actually charged.
- [ ] **Bundled heuristics** (graduation within 60 s, dev buy ≥ 50% of the curve) are definitions,
  not measured facts (`config/pumpfun.json`: `bundled_*`). Re-measure the "half of graduations are
  bundled" claim from the audit with the report's organic/bundled split.
- [ ] **`getTokenLargestAccounts`**: the largest account is assumed to be the curve (before
  graduation) or the AMM pool (after). `fart/engine/enrich.py` reports both including and excluding
  it.

## PumpPortal

- [ ] **Message field names** (`txType`, `solAmount`, `initialBuy`, `vSolInBondingCurve`,
  `vTokensInBondingCurve`, `marketCapSol`, `bondingCurveKey`, `traderPublicKey`, `signature`,
  `pool`, `name`, `symbol`, `uri`) and the migration message shape (`fart/ingest/store.py`).
  The live smoke test prints the key set of a real create message.
- [ ] **Migration messages with USDC / PUMP in `mint`** (VPS, 26 Oct 2026): `subscribeMigration` delivered
  messages whose `mint` field was the USDC or the PUMP mint (72,770 and 8,142 pool notifications in six
  minutes once they were on the watchlist). Since milestone 3 they are refused and kept:
  `sqlite3 /opt/fart/data/fart.db "select received_at, reason, raw_json from rejected_messages where reason like 'system_mint%' order by id desc limit 5"`.
  A migration of a coin created before fart started (`Cv934yS1kxVtW3Y6vS8jhk6APKpdtWbzpMcs6VHHpump`,
  26 Oct 2026, refused as `unknown_coin` by milestone 3) is stored since milestone 4 with
  `migrations.coin_unknown=1`, never watched, counted under `grads_without_create`; `fart status` shows
  the count. Open: whether such messages ever name a coin that is *not* older than fart (that would mean a
  missed create, which the sweep + backfill should then recover and clear the flag).
  Open: which coin those messages were about (pump.fun now supports non-SOL quote mints: `quote_mint`
  in CreateEvent/CompletePumpAmmMigrationEvent/CreatePoolEvent, `whitelisted_quote_mints` in Global;
  PumpPortal may report the quote side of such migrations), and whether another field of the message
  carries the real mint (docs/PUMPFUN-EVENTS.md, "PumpPortal migration messages").
- [x] **`subscribeTokenTrade`** is only served to a funded API key (VPS, 26 Oct 2026). Without
  `PUMPPORTAL_API_KEY` fart no longer sends it; trades come from the RPC. With a key, whether
  pump-amm trades arrive after graduation is still open (the RPC pool subscription covers it anyway).
- [ ] **PumpPortal creates coverage**: PumpPortal delivered about a third of the creates (hour 05 UTC:
  177 vs 467 on the RPC stream). Watch `coverage` per hour; the RPC stream + sweep is the primary path.
- [ ] **One connection per IP** (the VPS IP is shared): our connect gate is 12 per hour, watchdog
  120 s, backoff 20–180 s (`config/ingest.json`, `config/budget.json`).

## Solana RPC / Alchemy

- [~] **Compute-unit cost table** (`config/budget.json` → `rpc.cost_table`). Milestone 4 set the HTTP
  methods to the figures of Alchemy's compute-unit page (https://www.alchemy.com/docs/reference/compute-unit-costs,
  read by Jim on 26 Oct 2026): `getTransaction` 40, `getSignaturesForAddress` 40, `getTokenLargestAccounts`
  20, `getAccountInfo` 10 (they were 59 / 50 / 50 / 10). `getSlot` 10, `getBlockTime` 10 and
  `getMultipleAccounts` 25 were not read from the page and stay estimates. Still open: the dashboard
  comparison per method (procedure below). The enrich limit follows the table: 0.15 x 3,000,000 / 40 =
  11,250 `getTransaction` calls per day (7,627 before).
- [ ] **Websocket subscriptions are billed per byte, but at which rate.** The same page gives two
  numbers (26 Oct 2026, not reconciled): the Solana section says **0.0002 CU per byte** for Solana
  websocket subscriptions (`logsSubscribe` and the others), the section "Webhooks and Subscription APIs"
  says **0.04 CU per byte** (a factor 200). Since milestone 4 every frame of the two subscription
  connections is booked as bytes (the UTF-8 length of the frame received or sent) x `rpc.cu_per_byte`
  (0.0002, the working value; the ledger keeps the bytes next to the CU): notifications under
  `ws_notification` (creates/migrations stream) and `ws_trade_notification` (trade stream), the
  subscribe/unsubscribe requests and their answers under `ws_subscribe`, `ws_unsubscribe`,
  `ws_trade_subscribe`, `ws_trade_unsubscribe`. The flat 10 CU per notification of milestones 1-3 is gone;
  it was never a published figure. At 0.0002 CU/byte the 26 Oct volume (about 243,000 notifications of
  roughly 1 KB) is some 50,000 CU per day; at 0.04 it would be some 10,000,000 CU per day, far above the
  cap. This is the single number that decides how much of the trade stream the envelope lets through,
  and it stays unconfirmed until the dashboard says otherwise.

  **Calibration procedure (one whole UTC day of M4 running, not a partial day):**
  1. Our side, per method, with bytes:
     ```
     sudo -u fart sqlite3 /opt/fart/data/fart.db "select method, round(sum(cost)) as cu, sum(bytes) as bytes, count(*) as rows from api_ledger where source='rpc' and blocked_reason is null and ts >= strftime('%s','<day>') and ts < strftime('%s','<day>','+1 day') group by method order by 2 desc"
     ```
     (`fart status` → `rpc CU today by method` is the same for the running day; `rows` are one-minute
     batches for the websocket methods and calls for the HTTP methods.)
  2. Alchemy dashboard → Usage → the same UTC day → compute units per method for the app. Note every
     HTTP method with its call count and CU, and the websocket / subscription line(s).
  3. HTTP methods: dashboard CU / dashboard calls should equal our table value per method; our call
     counts should match the dashboard's within the rows of the minute boundaries. A different CU per call
     goes into `config/budget.json` → `rpc.cost_table` (one commit = a new `rules_version`).
  4. Websocket: factor = dashboard CU of the subscription line(s) / our bytes of all six `ws_*` methods
     together. Since milestone 6 the command does the arithmetic from the day's total:
     ```
     sudo -u fart /opt/fart/bin/fart budget calibrate --day <day> --dashboard-cu <the dashboard's CU of that UTC day, all methods>
     ```
     It prints our ledger per method, the HTTP methods at the published rates, the websocket bytes, the implied
     factor = (dashboard total - HTTP at the published rates) / bytes, the documented value it is closest to and
     the `config/budget.json` line that follows (a candidate within 15%, else the measured factor rounded to
     two significant digits); it writes nothing. Factor close to 0.0002: the table stands, tick this item with
     the dashboard numbers and the date. Factor close to 0.04: set `rpc.cu_per_byte` to 0.04 in a commit; the
     hourly envelope then admits 1/200 of the frames (lower `rpc_trades_cap` / `meta_burst_max_total` or raise
     the cap; the command says whether the trade stream's bytes still fit its daily share) and the enrich limit
     is unaffected. No websocket CU at all on the dashboard: set `cu_per_byte` to 0 (the envelope and the 80%
     rule become moot for the streams) and write the date down. Anything in between: the measured factor
     rounded to two significant digits. `config/budget.json` is not a rule file: the rules fingerprint stays.
  5. Record the dashboard figures here (day, CU per method, websocket CU, our bytes, the factor). Nothing
     is "confirmed" without the dashboard numbers next to it.
- [ ] **Milestone-1 estimate**: ~13,400 CU per 10 minutes (~2M CU/day) against the 3M cap before the
  trade stream was built on the 10-CU-per-notification assumption; superseded by the per-byte booking.
  `fart status` → `rpc CU today by method` is the current estimate; the 80% rule drops probation
  subscriptions first when the cap comes near.
- [ ] **The trade stream's daily share** (`config/ingest.json` → `rpc_trades.daily_cu_share` 0.35, hourly
  envelope 0.35 / 24 = 1.46% of the cap = 43,750 CU at 3,000,000) comes from the VPS override of 26 Oct
  2026 (`hourly_cu_fraction` 0.015 = 36% per day). Whether 35% is the right split between the trade
  stream and the rest (log stream, sweep, enrichment, engine lookups) is a question for the first full
  day with the calibrated `cu_per_byte`: the log stream's `ws_notification` bytes are the fixed cost of
  completeness and come first.
- [ ] **EUR 50/month ≈ 3,000,000 CU/day** (`RPC_DAILY_BUDGET_CU` default): Alchemy Pay As You Go is
  $0.525 per 1M CU with 30M CU per month free (pricing page, 26 Oct 2026): 90M CU per month = 60M billable
  = $31.50 plus any subscription fee of the plan. Check the invoice against the dashboard's monthly total.
- [ ] **Public RPC fallback limits** (`rpc_public`: 1 req/s, burst 4, 20k/day, no per-byte price so
  websocket frames count 1 each). The public endpoint may refuse `logsSubscribe` or
  `getSignaturesForAddress` with large limits.
- [ ] **`getSignaturesForAddress` on the mint authority** lists create transactions newest-first
  with `slot`/`blockTime`, and one transaction creates one coin (`fart/ingest/sweep.py`).

## X (Twitter) API v2

- [ ] **Prices** in `config/x.json` (search/recent $0.005 per post returned, counts/recent $0.005 per
  request, author expansion $0.010 per user per UTC day) are the previous builder's notes of 24 Oct 2026, not a
  verified price list. **Jim verifies them against the X developer console before (and again after) the token
  goes in**, as follows:
  1. console.x.com → the project / app that owns the bearer token → *Products* / *Pay-per-use* (the exact menu
     name changes; look for the per-endpoint price list and the usage page of the same app). Note, per
     endpoint, the unit X bills for and the price: `GET /2/tweets/search/recent` (per post returned? per
     request?), `GET /2/tweets/counts/recent` (per request?), the `expansions=author_id` user objects (per user?
     per day?), and `GET /2/tweets?ids=` (free re-read of the same day's ids, or billed per post?).
  2. Put the four numbers into `config/x.json` → `prices` (`post_usd`, `counts_request_usd`,
     `author_expansion_usd`, `refresh_post_usd`) in a commit (not a rule file: the fingerprint stays). A unit
     that differs from ours (a price per request where we assume per post) is a code change in
     `fart/social/x.py` (`_search` books posts and authors; `confirm` books requests), not a config change.
  3. `fart social dryrun` (milestone 6, no HTTP) then shows what the watch would cost per day at those prices
     against `X_DAILY_BUDGET_USD`, per kind, the planner's full-load ceiling (below), and the hours in which the
     envelope would defer calls or the 30% reserve or the KOL sub-cap would refuse them.
  4. After the first full day with the token (shadow mode): run
     ```
     sudo -u fart /opt/fart/bin/fart social calibrate --day <day> --console-usd <the console's USD for that UTC day>
     ```
     (milestone 7). It prints our `x_spend` rows per kind and unit (post, request, author; the refresh posts), the
     calls per kind, the gap, the uniform factor console / booked and, per component, the price that would explain the
     whole gap if only that one is wrong, each with the `config/x.json` → `prices` line it implies; within 5% the prices
     are confirmed. Which hypothesis is true follows from the console's per-endpoint breakdown, not from the command.
     A gap that no price explains means a wrong unit (per request where we assume per post): a code change in
     `fart/social/x.py`. The per-day dedupe (`x_dedupe`) and the "refresh is free" assumption below are the first
     suspects. Write the console figures here (day, per endpoint: units and USD) next to ours; nothing is "verified"
     without them.
- [ ] **Per-day deduplication** of billed posts/authors (assumed by the pricing notes; `x_dedupe`).
- [ ] **`GET /2/tweets?ids=` re-reads are free** for same-day ids (booked as `refresh` at $0).
- [ ] **`GET /2/users?ids=` bills per user object at the author-expansion price** (milestone 7: the confirmation search
  requests no `expansions` any more and looks up at most 3 authors afterwards, booked as kind `ca_authors` at
  `author_expansion_usd` per new user per UTC day). If the console bills the users endpoint per request, or differently
  from the expansion user objects, `fart social calibrate` shows it as an `author` component gap; the price is a config
  change, a per-request unit is a code change (`fart/social/x.py`, `_lookup_authors`).
- [ ] **The full-load formula of the planner** (milestone 7, `fart social dryrun` → "full load", `fart/social/dryrun.py`
  → `full_load`): with K keys a day, C counts per key, one search of P posts and A author lookups per key,

  ```
  confirmation ceiling = K x (C x counts_request_usd + P x post_usd + A x author_expansion_usd)
                       = 60 x (12 x $0.005 + 10 x $0.005 + 3 x $0.010) = 60 x $0.14 = $8.40 per day
  account watches      = mega posts x post_usd + mega accounts x author_expansion_usd (80 x $0.005 + 9 x $0.010 = $0.49)
                       + KOL at its sub-cap ($1.00; unthrottled 200 x $0.005 + 13 x $0.010 = $1.13)
  total ceiling        = $9.89 per day = 66% of X_DAILY_BUDGET_USD 15.00
  ```

  C = 12 comes from the schedule (a count at 0, 15, ..., 120 minutes after the decision, then hourly at 180, 240, 300:
  `planner.schedule_counts`), P = `confirm.search_max_results`, A = `planner.search_max_authors`, K =
  `planner.full_load_keys_per_day`. The ceiling counts every key both fully counted and searched, which the stop rules
  make impossible (a confirmed key stops counting, a refuted key never searches: a silent coin costs 4 counts = $0.02);
  the expected load with the dry-run assumptions (half the keys confirmed after four counts) is about $6.40 a day. To
  verify after the first shadow days: `fart status` → `x plan vs spend today` and the week report's "Kwaliteit per
  dollar" table should stay under the ceiling; `x_keys` holds every key with its counts, search and USD
  (`select stop_reason, count(*), round(avg(counts_n),1), round(sum(usd),2) from x_keys where stopped_at is not null group by 1`).
  The milestone-6 layer cost about $119 a day in the dry run of 26 Oct 2026 (61 keys x 96 counts, a search with author
  expansion for 10% of the counts).
- [ ] **Response field names** (`data[].public_metrics.like_count/quote_count/impression_count`,
  `includes.users[].public_metrics.followers_count`, `meta.newest_id`, counts `data[].tweet_count`,
  `meta.total_tweet_count`) come from the public docs; the fixtures under `tests/fixtures/x_*.json` are
  synthetic. Parsing is tolerant: missing fields become NULL, never 0.
- [ ] **Rate limits**: `x-rate-limit-reset` (epoch seconds) is honored as backoff on 429; the gate
  allows 0.5 calls/s (burst 5) and 5,000 calls/day for the source `x`.
- [ ] **Query operators** `from:`, `-is:reply`, `-is:retweet`, `$TICKER` cashtags and quoted exact phrases
  on the pay-per-use tier; query length <= 512 characters (the KOL batch is ~230).

## Telegram

- [ ] **Bot API shapes**: `sendMessage` returns `{"ok": true, "result": {"message_id": N}}`; a 429 carries
  `parameters.retry_after` in the body and a `Retry-After` header (both honored). Verify with
  `fart notify test` on the VPS (the first real send).
- [ ] **HTML parse mode** rejects unbalanced tags: every chain/X text is escaped; a 400 "can't parse
  entities" marks the row `gave_up` with the description (visible in `fart notify outbox`).
- [ ] **Time zone data**: `zoneinfo` needs `/usr/share/zoneinfo` (Ubuntu has it; the code falls back to
  UTC labels when it is missing). Milestone 5: the community quiet hours (02:00-08:00 Europe/Amsterdam) use
  the same lookup; without zoneinfo they would be read as UTC (two hours off in winter).
- [ ] **Community chat id and rights** (milestone 5, `TELEGRAM_COMMUNITY_CHAT_ID`, off by default): a Telegram
  supergroup or channel id is a negative number (channels and supergroups `-100...`); the bot must be a member
  (group) or an administrator with the right to post (channel) before `sendMessage` succeeds; a 400/403 marks
  the row `gave_up` with the description. Nothing was tried from the sandbox. When the day comes: set the id
  with `TELEGRAM_COMMUNITY_ENABLED=0` first, watch `fart status` → `telegram community: configured, disabled`,
  then switch the flag after the week's go/no-go.
- [ ] **Preview volume** (`TELEGRAM_COMMUNITY_PREVIEW=1`): previews count towards the owner's 6 observation
  batches per hour; on a busy day the owner may receive fewer observation batches because of them (by design,
  per the brief). Check `fart notify outbox` for `community_preview` rows and `observe_deferred` in the tick log.

## Website / Caddy (milestone 8)

The website was built and tested against its own server in the sandbox (`tests/test_m8_web.py`: a real
`ThreadingHTTPServer` on the loopback, Chromium screenshots of the pages); what the VPS's shared Caddy and systemd do
with it is implemented from documentation. Verify at the rollout (`deploy/DEPLOY.md`, "Milestone 8: website"):

- [ ] **Caddy 2.6.2 accepts the site file** (`deploy/caddy/fart.caddy`): `request_body { max_size 64KB }`, the `?`
  (default) and `-` (delete) prefixes and the `defer` subdirective of `header`, `encode zstd gzip`, `reverse_proxy`.
  All four are documented for Caddy 2.4 and later; `caddy validate --adapter caddyfile` is the proof before any reload.
  If `defer` is refused, drop that line (the `?` defaults then apply before the upstream answered and the app's own
  headers win anyway, because Caddy's `header` without `defer` runs before the proxy and the upstream's headers are
  copied afterwards; the HSTS line and `-Server` keep working).
- [ ] **Caddy's DynamicUser can read `/srv/fart/fart.caddy`** (`root:root 0644` in a `0755` directory) and really cannot
  read `/opt/fart/app/deploy/caddy/fart.caddy` (`/opt/fart` is `root:fart 0750`): that is why DEPLOY.md copies the file
  instead of importing it from the app directory. Validate as `nobody` to see what the DynamicUser sees.
- [ ] **The admin API is on** in that Caddy (`caddy reload` needs it; `admin off` in the Caddyfile means a restart, which
  interrupts every site on it for a few seconds).
- [ ] **`www.kingdegen.dev` has an A record**; without one, remove it from the site address line (Caddy would keep
  retrying the certificate for it).
- [ ] **`X-Forwarded-For` as Caddy sends it**: `reverse_proxy` appends the client IP as the last entry by default; the
  login guard and the log use that last entry when the peer is the loopback (`fart/web/server.py::_client_ip`). If this
  Caddy is configured with `trusted_proxies` or a different header policy, the IPs in `journalctl -u fart-web` show it.
- [ ] **`IPAddressDeny=any` + `IPAddressAllow=localhost`** in `fart-web.service` need systemd's cgroup BPF support
  (Ubuntu 24.04, systemd 255: yes). If the unit logs that the setting is unsupported, the service still runs (systemd
  ignores it with a warning); the loopback bind is the real guard.
- [ ] **Read-only WAL behaviour under `ReadOnlyPaths`**: with the daemons running, `-wal` and `-shm` exist and the
  read-only connection reads normally (SQLite 3.22+ opens the shm read-only when it cannot write it); with every daemon
  stopped the files are gone and the `immutable=1` fallback reads the checkpointed file. `fart/web/data.py::open_db`
  documents both; `journalctl -u fart-web` shows `web.request_failed` with `unable to open database file` if neither
  path works on this SQLite build (then start `fart-ingest`, which recreates the WAL files).
- [ ] **`/usr/share/zoneinfo`** is present on the VPS (the pages show Europe/Amsterdam times; without zoneinfo they would
  be labelled UTC twice, like the Telegram messages).

## Milestone 7

- [ ] **The planner's stop rules against real chatter**: `refuted` after three consecutive counts without growth of the
  24 h count assumes that `meta.total_tweet_count` of `counts/recent` with a 24 h `start_time` only grows for a coin
  younger than a day (posts never leave the window). Check on the first shadow days that refuted keys were really silent
  (`select key, last_n_1h, last_n_24h, peak_n_1h, counts_n from x_keys where stop_reason='refuted'`) and that confirmed
  keys were confirmed early (`counts_n` small); many `exhausted` keys mean the growth test never bites (chatter that grows
  by one post an hour) and `refute_after_flat_counts` or the growth measure should be revisited in a commit.
- [ ] **The hourly envelope defers, the hard rules refuse**: on the first shadow days `fart status` → `x planner` should
  show `deferred today` only in the morning hours (UTC 02:00 to 12:00 have weight 0.4) and `api_ledger` no
  `hourly_envelope` rows at all; `account_watch_reserve` and `daily_cost_cap` refusals should not appear with the cap at
  15.00 (the full load is 66% of it), `kol_daily_cap` refusals in the evening are expected (the KOL watch is $1.13 a day
  unthrottled against its $1.00 sub-cap).
- [ ] **Observed rates replace the assumptions** (`fart social dryrun` → "observed rates") from 5 stopped keys and 3
  searches on; compare the dry run's expected total with `x spend today` after the first full shadow day and write the
  observed confirmed share, counts per key, posts and authors per search into `config/x.json` → `dryrun` in a commit.

## Milestone 6

- [ ] **Shadow mode is the only X mode during the week** (`X_MODE` unset with a token): the engine passes
  neutral X features and no mega post whatever the social tables hold (`fart/engine/service.py`, `X_NEUTRAL`,
  pinned to `features.x_features` by a test), the social daemon writes `shadow_decisions` /
  `shadow_confirmations`. On the VPS, after the token goes in: `fart status` must say `mode shadow` and
  `daemons report social=shadow engine=shadow`, `decisions` must contain no `fast_post` row
  (`select count(*) from decisions where kind='fast_post'` stays 0) and `social_counts` must stay empty
  while `shadow_confirmations` grows. Live is a rules change (`x_live` in `config/engine.json`) after the week.
- [ ] **The dry-run assumptions** (`config/x.json` → `dryrun`: 80 mega posts/day, 200 KOL posts/day, 10% of
  the counts triggering a search) are guesses; after the first shadow day `fart social dryrun` uses the
  observed post rates of `social_posts` and says so. Replace the guesses by the observed figures in a commit.
- [ ] **The shadow outcome rule** (a shadow `fast_post` decision is a winner when one of its matched coins
  graduated within 24 h and that graduation's 24 h outcome is a winner; a coin that did not graduate within
  24 h is not a winner, as the universe is graduations) has not been exercised on real data: read the first
  week's "X-schaduw" sections and check a few shadow decisions by hand against `migrations` and `outcomes`.
- [ ] **`engine_candidates` volume**: one row per watched coin and ranked meta per day (expected a few thousand
  rows per day, pruned after 30 days). Check `select day, kind, count(*) from engine_candidates group by 1, 2`
  after the first day and the engine's `tick_s` in the journal (the UPSERTs run inside the decision write).

## Measurement (milestone 5)

- [ ] **Tracker coverage threshold** (`config/measure.json` → `tracker.max_sample_gap_s` 600): the tracker runs
  every 5 minutes, so 600 s allows one missed run; whether DexScreener lists every pump-amm pool from the first
  minutes after graduation (so the gap from the graduation to the first sample stays under 10 minutes) is to be
  read from the first week's `Dekking van de tracker` line. A low share with a healthy timer means DexScreener
  lists pools late, not that the tracker failed; the reports count those graduations as `uitkomst onbekend`.
- [ ] **Bootstrap seed and percentiles** (`week.bootstrap_ratio`): nearest-rank percentiles of 9,999 pooled
  ratios with `random.Random(20261026)`; the choice of days as blocks is documented in METING. A different
  seed gives slightly different bounds; the reported one is reproducible.
- [ ] **CU per day in the week report is the same estimate** as `fart status` and the daily report: nothing is
  calibrated until the Alchemy dashboard comparison above is done; the report says so on every costs line.

## Prices

- [ ] **DexScreener** `tokens/v1/solana/{mints}` (≤ 30 mints), fields `marketCap`, `fdv`,
  `liquidity.usd`, `volume.{h24,h6,h1}`, `txns`, `pairCreatedAt`, `dexId`; rate limit officially
  300/min, we use 100/min (`fart/measure/prices.py`, `config/budget.json`).
- [ ] **SOL/USD** from the deepest USD-quoted WSOL pair on DexScreener; GeckoTerminal
  `networks/solana/tokens/<wsol>` as fallback (`price_usd` attribute).
- [ ] **GeckoTerminal** 10 calls/min shared; `tokens/multi` and `ohlcv/minute` are not used in
  milestone 1 (peak refinement for top candidates is a milestone-2 option).

## Platform

- [ ] **Python 3.12 on the VPS**: the code targets 3.12 and avoids 3.13-only features, but the
  sandbox ran the tests on 3.13.17 only. Run `make check` on the server once (`WITH_DEV=1`).
- [~] **systemd hardening set** (`SystemCallFilter=@system-service`, `ProtectProc=invisible`, …): the
  daemons start clean on the VPS (26 Oct 2026). `~@privileged` includes `@chown`, which killed `gzip -f`
  in the backup unit (SIGSYS, exit 159); fixed in the script (`gzip -n -c`), the filter is unchanged.
  Anything else that calls `chown`/`fchown` (tar with `--same-owner`, `cp -p` as root, `install`) would
  die the same way: keep the service scripts to plain file writes.
- [ ] **PumpPortal and Alchemy websockets behind the shared VPS egress**: no proxy in the way.
