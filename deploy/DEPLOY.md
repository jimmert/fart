# Deploying fart on the VPS (Ubuntu 24.04)

Target layout (shared host, unprivileged user, outbound HTTPS/WSS only, no inbound ports):

```
/opt/fart/            root:fart 0750
  app/                the git checkout copy (read-only for the service)
  bin/fart            wrapper: cd /opt/fart/app && exec /opt/fart/venv/bin/python -m fart "$@" (FART_ROOT set)
  venv/               python3.12 venv with websockets
  data/               fart.db (SQLite WAL), raw/<source>/YYYYMMDD/HH.jsonl.gz, archive/, backup/
  out/recall/         daily reports <day>.json + <day>.md, week reports week-<end>.json + .md (milestone 5)
  logs/               ingest.log, engine.log, social.log, notify.log, measure-*.log (JSON lines, 5 MB x 5)
  .env                root:fart 0640 - the ONLY place where keys live
```

Every command below uses the wrapper: `sudo -u fart /opt/fart/bin/fart <command>`. (`python -m fart`
only works from `/opt/fart/app`; the wrapper does the `cd` and sets `FART_ROOT=/opt/fart`.)

systemd units (all in `deploy/`, installed to `/etc/systemd/system/`):

| unit | what | schedule |
|---|---|---|
| `fart.slice` | MemoryMax=1G, CPUQuota=100% for everything below | - |
| `fart-ingest.service` | PumpPortal (creates + migrations) + RPC streams (creates, migrations, per-mint trades before graduation), signature sweep, coverage, retention, Global check | daemon, Restart=always |
| `fart-engine.service` | candidate engine v0, observations only | daemon, 15 s loop |
| `fart-social.service` | X mega-account / KOL watch, CA/ticker confirmation counts (milestone 2; idle without `X_BEARER_TOKEN`) | daemon, 15 s loop |
| `fart-notify.service` | Telegram outbox to the owner chat (milestone 2; idle unless `TELEGRAM_NOTIFY_ENABLED=1`) | daemon, 20 s loop |
| `fart-measure-track.timer` | price samples + due outcomes + paper trades | every 5 min |
| `fart-measure-report.timer` | daily recall report for the previous UTC day | 06:00 UTC |
| `fart-measure-week.timer` | week report (rolling 7 days ending yesterday; the Monday run is the Monday-Sunday calendar week): universe, hit rate, recall, lead time, bootstrap intervals, costs, the frozen go/no-go test (milestone 5) | 06:30 UTC daily |
| `fart-backup.timer` | SQLite online backup + quick_check, 14 days kept | 05:30 UTC |

Hardening on every service: `User=fart`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`PrivateTmp`, `PrivateDevices`, `ReadWritePaths=/opt/fart/data /opt/fart/out /opt/fart/logs`,
`CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service`, `UMask=0027`, plus the usual
`Protect*`/`Restrict*` set. The code never signs anything and never opens a listening socket.

## 1. Prerequisites (once, as root)

```bash
apt-get update
apt-get install -y python3.12 python3.12-venv git rsync sqlite3
```

## 2. Install or update

```bash
# first time
git clone https://github.com/jimmert/fart.git /opt/fart-src
cd /opt/fart-src && git checkout main      # or the tag/commit you want to run
sudo deploy/install.sh

# update: pull, re-run (idempotent; .env, data, out, logs are never touched)
cd /opt/fart-src && git pull --ff-only
sudo deploy/install.sh
```

`install.sh` creates the `fart` system user, copies the checkout to `/opt/fart/app`, writes
`/opt/fart/app/VERSION` (the git commit = `rules_version` of every decision), builds the venv, installs
the `/opt/fart/bin/fart` wrapper, applies the database schema migration, installs the units, and
restarts the daemons. Set `START_SERVICES=0` to install without starting.

## 3. Configure `/opt/fart/.env`

The installer copies `.env.example` to `/opt/fart/.env` when it does not exist. Edit it as root:

```
RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/<key>
RPC_WSS_URL=wss://solana-mainnet.g.alchemy.com/v2/<key>
RPC_DAILY_BUDGET_CU=3000000
PUMPPORTAL_WSS_URL=wss://pumpportal.fun/api/data
PUMPPORTAL_API_KEY=                 # optional: funded PumpPortal key -> trade subscriptions via PumpPortal
X_BEARER_TOKEN=                     # optional: X API v2 bearer token -> fart-social active
X_DAILY_BUDGET_USD=3.00             # hard daily cap for all X calls together
TELEGRAM_BOT_TOKEN=                 # optional: bot token from @BotFather
TELEGRAM_OWNER_CHAT_ID=             # the owner's chat id (fart-notify sends nowhere else)
TELEGRAM_NOTIFY_ENABLED=0           # 1 = send; 0 = build nothing, send nothing
TELEGRAM_COMMUNITY_CHAT_ID=         # milestone 5, leave empty: the community chat (group/channel id); never sent to without BOTH set
TELEGRAM_COMMUNITY_ENABLED=0        # milestone 5, leave 0 during the measurement week: 1 = community messages to that chat
TELEGRAM_COMMUNITY_PREVIEW=0        # milestone 5: 1 = the community text to the OWNER, headed "VOORBEELD community-bericht"
FART_ROOT=/opt/fart
```

