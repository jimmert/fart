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
     together. Factor close to 0.0002: the table stands, tick this item with the dashboard numbers and the
     date. Factor close to 0.04: set `rpc.cu_per_byte` to 0.04 in a commit; the hourly envelope then admits
     1/200 of the frames (lower `rpc_trades_cap` / `meta_burst_max_total` or raise the cap) and the enrich
     limit is unaffected. No websocket CU at all on the dashboard: set `cu_per_byte` to 0 (the envelope and
     the 80% rule become moot for the streams) and write the date down. Anything in between: use the
     measured factor rounded to two significant digits.
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
  request, author expansion $0.010 per user per UTC day) are the previous builder's notes of 24 Oct 2026:
  compare with the developer console and with `fart status` → `x spend today` after one day.
- [ ] **Per-day deduplication** of billed posts/authors (assumed by the pricing notes; `x_dedupe`).
- [ ] **`GET /2/tweets?ids=` re-reads are free** for same-day ids (booked as `refresh` at $0).
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
