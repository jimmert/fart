# Unverified assumptions (verify at deploy, then tick off)

The build environment had no outbound network (the sandbox proxy refused every data source), so
everything below is implemented from documentation and memory, made configurable, and tested
against synthetic fixtures. Each item names the file to adjust when reality differs.

## Chain / pump.fun

- [~] **Event byte layouts** of CreateEvent, TradeEvent, CompletePumpAmmMigrationEvent,
  CompleteEvent (`fart/chain/events.py` LAYOUTS; `docs/PUMPFUN-EVENTS.md`). They decode on mainnet
  (VPS, 26 Oct 2026: creates and migrations flow in through the RPC stream); the byte-level proof of
  step 3 in `docs/PUMPFUN-EVENTS.md` is still open. Watch `decode.truncated_or_drift`.
- [ ] **pump-amm event layouts** BuyEvent / SellEvent / CreatePoolEvent (milestone 2, from the PumpSwap
  IDL as remembered). Self-check: `fart status` counter `decode.pump_amm_pool_mismatch` must stay 0
  and `trades` rows with `source='pump_amm'` must show plausible SOL amounts. Whether the pool
  reserves in the event are pre- or post-swap, and whether the quote mint is always WSOL.
- [ ] **Which address carries per-mint trade notifications**: `logsSubscribe {mentions:[bonding curve]}`
  before graduation, `{mentions:[pool]}` after. If `trades` with `source='rpc_trades'` stay empty while
  `ws_subscriptions.notifications` grows, switch `config/ingest.json` → `rpc_trades.mention` to `"mint"`.
- [ ] **Global account fields past offset 113** (`fart chain global`): withdraw_authority, enable_migrate,
  pool_migration_fee, creator_fee_basis_points, fee_recipients, set/admin_set_creator_authority are a
  guess at the newer layout; compare `creator_fee_basis_points` with `config/pumpfun.json` →
  `creator_fee_bps` (30) and `total_curve_fee_bps` (125).
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
  the protocol fee is 0.95%, not 1% (fixed). Still open: pump.fun's 2025 dynamic fee schedule
  (creator fee 0.30%?) - the paper trade uses the flat `total_curve_fee_bps` (125).
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
- [x] **`subscribeTokenTrade`** is only served to a funded API key (VPS, 26 Oct 2026). Without
  `PUMPPORTAL_API_KEY` fart no longer sends it; trades come from the RPC. With a key, whether
  pump-amm trades arrive after graduation is still open (the RPC pool subscription covers it anyway).
- [ ] **PumpPortal creates coverage**: PumpPortal delivered about a third of the creates (hour 05 UTC:
  177 vs 467 on the RPC stream). Watch `coverage` per hour; the RPC stream + sweep is the primary path.
- [ ] **One connection per IP** (the VPS IP is shared): our connect gate is 12 per hour, watchdog
  120 s, backoff 20–180 s (`config/ingest.json`, `config/budget.json`).

## Solana RPC / Alchemy

- [ ] **Compute-unit cost table** (`config/budget.json` → `rpc.cost_table`): getTransaction 59,
  getSignaturesForAddress 50, getTokenLargestAccounts 50, ws_notification 10, … are estimates.
  Reconcile `fart status` → `budget rpc cost` with the Alchemy dashboard after 24 h.
- [ ] **Whether websocket notifications are billed** and at what rate (`ws_notification`,
  `ws_subscribe`, `ws_unsubscribe` at 10 CU each are estimates). With ~150 per-mint subscriptions the
  trade stream dominates the CU spend: compare `fart status` → `rpc CU today by method` with the
  Alchemy dashboard after one day and adjust `config/budget.json`.
- [ ] **Milestone-1 estimate**: ~13,400 CU per 10 minutes (~2M CU/day) against the 3M cap before the
  trade stream; the 80% rule drops probation subscriptions first when the cap comes near.
- [ ] **EUR 50/month ≈ 3,000,000 CU/day** (`RPC_DAILY_BUDGET_CU` default). Depends on Alchemy's
  pay-as-you-go price per million CU.
- [ ] **Public RPC fallback limits** (`rpc_public`: 1 req/s, burst 4, 20k/day). The public
  endpoint may refuse `logsSubscribe` or `getSignaturesForAddress` with large limits.
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
  UTC labels when it is missing).

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
- [ ] **systemd hardening set** (`SystemCallFilter=@system-service`, `ProtectProc=invisible`, …):
  check `journalctl -u fart-ingest` for a clean start; relax the offending directive if not.
- [ ] **PumpPortal and Alchemy websockets behind the shared VPS egress**: no proxy in the way.
