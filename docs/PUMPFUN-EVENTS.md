# pump.fun and pump-amm Anchor events: layouts used by `fart/chain/events.py`

**Status (26 Oct 2026, milestone 3).** Every layout below follows the official IDLs published at
github.com/pump-fun/pump-public-docs (`idl/pump.json`, `idl/pump_amm.json`, read on 26 Oct 2026): the
milestone-1/2 field lists turned out to be the exact prefixes of the IDL structs and the IDL's trailing
fields were appended. On the VPS the milestone-1 deploy decoded `CreateEvent`, `TradeEvent` (the dev buy
inside the create transaction) and `CompletePumpAmmMigrationEvent` from the live log stream (467 creates
in one hour via the RPC stream), so those *decode on mainnet*; the byte-level check of step 3 under
"Verification" (a real recording decoding `full` with zero extra bytes) is still the formal proof. The
pump-amm swaps (`BuyEvent`, `SellEvent`) are stored only when the pool maps to a known base mint
(CreatePoolEvent in the same transaction or `migrations.pool_address`) whose quote mint is WSOL; an
unmappable swap is counted (`decode.pump_amm_pool_unknown`, `decode.pump_amm_quote_not_wsol`), never
guessed. Every event name of both IDLs is known to the decoder, so `fart events classify` names what it
does not decode. The fixtures under `tests/fixtures/` are synthetic (`tests/fixtures/gen_fixtures.py`) and
deliberately carry the pre-2026 field counts (an older program version's payload decodes as `partial`).

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
| TradeEvent | `bddb7fd34ee661ee` | pump | decoded (layout in `LAYOUTS`) |
| CompletePumpAmmMigrationEvent | `bde95db95c94ea94` | pump | decoded (layout in `LAYOUTS`) |
| CompleteEvent | `5f72619cd42e9808` | pump | decoded (layout in `LAYOUTS`) |
| BuyEvent | `67f4521f2cf57777` | pump_amm | decoded (layout in `LAYOUTS`) |
| SellEvent | `3e2f370aa503dc2a` | pump_amm | decoded (layout in `LAYOUTS`) |
| CreatePoolEvent | `b1310cd2a076a774` | pump_amm | decoded (layout in `LAYOUTS`) |

### Known events we skip (counted, payload not stored)

Every event of `idl/pump.json` and `idl/pump_amm.json` (26 Oct 2026) plus a few names of older IDL versions.

| event | discriminator (hex) | program(s) | status |
|---|---|---|---|
| AddQuoteControlMintEvent | `a4af574a582d213e` | pump | known, counted only |
| AdminCtoEvent | `6e7ce262aaff1178` | pump | known, counted only |
| AdminSetIdlAuthorityEvent | `f53b46224bb96d5c` | pump | known, counted only |
| AdminUpdateTokenIncentivesEvent | `93fa6c78f71d43de` | pump, pump_amm | known, counted only |
| ClaimCashbackEvent | `e2d6f62107f293e5` | pump, pump_amm | known, counted only |
| ClaimTokenIncentivesEvent | `4facf631cd5bcee8` | pump, pump_amm | known, counted only |
| CloseUserVolumeAccumulatorEvent | `929fbdac925838f4` | pump, pump_amm | known, counted only |
| CollectCreatorFeeEvent | `7a027f010ebf0caf` | pump | known, counted only |
| DistributeCreatorFeesEvent | `a537817004b3ca28` | pump | known, counted only |
| DistributeFeeToHoldersEvent | `e3bed7ceb0b4a584` | pump | known, counted only |
| ExtendAccountEvent | `6161d7905d92167c` | pump, pump_amm | known, counted only |
| InitUserVolumeAccumulatorEvent | `86240d48e86582d8` | pump, pump_amm | known, counted only |
| MigrateBondingCurveCreatorEvent | `9ba768dcd56cf303` | pump | known, counted only |
| MinimumDistributableFeeEvent | `a8d884efebb63134` | pump | known, counted only |
| PostCompleteBuyEvent | `6fb06d8b316cd5fb` | pump | known, counted only |
| RemoveQuoteControlMintEvent | `2e21568600d5d130` | pump | known, counted only |
| ReservedFeeRecipientsEvent | `2bbcfa12dd4bbb5f` | pump, pump_amm | known, counted only |
| SetCreatorEvent | `ed347b25f5fb48d2` | pump | known, counted only |
| SetMetaplexCreatorEvent | `8ecb06207f69bfa2` | pump | known, counted only |
| SetParamsEvent | `dfc39ff63e308f83` | pump | known, counted only |
| SetQuoteControlAdminEvent | `4af88d45ca511ef7` | pump | known, counted only |
| SetQuoteControlMintReservesEvent | `a943276d6686b6e8` | pump | known, counted only |
| SetQuoteControlReservesAdminEvent | `43fbb3b6575ca921` | pump | known, counted only |
| SweepBondingCurveFeeEvent | `742b4dbd117a482b` | pump | known, counted only |
| SyncUserVolumeAccumulatorEvent | `c57aa77c74515bff` | pump, pump_amm | known, counted only |
| UpdateCreatorFeeConfigEvent | `98c67c7c6af67fbf` | pump, pump_amm | known, counted only |
| UpdateGlobalAuthorityEvent | `b6c3892a23cecff7` | pump | known, counted only |
| UpdateMayhemVirtualParamsEvent | `757be4b6a1a8dcd6` | pump | known, counted only |
| AdminSetCreatorEvent | `4045c0681d1e196b` | pump | known, counted only |
| ReservesSyncedEvent | `129ac1451fabbea1` | pump | known, counted only |
| ReserveEvent | `4e79b291e163055d` | pump | known, counted only |
| CreateEventV2 | `a262083b9670e371` | pump | known, counted only |
| TradeEventV2 | `128ed28bb27bcb75` | pump | known, counted only |
| AdminCtoPoolEvent | `2f23a3f9969d937a` | pump_amm | known, counted only |
| BoostBuyAndBurnEvent | `3f451c16305cc2b9` | pump_amm | known, counted only |
| CollectCoinCreatorFeeEvent | `e8f5c2eeeada3a59` | pump_amm | known, counted only |
| CreateConfigEvent | `6b34598137e25116` | pump_amm | known, counted only |
| DepositEvent | `78f83d531f8e6b90` | pump_amm | known, counted only |
| DisableEvent | `6bfdc14ce4ca1b68` | pump_amm | known, counted only |
| InitBoostEvent | `ae7c4af90451f611` | pump_amm | known, counted only |
| MigratePoolCoinCreatorEvent | `aadd52c793a5f72e` | pump_amm | known, counted only |
| SetBondingCurveCoinCreatorEvent | `f2e7eb664163bdd3` | pump_amm | known, counted only |
| SetBoostAuthorityEvent | `5980f08d5bca4769` | pump_amm | known, counted only |
| SetMetaplexCoinCreatorEvent | `966bc77b7ccf66e4` | pump_amm | known, counted only |
| SweepPoolFeeEvent | `82a42461e48287a5` | pump_amm | known, counted only |
| UpdateAdminEvent | `e198ab57f63f42ea` | pump_amm | known, counted only |
| UpdateFeeConfigEvent | `5a1741233ef4bcd0` | pump_amm | known, counted only |
| WithdrawEvent | `1609851aa02c47c0` | pump_amm | known, counted only |
| SetCoinCreatorEvent | `e519b9b7eb58c24e` | pump_amm | known, counted only |
| AdminSetCoinCreatorEvent | `2ddc5d181961ac68` | pump_amm | known, counted only |
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
| TradeEvent | `bddb7fd34ee661ee` | pump | guess for classification only, never decoded |
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

## The undecoded discriminators seen on the VPS (26 Oct 2026)

Hour 05 UTC (milestone 1, 74 payloads in 30 minutes) and the milestone-2 run: `decode.unknown_discriminator`
21,744, `decode.too_short` 5,845, `decode.truncated_or_drift` 471 by 08:30 UTC. The bulk came from the two
system-mint subscriptions (USDC, PUMP: logs of Jupiter, Raydium, Orca, Meteora and token programs that
mention those mints, see below) and from the hot pump-amm pool; both are gone in milestone 3.

| discriminator | name | status after milestone 3 |
|---|---|---|
| `b1310cd2a076a774` | CreatePoolEvent (pump_amm) | decoded since milestone 2 |
| `67f4521f2cf57777` | BuyEvent (pump_amm) | decoded since milestone 2 |
| `ae7c4af90451f611` | **InitBoostEvent** (pump_amm, IDL) | known, counted only: the boost feature of pump-amm pools |
| `9ba768dcd56cf303` | **MigrateBondingCurveCreatorEvent** (pump, IDL) | known, counted only |
| `a537817004b3ca28` | **DistributeCreatorFeesEvent** (pump, IDL) | known, counted only |
| `40c6cde8260871e2` | SwapEvent (jupiter_v6 / raydium name match) | another program's event in the same transaction; `fart events classify` shows the emitting program |
| `e1ca49af932ba096` | Traded (orca_whirlpool name match) | another program's event; see the emitting program |
| `8569aac8b874fb58` | - | not a pump.fun or pump-amm event (neither IDL has it); the emitting program column decides |
| `15bac4b85be4e1cb` | - | not a pump.fun or pump-amm event; the emitting program column decides |

Anything `fart events classify` still lists with the emitting program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
or `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` and status `unknown` is a real gap: the program changed
after the IDL of 26 Oct 2026; add the name to `fart/chain/events.py`.

## PumpPortal migration messages with USDC / PUMP in `mint` (26 Oct 2026)

On the VPS the `subscribeMigration` stream delivered messages that `store.classify_pumpportal` read as
`migrate` and whose **`mint` field** carried `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC) and
`pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` (PUMP). `pumpportal_migration_record()` takes the mint from
exactly that field (`pool_address` is never in the message, `dex` = `pool` = `pump-amm`), so both mints
became `graduation` watchlist entries, `rpc_trades` subscribed to them (the mint itself, no pool known)
and every transaction on Solana that mentions USDC or PUMP arrived as a notification: 72,770 and 8,142 in
six minutes, 2,729 rows in `trades` with `source='pump_amm'` and those mints, because
`pump_amm_trade_records` took the mint from the subscription.

Since milestone 3: a migration is refused when its mint is on `config/pumpfun.json` → `system_mints`
(`store.migration_rejection`); the whole raw message is stored in `rejected_messages` (reason
`system_mint:USDC`, `system_mint:PUMP`) and counted (`ingest.rejected.pumpportal.<reason>`); pump-amm swaps
map their mint through the pool only; schema v3 deleted the rows. Milestone 3 also refused a PumpPortal
migration of a coin not in `coins` (`unknown_coin`: `Cv934yS1kxVtW3Y6vS8jhk6APKpdtWbzpMcs6VHHpump` and one
more on 26 Oct 2026, coins created before fart started); since milestone 4 such a migration is stored
with `migrations.coin_unknown=1` (part of the universe, never watched, the flag clears when the create
arrives; `tests/fixtures/pumpportal_migration_unknown_coin.json`). To see what PumpPortal actually sends
in the system-mint messages (the open question is which
coin they were about: pump.fun now has non-SOL quote mints, see the `quote_mint` fields below, so these may
be the quote side of USDC- or PUMP-quoted migrations):

```
sqlite3 /opt/fart/data/fart.db "select received_at, reason, raw_json from rejected_messages where reason like 'system_mint%' order by id desc limit 5"
```

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
| 22 | ix_name | string (IDL 26 Oct 2026) |
| 23 | mayhem_mode | bool |
| 24 | cashback_fee_basis_points | u64 |
| 25 | cashback | u64 |
| 26 | buyback_fee_basis_points | u64 |
| 27 | buyback_fee | u64 |
| 28 | shareholders | vec<Shareholder{address pubkey, share_bps u16}> |
| 29 | quote_mint | pubkey |
| 30 | quote_amount | u64 |
| 31 | virtual_quote_reserves | u64 |
| 32 | real_quote_reserves | u64 |
| 33 | holder_rewards_bps | u64 |
| 34 | holder_rewards | u64 |
| 35 | creator_fee_unclaimed | u64 |

Reserves in a TradeEvent are the state *after* the trade.

### pump.fun CompletePumpAmmMigrationEvent (required: first 2 fields)

user pubkey, mint pubkey, mint_amount u64, sol_amount u64, pool_migration_fee u64,
bonding_curve pubkey, timestamp i64, pool pubkey, quote_mint pubkey (IDL 26 Oct 2026).

### pump.fun CompleteEvent (required: first 2 fields)

user pubkey, mint pubkey, bonding_curve pubkey, timestamp i64, quote_mint pubkey (IDL 26 Oct 2026).

### pump-amm BuyEvent (IDL-confirmed order; required: first 16 fields)

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
| 29 | min_base_amount_out | u64 (IDL 26 Oct 2026) |
| 30 | ix_name | string |
| 31 | cashback_fee_basis_points | u64 |
| 32 | cashback | u64 |
| 33 | buyback_fee_basis_points | u64 |
| 34 | buyback_fee | u64 |
| 35 | virtual_quote_reserves | i128 |
| 36 | can_boost | bool |
| 37 | base_supply | u64 |
| 38 | holder_rewards_bps | u64 |
| 39 | holder_rewards | u64 |
| 40 | creator_fee_unclaimed | u64 |

Open question: whether the pool reserves are the state before or after the swap (stored as
`trades.virtual_sol/virtual_token` either way; the price error is one trade's worth). The quote mint is
not assumed any more: a pool whose CreatePoolEvent names another quote mint than WSOL is never a trade
source (`quote_not_wsol`).

### pump-amm SellEvent (IDL-confirmed order; required: first 16 fields)

Fields 1-14 as BuyEvent with `base_amount_in`, `min_quote_amount_out`, `quote_amount_out`,
`quote_amount_out_without_lp_fee`, `user_quote_amount_out`; then pool, user, user_base_token_account,
user_quote_token_account, protocol_fee_recipient, protocol_fee_recipient_token_account,
coin_creator, coin_creator_fee_basis_points, coin_creator_fee (23 fields), then (IDL 26 Oct 2026)
cashback_fee_basis_points u64, cashback u64, buyback_fee_basis_points u64, buyback_fee u64,
virtual_quote_reserves i128, can_boost bool, base_supply u64, holder_rewards_bps u64, holder_rewards u64,
creator_fee_unclaimed u64 (33 fields).

### pump-amm CreatePoolEvent (IDL-confirmed order; required: first 5 fields)

timestamp i64, index u16, creator pubkey, base_mint pubkey, quote_mint pubkey, base_mint_decimals u8,
quote_mint_decimals u8, base_amount_in u64, quote_amount_in u64, pool_base_amount u64,
pool_quote_amount u64, minimum_liquidity u64, initial_liquidity u64, lp_token_amount_out u64,
pool_bump u8, pool pubkey, lp_mint pubkey, user_base_token_account pubkey,
user_quote_token_account pubkey, coin_creator pubkey, then (IDL 26 Oct 2026) is_mayhem_mode bool,
creator_fee_bps u64, can_edit_creator_fee bool, is_holder_reward bool (24 fields). Fills
`migrations.pool_address` when the migration event itself was not decodable and is the pool → base-mint
mapping for the swaps of the same transaction (`store.pool_base_mints`). The pool PDA seeds in the IDL are
`["pool", index u16, creator, base_mint, quote_mint]` under the pump-amm program, as `fart chain pda` guesses;
whether `creator` is the pump.fun pool-authority PDA for migrated coins is still to compare with
`migrations.pool_address`.

## Tolerance rules

Fields are parsed in order until the bytes run out. Fewer than the required fields ->
`Undecoded(reason="truncated_or_drift")`, stored in `raw_undecoded` (max 500 per hour, with the
emitting program) and counted in `kv decode.*`. A `timestamp` outside 2020-2100 or a
`*_basis_points` field above 10,000 marks the event `suspect` (decoded, but a suspect timestamp is
not used as chain time and a suspect pump-amm swap is not stored as a trade). Unknown
discriminators are stored and counted; known but irrelevant events are counted only.
`DecodedEvent.layout` reads like `CreateEvent/full/20of20` (a current payload), `CreateEvent/partial/12of20`
(an older program version: the milestone-2 fields only) or `BuyEvent/partial/16of40`. Borsh `vec` and
`i128` fields are read by the decoder since milestone 3 (`shareholders`, `virtual_quote_reserves`).

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
   and a pump-amm `CreatePoolEvent` whose `pool` equals the migration event's `pool` and whose
   `quote_mint` is WSOL (`So111...112`).
6. Repeat for a pump-amm trade: `select signature from trades where source='pump_amm' limit 1`;
   expect `BuyEvent`/`SellEvent` with `extra_bytes == 0`, plausible fees and a `pool` that maps to the
   trade's mint in `migrations`.
7. Copy the responses to `tests/fixtures/tx_create_real.json`, `tests/fixtures/tx_migration_real.json`
   (public data, no secrets): `tests/test_events.py` picks them up automatically.
8. Update the status line at the top of this file and `docs/UNVERIFIED.md`.