Leave `RPC_HTTP_URL`/`RPC_WSS_URL` empty to run on the public RPC (`api.mainnet-beta.solana.com`,
strict rate limits, no key, **no per-mint trade stream**). The file is read by the processes directly
(`root:fart 0640`); it is never logged and never copied anywhere. `fart status` prints only
`configured` / `not configured` per key. After editing:
`sudo systemctl restart fart-ingest fart-engine fart-social fart-notify`.

The community channel (milestone 5) is off by default and stays off until two names are set explicitly:
`TELEGRAM_COMMUNITY_CHAT_ID` (the chat id of the group or channel; the bot must be a member or admin there)
AND `TELEGRAM_COMMUNITY_ENABLED=1`, on top of the owner channel being enabled. Without both, `fart-notify`
never queues or sends a message for any chat but the owner's (a test proves it, and the sender refuses a
row for another chat without calling the API). `TELEGRAM_COMMUNITY_PREVIEW=1` sends the community-formatted
text to the owner as an extra message headed "VOORBEELD community-bericht"; previews count within the owner
limits (20 per minute, 6 observation batches per hour) and follow the community rules (one per coin and per
theme per 24 h, 6 per hour, quiet hours 02:00-08:00 Europe/Amsterdam; `config/notify.json` -> `community`).

`RPC_DAILY_BUDGET_CU` is a hard daily cap on *estimated* compute units (cost table in
`config/budget.json`: the HTTP methods at the figures of Alchemy's compute-unit page, websocket frames per
byte at `cu_per_byte`). Reconcile the estimate with the Alchemy dashboard after the first days and adjust
the table, `cu_per_byte` or the cap (the procedure is in `docs/UNVERIFIED.md`). The per-mint trade stream
may spend at most `daily_cu_share` (0.35) of the cap over a day on its own frames, enforced as an hourly
envelope of 0.35 / 24 = 1.46% of the cap (43,750 CU at 3,000,000; `config/ingest.json` → `rpc_trades`;
above it every trade subscription is dropped until the next hour), so the log stream, the enrichment and
the engine always keep the other 65%. It also drops any single subscription that passes 2,000
notifications in an hour; at 80% of the daily cap it drops the probation subscriptions; at the cap the
whole RPC side stops (log streams disconnect, enrichment pauses); PumpPortal keeps running; a warning is
logged once per hour.

## 4. Verify

```bash
systemctl status fart-ingest fart-engine fart-social fart-notify --no-pager
systemctl list-timers 'fart-*' --no-pager
journalctl -u fart-ingest -n 50 --no-pager
sudo -u fart /opt/fart/bin/fart status
sudo -u fart /opt/fart/bin/fart coverage --hours 3
sudo -u fart /opt/fart/bin/fart chain global          # Global account vs config/pumpfun.json (exit 1 = mismatch)
sudo -u fart /opt/fart/bin/fart events classify       # undecoded event discriminators and their emitting program
tail -f /opt/fart/logs/ingest.log                     # JSON lines: event, source, counts
ls /opt/fart/data/raw/pumpportal/$(date -u +%Y%m%d)/
```

Healthy after 10 minutes: `status` shows `ingest.pumpportal.last_msg_at` and `ingest.rpc_logs.last_msg_at`
a few seconds old, `ingest.rpc_trades.last_msg_at` fresh as soon as a watched coin trades, coins counting
up, `trades today by source` with `rpc_trades`, no open gaps except `rpc_trades` while the watchlist is
empty, budget rows with `errors 0`, `failures: none` (or counters that do not grow: `engine.tick_failed`,
`ingest.handle_failed.*`); `coverage` shows `pp` and `rpc` counts of the same order of magnitude for the
current hour (`covered` yes for every hour fart was running; the hour is only `complete` once it is over
and a source ran through the whole of it; a `recov(s)` column shows gap seconds the sweep recovered).

The 60-second live smoke test (one extra PumpPortal connection, no key) from the checkout:

```bash
cd /opt/fart-src && WITH_DEV=1 sudo deploy/install.sh   # installs pytest into the venv once
sudo -u fart env FART_ROOT=/tmp/fart-smoke FART_LIVE=1 /opt/fart/venv/bin/python -m pytest -q tests/test_live_smoke.py -s
```

Run it while `fart-ingest` is stopped if you want to be strict about PumpPortal's one-connection
rule for the shared VPS IP (the test itself only holds a connection for 60 s).

## 5. Upgrade from milestone 1

1. Back up first (the migration is one-way): `sudo systemctl start fart-backup.service` and check
   `ls -la /opt/fart/data/backup/`.
2. Pull and install:
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer installs `/opt/fart/bin/fart`, opens the database once as the `fart` user (this
   applies the schema migration `PRAGMA user_version` 1 → 2: new tables `social_posts`,
   `social_post_metrics`, `social_counts`, `x_spend`, `x_dedupe`, `outbox`, `ws_subscriptions`, new
   columns `coverage_gaps.recovered_at/recovered_by` and `raw_undecoded.program`; every milestone-1
   row stays), installs the two new units and restarts everything.
3. Add the new names to `/opt/fart/.env` (all optional, see section 3): `PUMPPORTAL_API_KEY`,
   `X_BEARER_TOKEN`, `X_DAILY_BUDGET_USD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID`,
   `TELEGRAM_NOTIFY_ENABLED`. Without them: PumpPortal stays creates + migrations, trades come from
   the RPC, `fart-social` and `fart-notify` idle with a heartbeat.
   `sudo systemctl restart fart-ingest fart-engine fart-social fart-notify` after editing.
