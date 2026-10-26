# Unverified assumptions (verify at deploy, then tick off)

The build environment had no outbound network (the sandbox proxy refused every data source), so
everything below is implemented from documentation and memory, made configurable, and tested
against synthetic fixtures. Each item names the file to adjust when reality differs.

## Chain / pump.fun

- [ ] **Event byte layouts** of CreateEvent, TradeEvent, CompletePumpAmmMigrationEvent,
  CompleteEvent (`fart/chain/events.py` LAYOUTS; `docs/PUMPFUN-EVENTS.md`). Discriminators follow
  the Anchor formula and match the commonly seen base64 prefixes; field order/extensions are the
  risk. Check `fart status` counters `decode.truncated_or_drift` and the `raw_undecoded` table.
- [ ] **Events appear as `Program data:` log lines** (not only as self-CPI inner instructions). If
  pump.fun emits only via CPI, the `logsSubscribe` stream stores nothing decodable and the sweep +
  `getTransaction` backfill carries the RPC side alone (`fart/ingest/rpc_logs.py`).
- [ ] **`logsSubscribe {"mentions":[mint authority]}` sees every create** and
  `{"mentions":[39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg]}` every graduation
  (`config/pumpfun.json`: `mint_authority`, `migration_account`).
- [ ] **Bonding curve parameters** (`config/pumpfun.json`): 30 SOL / 1.073e9 virtual tokens,
  793.1M real tokens, 1e9 supply, 1% fee, ~85 SOL to graduate. Compare with the pump.fun Global
  account. pump.fun changed its fee schedule in 2025 (dynamic fees, creator fee); the paper trade
  uses the flat `fee_bps`.
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
- [ ] **`subscribeTokenTrade` after graduation**: whether pump-amm trades arrive for a watched
  mint. If not, post-graduation measurement relies on DexScreener samples only (already the
  fallback).
- [ ] **Subscription message rate**: probation subscribes every new coin for 5 minutes (~1000
  subscribe/unsubscribe keys per hour, batched every 5 s). PumpPortal's tolerance is unknown;
  disable with `"probation": false` in `config/engine.json` → `watchlist` if needed.
- [ ] **One connection per IP** (the VPS IP is shared): our connect gate is 12 per hour, watchdog
  120 s, backoff 20–180 s (`config/ingest.json`, `config/budget.json`).

## Solana RPC / Alchemy

- [ ] **Compute-unit cost table** (`config/budget.json` → `rpc.cost_table`): getTransaction 59,
  getSignaturesForAddress 50, getTokenLargestAccounts 50, ws_notification 10, … are estimates.
  Reconcile `fart status` → `budget rpc cost` with the Alchemy dashboard after 24 h.
- [ ] **Whether websocket notifications are billed** and at what rate (`ws_notification`).
- [ ] **EUR 50/month ≈ 3,000,000 CU/day** (`RPC_DAILY_BUDGET_CU` default). Depends on Alchemy's
  pay-as-you-go price per million CU.
- [ ] **Public RPC fallback limits** (`rpc_public`: 1 req/s, burst 4, 20k/day). The public
  endpoint may refuse `logsSubscribe` or `getSignaturesForAddress` with large limits.
- [ ] **`getSignaturesForAddress` on the mint authority** lists create transactions newest-first
  with `slot`/`blockTime`, and one transaction creates one coin (`fart/ingest/sweep.py`).

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
