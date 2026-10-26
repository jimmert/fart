# pump.fun and pump-amm Anchor events: layouts used by `fart/chain/events.py`

**Status (26 Oct 2026).** The pump.fun layouts below come from the published IDL history. On the VPS
the milestone-1 deploy decoded `CreateEvent`, `TradeEvent` (the dev buy inside the create transaction)
and `CompletePumpAmmMigrationEvent` from the live log stream (467 creates in one hour via the RPC
stream), so those three *decode on mainnet*; the byte-level check of step 3 under "Verification" is
still the formal proof and still open. The pump-amm (PumpSwap) layouts (`BuyEvent`, `SellEvent`,
`CreatePoolEvent`) were added in milestone 2 from the PumpSwap IDL as remembered and are
**TO VERIFY**: a `BuyEvent`/`SellEvent` whose `pool` differs from the pool we subscribed to is
rejected and counted (`decode.pump_amm_pool_mismatch`), which is the built-in self-check.
The fixtures under `tests/fixtures/` are synthetic (`tests/fixtures/gen_fixtures.py`).

## Where events live in a transaction

* `meta.logMessages`: lines `Program data: <base64>` (Anchor `emit!`). This is what
  `logsSubscribe` delivers. `attribute_program_data()` walks the `Program X invoke [n]` /
  `Program X success` nesting so every payload is attributed to the program that emitted it; the
  program is stored with every undecodable payload (`raw_undecoded.program`).