4. Verify:
   ```bash
   sudo -u fart /opt/fart/bin/fart status               # schema_version: 2; trades: rpc only (no key)
   journalctl -u fart-ingest -n 30 --no-pager            # pp.mode ..., rpc_trades.connected subscriptions=N, global.check_ok
   sudo -u fart /opt/fart/bin/fart chain global          # exit 0
   sudo -u fart /opt/fart/bin/fart events classify       # CreatePoolEvent/BuyEvent now 'decoded'; the rest with emitting program
   sudo -u fart /opt/fart/bin/fart notify outbox         # empty until TELEGRAM_NOTIFY_ENABLED=1
   ```
   After an hour: `trades today by source` shows `rpc_trades` (and `pump_amm` once a watched coin
   graduated), `top open subscriptions` lists the busiest mints, `rpc CU today by method` shows what the
   trade stream costs - compare with the Alchemy dashboard and tune `config/budget.json`.
5. Telegram: with the token, chat id and `TELEGRAM_NOTIFY_ENABLED=1` in `.env`,
   `sudo -u fart /opt/fart/bin/fart notify test` sends one test message to the owner chat.

Rollback to milestone 1 code after the migration is possible (milestone-1 code ignores the new
tables and columns), but restoring the pre-upgrade backup is the clean way back.


## 5b. Upgrade to milestone 3 (M3)

M3 changes nothing in `.env` except the cap you temporarily raised, and nothing in the units. What it
changes on the server: every database write is one short `BEGIN IMMEDIATE` transaction with retries
(the engine writes again, the `database is locked` storm of 26 Oct is gone), the schema goes to
`user_version` 3 and removes the rows the USDC/PUMP "migrations" caused, the per-mint trade stream
only runs before graduation under an hourly CU envelope, the backup script compresses without the
`fchown` call that the syscall filter killed, and the daily report says NIET GEDEKT for hours fart was
not running. Step by step:

1. **Back up first.** On an M2 install `fart-backup.service` ends in `Result=signal` (exit 159, SIGSYS in
   gzip) *after* writing a complete `fart-<stamp>.db`; the `.gz` next to it may be truncated:
   ```bash
   sudo systemctl start fart-backup.service; systemctl show -p Result,ExecMainStatus fart-backup.service
   ls -la /opt/fart/data/backup/        # keep the newest fart-<stamp>.db; a half-written .gz of the same stamp can go
   ```
2. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   * The rsync replaces `/opt/fart/app/config/ingest.json`: the temporary VPS override
     `rpc_trades.enabled=false` disappears and the repository value applies (`true`, with
     `reasons`, `max_notifications_per_subscription_per_hour` 2000 and `hourly_cu_fraction` 1/24).
     To keep the stream off for a day of observation instead, set `rpc_trades.enabled` to `false` again
     in `/opt/fart/app/config/ingest.json` after the installer ran and `systemctl restart fart-ingest`
     (every `install.sh` run resets it; a config change is normally a commit).
   * The installer opens the database once as the `fart` user. That applies schema migration 2 → 3
     (table `rejected_messages`, column `ws_subscriptions.sub_id`, column `coverage.covered`, index
     `migrations(pool_address)`) **and the cleanup**: rows whose mint is USDC, USDT, WSOL or PUMP are
     deleted from `trades`, `migrations`, `mcap_samples` and `coins`, their `watchlist` and
     `ws_subscriptions` rows closed. The installer prints `v3 cleanup of system-mint rows: {"trades": N, ...}`;
     on the VPS expect `trades` close to 2729, `migrations` 2, `watchlist` 2. A second run prints zeros.
     The counts stay in kv (`fart status` → `schema v3 cleanup`).
3. **Put the CU cap back** in `/opt/fart/.env`: `RPC_DAILY_BUDGET_CU=4500000` → `RPC_DAILY_BUDGET_CU=3000000`
   (the override was only there because the trade stream burned 2.4M CU in two hours; the envelope now
   bounds that stream to 125,000 CU per hour at this cap). Then
   `sudo systemctl restart fart-ingest fart-engine fart-social fart-notify`.
4. **Verify the backup fix** (the script, not the syscall filter, changed):
   ```bash
   sudo systemctl start fart-backup.service
   systemctl show -p Result,ExecMainStatus,ExecMainCode fart-backup.service   # Result=success ExecMainStatus=0 ExecMainCode=1 (exited)
   journalctl -u fart-backup -n 6 --no-pager                                  # "backup: compressed ... (gzip -t ok)" and "backup: done N file(s) kept"
   ls -la /opt/fart/data/backup/                                              # only fart-<stamp>.db.gz files, no .db, no .tmp
   gzip -t /opt/fart/data/backup/fart-*.db.gz && echo archives ok
   ```