* Self-CPI inner instructions (Anchor `emit_cpi!`): instruction data = 8 bytes
  `e445a52e51cb9a1d` (Anchor's EVENT_IX_TAG, little-endian) + the same event payload. Only visible
  through `getTransaction` (`meta.innerInstructions[].instructions[].data`, base58). The decoder
  strips the tag when it sees it, so a stored discriminator is always the event's own.

`fart.chain.events.decode_event_bytes` accepts both forms; `fart.chain.tx.events_from_transaction`
reads both and collapses duplicates.

## Payload = discriminator (8 bytes) + Borsh fields

Discriminator = `sha256("event:<Name>")[:8]`. The program is **not** part of the hash: an event
name shared by pump.fun and pump-amm (`ExtendAccountEvent`, the volume-accumulator events, ...) has
one discriminator for both programs; only the log nesting tells them apart.

Borsh encodings: `string` = u32 little-endian length + UTF-8 bytes; `pubkey` = 32 bytes;
`u64`/`i64` = 8 bytes little-endian; `u16` = 2 bytes; `u8`/`bool` = 1 byte.

### Decoded events

| event | discriminator (hex) | program(s) | status |
|---|---|---|---|
| CreateEvent | `1b72a94ddeeb6376` | pump | decoded (layout in `LAYOUTS`) |
| TradeEvent | `bddb7fd34ee661ee` | pump, raydium | decoded (layout in `LAYOUTS`) |
| CompletePumpAmmMigrationEvent | `bde95db95c94ea94` | pump | decoded (layout in `LAYOUTS`) |
| CompleteEvent | `5f72619cd42e9808` | pump | decoded (layout in `LAYOUTS`) |
| BuyEvent | `67f4521f2cf57777` | pump_amm | decoded (layout in `LAYOUTS`) |
| SellEvent | `3e2f370aa503dc2a` | pump_amm | decoded (layout in `LAYOUTS`) |
| CreatePoolEvent | `b1310cd2a076a774` | pump_amm | decoded (layout in `LAYOUTS`) |

### Known events we skip (counted, payload not stored)

| event | discriminator (hex) | program(s) | status |
|---|---|---|---|
| SetParamsEvent | `dfc39ff63e308f83` | pump | known, counted only |
| CollectCreatorFeeEvent | `7a027f010ebf0caf` | pump | known, counted only |
| ExtendAccountEvent | `6161d7905d92167c` | pump, pump_amm | known, counted only |
| UpdateGlobalAuthorityEvent | `b6c3892a23cecff7` | pump | known, counted only |
| SetCreatorEvent | `ed347b25f5fb48d2` | pump | known, counted only |
| SetMetaplexCreatorEvent | `8ecb06207f69bfa2` | pump | known, counted only |
| ClaimTokenIncentivesEvent | `4facf631cd5bcee8` | pump, pump_amm | known, counted only |
| InitUserVolumeAccumulatorEvent | `86240d48e86582d8` | pump, pump_amm | known, counted only |
| SyncUserVolumeAccumulatorEvent | `c57aa77c74515bff` | pump, pump_amm | known, counted only |
| AdminSetCreatorEvent | `4045c0681d1e196b` | pump | known, counted only |
| AdminUpdateTokenIncentivesEvent | `93fa6c78f71d43de` | pump, pump_amm | known, counted only |
| CloseUserVolumeAccumulatorEvent | `929fbdac925838f4` | pump, pump_amm | known, counted only |
| ReservesSyncedEvent | `129ac1451fabbea1` | pump | known, counted only |
| AdminSetIdlAuthorityEvent | `f53b46224bb96d5c` | pump | known, counted only |
| ReserveEvent | `4e79b291e163055d` | pump | known, counted only |
| CreateEventV2 | `a262083b9670e371` | pump | known, counted only |
| TradeEventV2 | `128ed28bb27bcb75` | pump | known, counted only |
| DepositEvent | `78f83d531f8e6b90` | pump_amm | known, counted only |
| WithdrawEvent | `1609851aa02c47c0` | pump_amm | known, counted only |
| DisableEvent | `6bfdc14ce4ca1b68` | pump_amm | known, counted only |
| UpdateAdminEvent | `e198ab57f63f42ea` | pump_amm | known, counted only |
| UpdateFeeConfigEvent | `5a1741233ef4bcd0` | pump_amm | known, counted only |
| CollectCoinCreatorFeeEvent | `e8f5c2eeeada3a59` | pump_amm | known, counted only |
| SetCoinCreatorEvent | `e519b9b7eb58c24e` | pump_amm | known, counted only |
| AdminSetCoinCreatorEvent | `2ddc5d181961ac68` | pump_amm | known, counted only |
| CreateConfigEvent | `6b34598137e25116` | pump_amm | known, counted only |
| UpdateConfigEvent | `6070fd663b4e4b86` | pump_amm | known, counted only |
| BuyExactQuoteInEvent | `7b3d3ef45415f4be` | pump_amm | known, counted only |
| SellExactQuoteOutEvent | `0a97ae00597b36cf` | pump_amm | known, counted only |

### Other programs' events (classification guesses)

These names are only used by `classify_discriminator()` / `fart events classify` to label payloads
that other Anchor programs emit inside transactions that also touch pump.fun (Jupiter routes, Orca,
Raydium, Meteora). They are never decoded.

| event | discriminator (hex) | program(s) | status |
|---|---|---|---|
| SwapEvent | `40c6cde8260871e2` | jupiter_v6, raydium | guess for classification only, never decoded |
| FeeEvent | `494f4e7fb8d50ddc` | jupiter_v6 | guess for classification only, never decoded |
| LpChangeEvent | `79a3cdc939da753c` | raydium | guess for classification only, never decoded |
| PoolCreateEvent | `97d7e20976a173ae` | raydium | guess for classification only, never decoded |
| TradeEvent | `bddb7fd34ee661ee` | pump, raydium | guess for classification only, never decoded |
| ClaimVestedEvent | `15c2725778d3e220` | raydium | guess for classification only, never decoded |
| CreateVestingEvent | `96980bb334d2bf7d` | raydium | guess for classification only, never decoded |
| SwapBaseInEvent | `462fac95f82564a2` | raydium | guess for classification only, never decoded |
| SwapBaseOutEvent | `d5c04792ff0b1a7d` | raydium | guess for classification only, never decoded |
| IncreaseLiquidityEvent | `314f69d420221e54` | raydium | guess for classification only, never decoded |
| DecreaseLiquidityEvent | `3ade563a44325538` | raydium | guess for classification only, never decoded |
| CollectPersonalFeeEvent | `a6ae69c051a15369` | raydium | guess for classification only, never decoded |
| CreatePersonalPositionEvent | `641e57f9c4df9ace` | raydium | guess for classification only, never decoded |
| LiquidityChangeEvent | `7ef0afce9e58996b` | raydium | guess for classification only, never decoded |
| LiquidityCalculateEvent | `ed7094e63954b4a2` | raydium | guess for classification only, never decoded |
| ConfigChangeEvent | `f7bd07776a705f97` | raydium | guess for classification only, never decoded |
| CollectProtocolFeeEvent | `ce57114f2d29d53d` | raydium | guess for classification only, never decoded |
| MigrateEvent | `d8afe75f2d626c15` | raydium | guess for classification only, never decoded |
| Traded | `e1ca49af932ba096` | orca_whirlpool | guess for classification only, never decoded |
| LiquidityIncreased | `1e0790b566fe9ba1` | orca_whirlpool | guess for classification only, never decoded |
| LiquidityDecreased | `a601244770cab5ab` | orca_whirlpool | guess for classification only, never decoded |
| PoolInitialized | `6476ad570cc6fee5` | orca_whirlpool | guess for classification only, never decoded |
| Swap | `516ce3becdd00ac4` | meteora | guess for classification only, never decoded |
| AddLiquidity | `1f5e7d5ae3343dba` | meteora | guess for classification only, never decoded |
| RemoveLiquidity | `74f461e8671f983a` | meteora | guess for classification only, never decoded |
| PositionCreate | `908efc549d352579` | meteora | guess for classification only, never decoded |
| PositionClose | `ffc4106b1cca3580` | meteora | guess for classification only, never decoded |
| ClaimFee | `4b7a9a308c4a7ba3` | meteora | guess for classification only, never decoded |
| ClaimReward | `947486cc16ab555f` | meteora | guess for classification only, never decoded |
| LbPairCreate | `b94afc7d1bd7bc6f` | meteora | guess for classification only, never decoded |
| CompositionFee | `80977b6a1166718e` | meteora | guess for classification only, never decoded |
| EvtSwap | `1b3c15d58aaabb93` | meteora | guess for classification only, never decoded |
| EvtSwap2 | `bd4233a826507599` | meteora | guess for classification only, never decoded |
| EvtCreatePool | `1ec749ffb705d6c6` | meteora | guess for classification only, never decoded |
| EvtAddLiquidity | `aff2089d1ef7b9a9` | meteora | guess for classification only, never decoded |
| EvtRemoveLiquidity | `572e5862af60225b` | meteora | guess for classification only, never decoded |
| EvtClaimPositionFee | `c6b6b734610c3138` | meteora | guess for classification only, never decoded |
| EvtInitializePool | `e432f655cb428625` | meteora | guess for classification only, never decoded |
| EvtCurveComplete | `e5e756549c864b18` | meteora | guess for classification only, never decoded |
| EvtClaimTradingFee | `1a5375f05cca70fe` | meteora | guess for classification only, never decoded |
| EvtClaimProtocolFee | `baf44bfbbc0d1921` | meteora | guess for classification only, never decoded |
| EvtCreatorWithdrawSurplus | `9849150f4257359d` | meteora | guess for classification only, never decoded |
| EvtCreateDammV2MigrationMetadata | `676f84a88cfd9672` | meteora | guess for classification only, never decoded |
| EvtClaimCreatorTradingFee | `9ae4d7ca859bd68a` | meteora | guess for classification only, never decoded |

## The undecoded discriminators seen on the VPS (hour 05 UTC, 26 Oct 2026)

74 undecodable `Program data:` payloads in 30 minutes, from the create (mint authority) and
graduation (migration account) streams:

| discriminator | best guess | status after milestone 2 |
|---|---|---|
| `b1310cd2a076a774` | CreatePoolEvent | **CreatePoolEvent** (pump_amm) - decoded from now on; the pump-amm layout is TO VERIFY (`fart events classify` should stop listing it) |
| `ae7c4af90451f611` | - | no candidate name; the emitting program (now stored per row) decides whether this matters at all |
| `40c6cde8260871e2` | SwapEvent | name match **SwapEvent** of jupiter_v6, raydium - another Anchor program in the same transaction; confirm the emitting program with `fart events classify --fetch 5` |
| `e1ca49af932ba096` | Traded | name match **Traded** of orca_whirlpool - another Anchor program in the same transaction; confirm the emitting program with `fart events classify --fetch 5` |
| `9ba768dcd56cf303` | - | no candidate name; the emitting program (now stored per row) decides whether this matters at all |
| `8569aac8b874fb58` | - | no candidate name; the emitting program (now stored per row) decides whether this matters at all |
| `a537817004b3ca28` | - | no candidate name; the emitting program (now stored per row) decides whether this matters at all |
| `15bac4b85be4e1cb` | - | no candidate name; the emitting program (now stored per row) decides whether this matters at all |
| `67f4521f2cf57777` | BuyEvent | **BuyEvent** (pump_amm) - decoded from now on; the pump-amm layout is TO VERIFY (`fart events classify` should stop listing it) |

Interpretation: a graduation transaction calls pump-amm `create_pool` (`CreatePoolEvent`) and the
first buy on the pool (`BuyEvent`); both are pump-amm events and are decoded from milestone 2 on.
`SwapEvent`/`Traded` are swaps of other DEX programs that share a transaction with a pump.fun
instruction (bundled create-and-swap transactions). The five unknown ones carry no pump.fun or
pump-amm name; the emitting program per row (`raw_undecoded.program`, written from milestone 2 on,
or `fart events classify --fetch 5` for older rows) tells whether they are pump.fun's at all.

### pump.fun CreateEvent (required: first 6 fields)

| # | field | type | note |
|---|---|---|---|
| 1 | name | string | |
| 2 | symbol | string | |
| 3 | uri | string | metadata JSON |
| 4 | mint | pubkey | |
| 5 | bonding_curve | pubkey | = `find_program_address(["bonding-curve", mint], pump)` (`fart/chain/pda.py`) |
| 6 | user | pubkey | transaction signer |
| 7 | creator | pubkey | newer program versions |
| 8 | timestamp | i64 | chain time (seconds) |
| 9 | virtual_token_reserves | u64 | |
| 10 | virtual_sol_reserves | u64 | lamports |
| 11 | real_token_reserves | u64 | |
| 12 | token_total_supply | u64 | |

### pump.fun TradeEvent (required: first 10 fields)

| # | field | type |
|---|---|---|
| 1 | mint | pubkey |
| 2 | sol_amount | u64 (lamports) |
| 3 | token_amount | u64 (6 decimals) |
| 4 | is_buy | bool |
| 5 | user | pubkey |
| 6 | timestamp | i64 |
| 7 | virtual_sol_reserves | u64 |
| 8 | virtual_token_reserves | u64 |
| 9 | real_sol_reserves | u64 |
| 10 | real_token_reserves | u64 |
| 11 | fee_recipient | pubkey |
| 12 | fee_basis_points | u64 |
| 13 | fee | u64 |
| 14 | creator | pubkey |
| 15 | creator_fee_basis_points | u64 |
| 16 | creator_fee | u64 |
| 17 | track_volume | bool |
| 18 | total_unclaimed_tokens | u64 |
| 19 | total_claimed_tokens | u64 |
| 20 | current_sol_volume | u64 |
| 21 | last_update_timestamp | i64 |

Reserves in a TradeEvent are the state *after* the trade.

### pump.fun CompletePumpAmmMigrationEvent (required: first 2 fields)

user pubkey, mint pubkey, mint_amount u64, sol_amount u64, pool_migration_fee u64,
bonding_curve pubkey, timestamp i64, pool pubkey.

### pump.fun CompleteEvent (required: first 2 fields)

user pubkey, mint pubkey, bonding_curve pubkey, timestamp i64.

### pump-amm BuyEvent (TO VERIFY; required: first 16 fields)

| # | field | type |
|---|---|---|
| 1 | timestamp | i64 |
| 2 | base_amount_out | u64 (token base units) |
| 3 | max_quote_amount_in | u64 |
| 4 | user_base_token_reserves | u64 |
| 5 | user_quote_token_reserves | u64 |
| 6 | pool_base_token_reserves | u64 |
| 7 | pool_quote_token_reserves | u64 |
| 8 | quote_amount_in | u64 (lamports, quote = WSOL) |
| 9 | lp_fee_basis_points | u64 |
| 10 | lp_fee | u64 |
| 11 | protocol_fee_basis_points | u64 |
| 12 | protocol_fee | u64 |
| 13 | quote_amount_in_with_lp_fee | u64 |
| 14 | user_quote_amount_in | u64 |
| 15 | pool | pubkey |
| 16 | user | pubkey |
| 17 | user_base_token_account | pubkey |
| 18 | user_quote_token_account | pubkey |
| 19 | protocol_fee_recipient | pubkey |
| 20 | protocol_fee_recipient_token_account | pubkey |
| 21 | coin_creator | pubkey |
| 22 | coin_creator_fee_basis_points | u64 |
| 23 | coin_creator_fee | u64 |
| 24 | track_volume | bool |
| 25 | total_unclaimed_tokens | u64 |
| 26 | total_claimed_tokens | u64 |
| 27 | current_sol_volume | u64 |
| 28 | last_update_timestamp | i64 |

Open questions: whether the pool reserves are the state before or after the swap (stored as
`trades.virtual_sol/virtual_token` either way; the price error is one trade's worth), and whether
the quote mint is always WSOL for migrated pump.fun coins (assumed).

### pump-amm SellEvent (TO VERIFY; required: first 16 fields)

Fields 1-14 as BuyEvent with `base_amount_in`, `min_quote_amount_out`, `quote_amount_out`,
`quote_amount_out_without_lp_fee`, `user_quote_amount_out`; then pool, user, user_base_token_account,
user_quote_token_account, protocol_fee_recipient, protocol_fee_recipient_token_account,
coin_creator, coin_creator_fee_basis_points, coin_creator_fee (23 fields).

### pump-amm CreatePoolEvent (TO VERIFY; required: first 5 fields)

timestamp i64, index u16, creator pubkey, base_mint pubkey, quote_mint pubkey, base_mint_decimals u8,
quote_mint_decimals u8, base_amount_in u64, quote_amount_in u64, pool_base_amount u64,
pool_quote_amount u64, minimum_liquidity u64, initial_liquidity u64, lp_token_amount_out u64,
pool_bump u8, pool pubkey, lp_mint pubkey, user_base_token_account pubkey,
user_quote_token_account pubkey, coin_creator pubkey. Used to fill `migrations.pool_address`
when the migration event itself was not decodable.

## Tolerance rules

Fields are parsed in order until the bytes run out. Fewer than the required fields ->
`Undecoded(reason="truncated_or_drift")`, stored in `raw_undecoded` (max 500 per hour, with the
emitting program) and counted in `kv decode.*`. A `timestamp` outside 2020-2100 or a
`*_basis_points` field above 10,000 marks the event `suspect` (decoded, but a suspect timestamp is
not used as chain time and a suspect pump-amm swap is not stored as a trade). Unknown
discriminators are stored and counted; known but irrelevant events are counted only.
`DecodedEvent.layout` reads like `CreateEvent/full/12of12` or `BuyEvent/partial/16of28`.

## Verification (do this at deploy)

1. `fart events classify` on the VPS: `CreatePoolEvent`/`BuyEvent` should no longer appear in new
   rows; for the remaining unknown discriminators the `emitting program` column (or
   `--fetch 5`, five budget-gated `getTransaction` calls) names the program. Anything emitted by
   `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` or `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
   that we do not name is a real gap: add the name to `events.py`.
2. Take a fresh create signature:
   `sqlite3 /opt/fart/data/fart.db "select signature from coins where signature is not null order by received_at desc limit 1"`.
3. Fetch it: `curl -s $RPC_HTTP_URL -X POST -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"getTransaction","params":["<sig>",{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]}' > tx_create_real.json`
4. Decode it with our code:
   ```
   cd /opt/fart/app && /opt/fart/venv/bin/python - <<'EOF'
   import json
   from fart.chain.tx import events_from_transaction, create_from_tx
   tx = json.load(open("tx_create_real.json"))["result"]
   te = events_from_transaction(tx)
   for e in te.events: print(e.program, e.layout, e.suspect, e.extra_bytes)
   for u in te.undecoded: print("UNDECODED", u.program, u.reason, u.discriminator_hex)
   print(create_from_tx(te))
   EOF
   ```
   Expect `CreateEvent/full/...` with zero `extra_bytes` and no suspect flags, and a `TradeEvent`
   when the creator bought in the same transaction.
5. Repeat for a migration signature (`migrations.signature`) - expect `CompletePumpAmmMigrationEvent`
   and a pump-amm `CreatePoolEvent` whose `pool` equals the migration event's `pool`.
6. Repeat for a pump-amm trade: `select signature from trades where source='pump_amm' limit 1`;
   expect `BuyEvent`/`SellEvent` with `extra_bytes == 0`, plausible fees and the right pool.
7. Copy the responses to `tests/fixtures/tx_create_real.json`, `tests/fixtures/tx_migration_real.json`
   (public data, no secrets): `tests/test_events.py` picks them up automatically.
8. Update the status line at the top of this file and `docs/UNVERIFIED.md`.