5. **Verify the rest** after 15 minutes:
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   schema_version: 3; freshness engine.heartbeat a few seconds; failures: none (engine.tick_failed must not appear or grow)
   #   rpc CU today by method: ws_trade_notification separate from ws_notification; stays under 125,000 per hour
   #   top open subscriptions: curve/probation|early_buyers|meta_burst only, never USDC/PUMP, never a pool
   #   rejected messages today: system_mint:USDC / system_mint:PUMP counting up is expected (PumpPortal keeps sending them)
   #   enrich (getTransaction): used/limit (7627 at the 3M cap), backlog migrations=... creates=...
   journalctl -u fart-engine --since -15min --no-pager | grep -c tick_failed      # 0
   journalctl -u fart-ingest --since -15min --no-pager | grep -c handle_failed    # 0
   sudo -u fart /opt/fart/bin/fart chain global          # exit 0: creator_fee_basis_points 5 = config creator_fee_bps 5
   sudo -u fart /opt/fart/bin/fart events classify       # ae7c4af90451f611 = InitBoostEvent (pump-amm), 9ba7... / a537... named too
   sudo -u fart /opt/fart/bin/fart coverage --hours 6    # 'covered' column; 'complete' only for full hours
   ```
   The next morning: `out/recall/<day>.md` has a "Verrijking en fouten" section and a `gedekt` column;
   a day fart did not run is headed **NIET GEDEKT** with empty percentages (the 25 Oct report that said
   COMPLEET was wrong for this reason).
6. **If handlers failed** (`failures:` in `status` shows `ingest.handle_failed.<source>`), re-feed the
   raw hour files of that period; it is idempotent:
   ```bash
   sudo -u fart /opt/fart/bin/fart status --json | grep -o '"ingest.handle_failed[^,]*'   # the .last key names the raw file
   sudo -u fart /opt/fart/bin/fart ingest replay --source rpc_logs --since 2026-10-26T08:00Z --until 2026-10-26T10:00Z
   ```
7. **Rollback** to M2 code is possible after the migration (M2 ignores the new table and columns; the
   deleted system-mint rows do not come back), the pre-upgrade `.db` from step 1 is the clean way back.

## 5c. Upgrade to milestone 4 (M4)

M4 changes nothing in the units and nothing in `.env` except the cap you temporarily raised. What it
changes on the server: the watchlist no longer churns (an entry is only evicted by a *strictly* higher
priority, `since` is never reset, 100 of the 200 slots are reserved for probation, the engine freezes a
choice of at most 40 meta_burst coins), the trade stream subscribes with hysteresis (a mint must be wanted
in two consecutive syncs, 10 s, before it is subscribed and unwanted for 60 s before it is dropped) and may
spend 35% of the daily CU cap over a day (1.46% per hour: the `0.015` you ran with), websocket frames are
booked per byte and the HTTP methods at Alchemy's published figures, a PumpPortal migration of a coin
created before fart started is stored with `coin_unknown=1` instead of refused, the schema goes to
`user_version` 4 and closes the milestone-2 `graduation` watchlist rows, and `fart status` shows the open
subscriptions, what closed today and why, bytes next to CU. Step by step:

1. **Back up first** (the migration is one-way):
   ```bash
   sudo systemctl start fart-backup.service; systemctl show -p Result fart-backup.service   # Result=success
   ls -la /opt/fart/data/backup/
   ```
2. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   * The rsync replaces `/opt/fart/app/config/ingest.json`: the temporary VPS overrides
     `rpc_trades.enabled=false` and `rpc_trades.hourly_cu_fraction=0.015` disappear. The repository has
     `enabled` `true`, `daily_cu_share` `0.35` (0.35 / 24 = 0.0146 per hour, the value you ran with),
     `unsubscribe_after_s` 60, `subscribe_after_syncs` 2 and `log_interval_s` 60; `config/engine.json`
     has `probation_reserve` 100, `meta_burst_per_meta` 5, `meta_burst_max_total` 40,
     `meta_burst_release_s` 600; `config/budget.json` has the calibrated table and `cu_per_byte` 0.0002.
     Nothing on the server needs to be edited by hand any more; a config change is a commit.
   * The installer opens the database once as the `fart` user: schema migration 3 → 4 adds the columns
     `watchlist.activated_at`, `migrations.coin_unknown`, `ws_subscriptions.close_reason`,
     `api_ledger.bytes`, **closes every active `graduation` watchlist row** (and its open
     `ws_subscriptions` row, `close_reason` `schema_v4`) and flags every migration without a coin row.
     The installer prints `v4 cleanup (graduation rows closed, migrations flagged): {...}`; on the VPS
     expect `watchlist_graduation_closed` close to 35 and `migrations_coin_unknown` a handful (the RPC
     stream's migrations of coins created before the first start). A second run prints zeros. The counts
     stay in kv (`fart status` → `schema v4 cleanup`).
3. **Put the CU cap back** in `/opt/fart/.env`: `RPC_DAILY_BUDGET_CU=4500000` → `RPC_DAILY_BUDGET_CU=3000000`
   (the stream is bounded to 43,750 CU per hour at this cap by its daily share; the engine, the log stream
   and the enrichment keep the other 65%). Then
   `sudo systemctl restart fart-ingest fart-engine fart-social fart-notify`.
4. **Verify after 15 minutes:**
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   schema_version: 4
   #   counts: ... watchlist_active=N (early_buyers a few, meta_burst <= 40, probation 20-60): the reserve keeps probation in
   #   freshness (s): ingest.rpc_trades.last_msg_at a few seconds (a watched coin trades), engine.heartbeat a few seconds
   #   rpc CU today by method: ws_notification ... CU = ... bytes (N batches), ws_trade_notification ... CU = ... bytes (...),
   #     ws_trade_subscribe / ws_trade_unsubscribe small; getSignaturesForAddress 40 per call  [cu_per_byte 0.0002]
   #   subscriptions open: N (meta_burst <= 40, probation present, early_buyers a few); closed today: unwatched ..., graduated ...
   #     -> `envelope` and `capped` absent or low; no `address_changed`; the open count stable between two runs a minute apart
   #   top open subscriptions (notifications): curve/probation|early_buyers|meta_burst only
   #   failures: none  (no engine.tick_failed, no ingest.handle_failed.*; rpc_trades.envelope_paused and
   #     rpc_trades.subscription_capped absent or a small number that does not grow every hour)
   #   schema v4 cleanup (...): {"watchlist_graduation_closed": 35, ...}
   journalctl -u fart-ingest --since -15min --no-pager | grep -c '"rpc_trades.subscriptions"'   # ~15: once a minute, not every 5 s
   journalctl -u fart-ingest --since -15min --no-pager | grep 'rpc_trades.subscriptions' | tail -3   # sent=0 ... pending_unsubscribe small
   journalctl -u fart-ingest --since -15min --no-pager | grep -c handle_failed    # 0
   journalctl -u fart-engine --since -15min --no-pager | grep -c tick_failed      # 0
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select count(*), count(distinct mint) from ws_subscriptions where since > strftime('%s','now') - 900"
   #   rows in the order of the new coins of the quarter hour plus <= 40, every mint once (26 Oct before M4: 5,298 rows / 238 mints in 52 min)
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select close_reason, count(*) from ws_subscriptions where until > strftime('%s','now') - 900 group by 1"
   #   unwatched (probation expiries) and graduated; envelope/capped rare
   ```
   The `rpc_trades.limits` line at ingest start names `daily_cu_share` 0.35, `hourly_cu_allowance` 43750,
   `unsubscribe_after_s` 60 and `subscribe_after_syncs` 2.
5. **Calibrate the CU estimate with the Alchemy dashboard** the next day (docs/UNVERIFIED.md, "Solana RPC /
   Alchemy", has the full procedure): for one whole UTC day compare the dashboard's compute units per method
   with
   ```bash
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select method, round(sum(cost)) as cu, sum(bytes) as bytes, count(*) as rows from api_ledger where source='rpc' and blocked_reason is null and ts >= strftime('%s','<day>') and ts < strftime('%s','<day>','+1 day') group by method order by 2 desc"
   ```
   The HTTP methods should match call for call (`getSignaturesForAddress` 40, `getTransaction` 40,
   `getTokenLargestAccounts` 20, `getAccountInfo` 10). For the websocket lines the factor is
   dashboard CU / our bytes: `0.0002` confirms the table; `0.04` means `config/budget.json` →
   `rpc.cu_per_byte` must become `0.04` (the stream then gets 1/200 of the frames per hour; lower
   `rpc_trades_cap` or raise the cap); no websocket CU at all on the dashboard means `0`. Change the value
   in a commit, never on the server, and write the dashboard numbers and date into `docs/UNVERIFIED.md`.
   Until then the CU figures of `fart status` and the daily report are estimates.
6. **The daily report** of the first full M4 day (`out/recall/<day>.md`) carries the new line
   "Graduaties zonder bekende create (coin_unknown, nooit gevolgd)" in "Verrijking en fouten"; those
   graduations are in the universe (`grads_without_create`) and measured by the tracker like every other.
7. **Rollback** to M3 code after the migration: M3 ignores the new columns, but it would re-add
   `graduation` rows only with `subscribe_graduations` (false), so nothing comes back by itself; the
   pre-upgrade `.db` from step 1 is the clean way back. The 35 closed graduation rows stay closed.

## 5d. Upgrade to milestone 5 (M5)

M5 delivers what the measurement week needs and prepares the community step without taking it. On the
server it changes: a **week report** (`fart measure week`, new timer `fart-measure-week.timer`, 06:30 UTC
daily, `out/recall/week-<end>.md|json`) with per-day universe, hit rate, recall, lead time, block-bootstrap
intervals, costs and the **frozen go/no-go test** (`config/measure.json` → `community_gate`,
`docs/DECISIONS.md`); a **community channel** in `fart-notify` that is OFF unless two new `.env` names are
set, plus a **preview mode** that sends the community text to you; the **tracker-coverage line** in the
daily and week report (a graduation without a `mcap_samples` row at least every 10 minutes is "uitkomst
onbekend", never "geen winnaar"; `outcomes` rows and `method_version` m1.0 are unchanged) and the counter
`measure.track.skipped`; and the **M4 leftover**: the 195 `meta_burst` rows milestone 3 wrote kept their
slots after the M4 upgrade (probation got 5 of its 100 reserved slots), so the engine now trims
`meta_burst` rows above 40 every tick and **schema v5** trims them once at the upgrade. No engine rule
changes: the fingerprint of the rule-defining files equals the frozen one of `150ca73cb354` (a test pins
it), so the week report pools the new commit's `rules_version` with the frozen one. Step by step:

1. **Back up first** (the migration is one-way, although it only closes watchlist rows):
   ```bash
   sudo systemctl start fart-backup.service; systemctl show -p Result fart-backup.service   # Result=success
   ls -la /opt/fart/data/backup/
   ```
2. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   * The installer opens the database once as the `fart` user: schema migration 4 → 5 adds no table and no
     column; it **closes the active `meta_burst` watchlist rows above 40**, oldest first, keeping the mints of
     the engine's frozen choice (kv `engine.meta_burst_sets`) and any row that traded in the last 10 minutes.
     It prints `v5 cleanup (meta_burst rows trimmed): {"meta_burst_active_before": N, "meta_burst_closed": N-40,
     "meta_burst_active_after": 40, ...}`; on the VPS expect `before` between 100 and 195 (whatever is left of
     the 195 M3 rows after their idle expiry) and `after` at most 40. A second run prints zeros. The trade
     stream unsubscribes the closed mints by itself (`close_reason` `unwatched`, 60 s later; the restart closes
     the rest with `shutdown`). The counts stay in kv (`fart status` → `schema v5 cleanup`).
   * The installer installs, enables and starts `fart-measure-week.timer` (06:30 UTC daily) and restarts the
     daemons. The engine writes its rules fingerprint next to its rules_version from the first tick.
3. **New names in `/opt/fart/.env`** (all three optional; the defaults are off; the installer never edits the
   file, so add them yourself from `.env.example`):
   ```
   TELEGRAM_COMMUNITY_CHAT_ID=
   TELEGRAM_COMMUNITY_ENABLED=0
   TELEGRAM_COMMUNITY_PREVIEW=0
   ```
   During the measurement week leave the first two exactly like this. `TELEGRAM_COMMUNITY_PREVIEW=1` is
   allowed during the week: you then receive, next to the normal observation batches, the text the community
   would have received, headed **VOORBEELD community-bericht** (kind `community_preview` in `fart notify outbox`),
   under the community rules (one per coin and per theme per 24 h, at most 6 per hour, nothing between 02:00 and
   08:00 Amsterdam time; `config/notify.json` → `community`) and within your own limits (the previews count
   towards the 6 observation batches per hour and the 20 messages per minute). On Mondays at 06:45 UTC the
   preview also brings the weekly accountability message (the go/no-go section in plain words). Setting
   `TELEGRAM_COMMUNITY_ENABLED=1` is the community step itself: not before the week's go/no-go, and only with
   the chat id of the group or channel (usually a negative number; the bot must be a member or admin there).
   After editing: `sudo systemctl restart fart-notify`.
4. **Verify after 15 minutes:**
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   schema_version: 5
   #   telegram community: not configured, disabled (TELEGRAM_COMMUNITY_ENABLED=0 or no chat id), preview to owner off
   #   counts: ... watchlist_active=N (early_buyers a few, meta_burst <= 40, probation 20-100): probation has its reserve again
   #   freshness (s): engine.heartbeat a few seconds; measure.week.last_run appears after the first 06:30 UTC run
   #   tracker: skipped mints total=0 today=0 (measure.track.skipped; max_mints_per_run 600)   <- grows only when a run has more than 600
   #     subjects or DexScreener refuses a batch; a growing number means the samples are thinner than every 5 minutes
   #   rules fingerprints (engine): <new commit>=sha256:e3bf4320c45c   <- the same value as frozen_rules in config/measure.json:
   #     the M5 code runs the frozen rules; the week report pools it with 150ca73cb354
   #   schema v5 cleanup (meta_burst rows trimmed at ...): {"meta_burst_active_before": N, "meta_burst_closed": ..., "meta_burst_active_after": <= 40, ...}
   #   failures: none
   systemctl list-timers 'fart-*' --no-pager                 # fart-measure-week.timer, next elapse tomorrow 06:30 UTC
   journalctl -u fart-engine --since -15min --no-pager | grep -c tick_failed      # 0
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select reason, count(*) from watchlist where active=1 group by 1"
   #   meta_burst <= 40; probation present; the ratio is what the M4 reserve was meant to give
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select close_reason, count(*) from ws_subscriptions where until > strftime('%s','now') - 900 group by 1"
   #   unwatched (the trimmed mints) and shutdown (the restart); nothing else new
   ```
5. **Smoke test of the week report on real data** (reads the database, writes two files, sends nothing):
   ```bash
   sudo -u fart /opt/fart/bin/fart measure week --end 2026-10-27 --days 1
   ls -la /opt/fart/out/recall/week-2026-10-27.*
   sed -n 1,60p /opt/fart/out/recall/week-2026-10-27.md
   ```
   Check in the markdown: the header says `1 dagen, rollend venster`; one segment headed `Regels ... (bevroren
   meetweek)` lists the rules_versions of that day's decisions (`150ca73cb354` and/or the new commit: the same
   fingerprint, so one segment, never two); the
   "Universum per dag" row for 2026-10-27 matches `out/recall/2026-10-27.md` (graduations, winners, hours not
   covered); the interval lines say `te weinig data (1 gedekte dagen (< 3), ...)`; the go/no-go table has six
   rows, none of them `voldaan` by accident on one day; the costs table shows the CU estimate with the
   calibration caveat. Run it after 06:30 UTC on 28 Oct so the day's 24 h outcomes are in; before that the day
   shows `nog bezig` graduations, which is correct, not a fault. The timer's first run is 27 Oct 06:30 UTC with
   `--end 2026-10-26` (the deploy day: partly M3/M4 rules_versions, pooled only where the fingerprint matches).
   The Monday run on 2 Nov covers 26 Oct to 1 Nov; the first full measurement week (27 Oct to 2 Nov) is
   `fart measure week --end 2026-11-02 --days 7` on 3 Nov after 06:30 UTC. Nothing in it decides or sends.
6. **The daily report** (`out/recall/<day>.md`) carries new lines: "Dekking van de tracker" in the summary,
   "Piekuren (12:00 tot 02:00 UTC) niet gedekt" under coverage, a section "Budget en kosten", "Tracker: N munten
   overgeslagen" under "Verrijking en fouten", and "Observaties per rules_version" under "Versies". The counts
   `geen winnaar` and `uitkomst onbekend` now follow the tracker-coverage rule; the `outcomes` rows are unchanged.
7. **Rollback** to M4 code after the migration: M4 ignores nothing new (no columns were added) and would not
   re-add the trimmed rows; disable the new timer (`systemctl disable --now fart-measure-week.timer`) since the
   M4 installer does not know it. The pre-upgrade `.db` from step 1 is the clean way back if you want the rows.

## 6. First-day checklist (docs/UNVERIFIED.md)

1. `fart chain global`: reserves, supply, `fee_basis_points` 95 and `creator_fee_basis_points` 5 must
   match `config/pumpfun.json` (`creator_fee_bps` 5, `total_curve_fee_bps` 100; verified against the IDL
   and the live account on 26 Oct 2026).
2. `fart events classify`: every remaining unknown discriminator should carry an emitting program;
   unknown ones emitted by the pump.fun or pump-amm program are real gaps (`fart/chain/events.py`).
   `--fetch 5` attributes rows written before milestone 2 (five budget-gated `getTransaction` calls).
3. Compare one real create transaction and one pump-amm trade (`trades` with `source='pump_amm'`) with
   `docs/PUMPFUN-EVENTS.md`; `decode.pump_amm_pool_unknown` / `quote_not_wsol` in `fart status` should
   stay small (a swap on a pool we never saw created is expected now and then).
4. Compare `fart status` → `rpc CU today by method` with the Alchemy dashboard, in particular the CU per
   byte of the websocket frames (`ws_notification`, `ws_trade_notification`: 0.0002 or 0.04 per byte, or
   not billed); adjust `config/budget.json` → `rpc.cu_per_byte` in a commit (docs/UNVERIFIED.md, "Solana
   RPC / Alchemy", step by step in section 5c).
5. Watch `coverage --hours 24` for a day: `grads_without_create` should be close to zero.
6. X: compare the prices in `config/x.json` with the developer console; `fart status` → `x spend today`.
7. Telegram: `fart notify test`, then watch `fart notify outbox` for `sent` rows.
8. Week report (M5): `fart measure week --end <yesterday> --days 1` after 06:30 UTC; compare its universe row with
   the daily report of the same day; the bootstrap says `te weinig data` until the window has 3 covered days and
   20 winners.

## 7. Backup and restore

`fart-backup.timer` runs `deploy/backup.sh` daily: SQLite online backup API into
`/opt/fart/data/backup/fart-<stamp>.db.gz` after `PRAGMA quick_check`, keeping 14 days.

Restore (stops the daemons; raw gz files and out/ are untouched):

```bash
sudo systemctl stop fart-ingest fart-engine fart-social fart-notify fart-measure-track.timer
sudo -u fart bash -c 'cd /opt/fart/data && mv fart.db fart.db.broken && gunzip -c backup/fart-<stamp>.db.gz > fart.db'
sudo systemctl start fart-ingest fart-engine fart-social fart-notify fart-measure-track.timer
```

## 8. Rollback

Code rollback is a checkout of the previous commit/tag followed by `install.sh`:

```bash
cd /opt/fart-src && git checkout <previous-commit> && sudo deploy/install.sh
```

Data is forward-compatible within a schema version (`PRAGMA user_version`, see `fart/db.py`).
Rolling back across a schema migration requires restoring the backup taken before the upgrade.
Decisions and outcomes carry `rules_version`/`method_version`, so a rollback never rewrites history:
old rows stay, new rows get the old version string again.

## 9. Troubleshooting

| symptom (`fart status` / logs) | meaning | action |
|---|---|---|
| `pp.trade_subscription_refused` / info message "subscribeTokenTrade and subscribeAccountTrade are only available when connecting with an API key funded with at least some balance" | PumpPortal serves trade subscriptions to funded keys only | expected without `PUMPPORTAL_API_KEY`: fart no longer sends them, trades come from the RPC (`trades today by source: rpc_trades`). With a key: fund the key or remove it from `.env` |
| `pp.connect_gated reason=rate_limit` | more than 12 PumpPortal connects in an hour | wait; check why sessions drop (`pp.disconnected`) |
| `pp.disconnected ... http_status=403/429` repeatedly, `pumpportal last HTTP refusal` | PumpPortal refusing the shared IP | stop other PumpPortal clients on the host (momentdesk also holds one); one connection only; the health alert fires once per hour |
| `rpc_trades.disabled reason=public RPC fallback` | no keyed RPC URL | fill `RPC_HTTP_URL`/`RPC_WSS_URL`; the per-mint trade stream needs Alchemy |
| `rpc_trades.cu_policy_dropping_probation` | the day's CU spend passed 80% of the cap | expected degradation; raise `RPC_DAILY_BUDGET_CU` or lower `rpc_trades_cap` in `config/engine.json` |
| `rpc_trades.envelope_paused` / `status` → `rpc_trades.envelope_paused` counting up every hour | the trade stream spent its hourly share (`daily_cu_share` / 24 of the cap) | expected now and then; every hour means the share is too small for the number of subscriptions: lower `rpc_trades_cap` or `meta_burst_max_total`, or raise `daily_cu_share` in `config/ingest.json` (a commit) only after reconciling `cu_per_byte` with Alchemy |
| `rpc_trades.subscription_capped` (and `rpc_trades.last_capped` in `status --json`) | one coin produced more than 2,000 notifications in an hour | expected: that subscription is dropped for the rest of the hour; the trades it already delivered stay |
| `subscriptions closed today:` shows `unwatched` in the thousands, or `ws_subscriptions` gets many rows per mint | the watchlist churns again (an eviction rule or the meta_burst choice changed) | `tests/test_m4_watchlist_stream.py` is the reproduction; `watch.add()` must evict strictly lower priority only and never reset `since` |
| `subscriptions open:` shows no `probation` for a quarter of an hour while coins are created | probation starved: the shared slots are full and `probation_reserve` is 0 or too small | `config/engine.json` → `watchlist.probation_reserve` (100 of 200); check `counts: watchlist_active=... (probation N)` |
| `rpc_trades.subscriptions` logged every 5 s with `sent>0` | the desired set really changes every sync (not a flicker: the hysteresis absorbs those) | look at `closed today:` reasons; `envelope` every hour = share too small, `stream_cap` = more eligible mints than `rpc_trades_cap` |
| `pp.migration_rejected reason=system_mint:USDC` / `rejected messages today` | PumpPortal put a quote mint in a migration message's `mint` field | expected and harmless now; inspect `rejected_messages` (docs/PUMPFUN-EVENTS.md) |
| `engine.tick_failed` in `failures:` growing | a tick still fails after 3 retries | read `engine.tick_failed.last` in `status --json` and `journalctl -u fart-engine`; a lock holder longer than 15 s is a bug, not expected |
| `ingest.handle_failed.<source>` in `failures:` growing | a handler could not write after 3 retries | the message is in the raw store: `fart ingest replay --source <source> --since <time>` after fixing the cause |
| `fart-backup.service` `Result=signal` (exit 159) | the M2 script's `gzip -f` was killed by the syscall filter (`fchown`) | upgrade to M3 (`deploy/backup.sh` uses `gzip -n -c`); the `.db` written before the kill is complete |
| `ws_subscriptions.notifications` grows but `trades` with `source='rpc_trades'` stays 0 | TradeEvents do not arrive on the bonding-curve address | set `config/ingest.json` → `rpc_trades.mention` to `"mint"` and restart |
| `decode.pump_amm_pool_mismatch` counting up | the pump-amm BuyEvent/SellEvent layout differs from `fart/chain/events.py` | inspect `raw_undecoded` / `fart events classify`; fix the layout (docs/PUMPFUN-EVENTS.md) |
| `global.check_mismatch` at ingest start / `fart chain global` exit 1 | config/pumpfun.json differs from the chain | update the config (one commit = new `rules_version`); a mismatch never stops the daemons |
| `api.budget_cap_reached source=rpc` | daily CU cap hit | raise `RPC_DAILY_BUDGET_CU` or lower sweep/enrich limits in `config/ingest.json` |
| `api.budget_cap_reached source=x` | daily X USD cap hit | expected: the watch pauses until the next UTC day; raise `X_DAILY_BUDGET_USD` if wanted |
| open gap for `rpc_logs` with `budget_cap` | RPC stream paused until the next UTC day | expected degradation; PumpPortal continues |
| open gap for `rpc_sigs` with `sweep_error` | the signature sweep fails (RPC down or budget) | check the `rpc` budget row and `sweep.rpc_unavailable` in the log |
| `grads_without_create` high | creates missed by both streams | check `sweep.done missed=` and `enrich.done`; RPC key present? |
| `decode.truncated_or_drift` counting up | pump.fun changed an event layout | inspect `raw_undecoded`, update `fart/chain/events.py` LAYOUTS |
| `outbox` rows stuck in `pending` with `error` | Telegram unreachable or wrong token/chat id | `fart notify outbox`; fix `.env`; rows retry 15 s → 1 h then `gave_up` |
| `outbox` rows `gave_up` with `chat not allowed` | a row was queued for a chat that is not the owner's while the community channel is off (or was switched off) | expected and safe: nothing left the server; `fart status` → `telegram community` shows the state; set both community names on purpose or leave it |
| `tracker: skipped mints total=N` growing in `status` | more than 600 subjects in a tracker run (`max_mints_per_run`) or DexScreener refused batches | the reports count the affected graduations as `uitkomst onbekend`; raise `max_mints_per_run` in `config/measure.json` (a commit) or accept thinner coverage |
| `counts: ... meta_burst` above 40 for more than one engine tick | the trim did not run (engine down) or the frozen sets hold more than 40 | `journalctl -u fart-engine`; `meta_burst_trimmed` in the tick log; the frozen sets are bounded by `meta_burst_max_total` as well |
| week report says `te weinig data` everywhere | fewer than 3 covered days or fewer than 20 winners in the window | expected early in the week; the Monday report of a full week has the data or says why not |
| `database is locked` | another connection held the write lock for more than 15 s (busy timeout), or a writer used a plain `BEGIN` after reading | every writer uses `BEGIN IMMEDIATE` with 3 retries (`fart/db.py`); a `sqlite3` shell left in a transaction is the usual culprit |
