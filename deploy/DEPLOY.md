# Deploying fart on the VPS (Ubuntu 24.04)

Target layout (shared host, unprivileged user, outbound HTTPS/WSS only, no inbound ports):

```
/opt/fart/            root:fart 0750
  app/                the git checkout copy (read-only for the service)
  bin/fart            wrapper: cd /opt/fart/app && exec /opt/fart/venv/bin/python -m fart "$@" (FART_ROOT set)
  venv/               python3.12 venv with websockets
  data/               fart.db (SQLite WAL), raw/<source>/YYYYMMDD/HH.jsonl.gz, archive/, backup/
  out/recall/         daily reports <day>.json + <day>.md, week reports week-<end>.json + .md (milestone 5)
  logs/               ingest.log, engine.log, social.log, notify.log, measure-*.log, web.log (JSON lines, 5 MB x 5)
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
| `fart-social.service` | X mega-account / KOL watch (60 s / 180 s in the peak hours, 120 s / 300 s outside), the confirmation planner (milestone 7: keys from observations, counts every 15 min then hourly, one search per confirmed key, within `X_DAILY_BUDGET_USD`; idle without `X_BEARER_TOKEN`; since milestone 6 in **shadow mode** by default: nothing of it reaches the decisions, `X_MODE`) | daemon, 15 s loop |
| `fart-notify.service` | Telegram outbox to the owner chat (milestone 2; idle unless `TELEGRAM_NOTIFY_ENABLED=1`) | daemon, 20 s loop |
| `fart-measure-track.timer` | price samples + due outcomes + paper trades | every 5 min |
| `fart-measure-report.timer` | daily recall report for the previous UTC day | 06:00 UTC |
| `fart-measure-week.timer` | week report (rolling 7 days ending yesterday; the Monday run is the Monday-Sunday calendar week): universe, hit rate, recall, lead time, bootstrap intervals, costs, the frozen go/no-go test (milestone 5) | 06:30 UTC daily |
| `fart-backup.timer` | SQLite online backup + quick_check, 14 days kept; since milestone 6 followed by `fart backup verify` (the newest archive unpacked, checked and compared with the live database; a failed check fails the unit) | 05:30 UTC |
| `fart-web.service` | the website (milestones 8 and 9): the public live dashboard, observations, measurement, reports, method and tools pages plus `/api/public/*` (`FART_WEB_PUBLIC=1`, the default; `0` puts everything behind the login as in M8) and the owner section `/eigenaar` behind the login, in Dutch, on `127.0.0.1:8792` only; the shared Caddy serves it as `https://kingdegen.dev`; the database is opened read-only (`mode=ro`, `PRAGMA query_only`), the unit may write only `logs/`; installed always, started only with `FART_WEB_PASSWORD_HASH` and `FART_WEB_SECRET` in `.env` | daemon |

Hardening on every service: `User=fart`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`PrivateTmp`, `PrivateDevices`, `ReadWritePaths=/opt/fart/data /opt/fart/out /opt/fart/logs`,
`CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service`, `UMask=0027`, plus the usual
`Protect*`/`Restrict*` set. The code never signs anything. The only listening socket is the website's
(milestone 8): `fart-web.service` binds `127.0.0.1:8792`, has `ReadWritePaths=/opt/fart/logs` only,
`ReadOnlyPaths=/opt/fart/data /opt/fart/out`, and `IPAddressDeny=any` + `IPAddressAllow=localhost`, so it
can neither write the database nor reach the internet; the shared Caddy is the only thing that talks to it.

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
X_BEARER_TOKEN=                     # optional: X API v2 bearer token -> fart-social active (in shadow mode, see X_MODE)
X_DAILY_BUDGET_USD=15.00            # hard daily cap for all X calls together; milestone 7: the planner is tuned to 15.00 (Jim's $10 to $20) and never exceeds it
X_MODE=                             # milestone 6: off | shadow | live; empty = off without a token, shadow with one; live needs the x_live rules key
TELEGRAM_BOT_TOKEN=                 # optional: bot token from @BotFather
TELEGRAM_OWNER_CHAT_ID=             # the owner's chat id (fart-notify sends nowhere else)
TELEGRAM_NOTIFY_ENABLED=0           # 1 = send; 0 = build nothing, send nothing
TELEGRAM_COMMUNITY_CHAT_ID=         # milestone 5, leave empty: the community chat (group/channel id); never sent to without BOTH set
TELEGRAM_COMMUNITY_ENABLED=0        # milestone 5, leave 0 during the measurement week: 1 = community messages to that chat
TELEGRAM_COMMUNITY_PREVIEW=0        # milestone 5: 1 = the community text to the OWNER, headed "VOORBEELD community-bericht"
FART_WEB_BIND=127.0.0.1:8792        # milestone 8: where fart-web listens (loopback only; Caddy proxies kingdegen.dev to it)
FART_WEB_PASSWORD_HASH=             # milestone 8: the owner's password as an scrypt hash, printed by `fart web hash-password`
FART_WEB_SECRET=                    # milestone 8: the cookie-signing secret, printed by `fart web new-secret`
FART_WEB_PUBLIC=1                   # milestone 9: 1 = the public section needs no login (default, also without the line); 0 = everything behind the login as in M8
FART_ROOT=/opt/fart
```

Leave `RPC_HTTP_URL`/`RPC_WSS_URL` empty to run on the public RPC (`api.mainnet-beta.solana.com`,
strict rate limits, no key, **no per-mint trade stream**). The file is read by the processes directly
(`root:fart 0640`); it is never logged and never copied anywhere. `fart status` prints only
`configured` / `not configured` per key. After editing:
`sudo systemctl restart fart-ingest fart-engine fart-social fart-notify` (and `fart-web` for the two website names).

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
   `sudo systemctl restart fart-ingest fart-engine fart-social fart-notify` (and `fart-web` for the two website names).
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
   `sudo systemctl restart fart-ingest fart-engine fart-social fart-notify` (and `fart-web` for the two website names).
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

## 5e. Upgrade to milestone 6 (M6)

M6 makes the X layer safe to switch on during the measurement week, shows why the coin branch gives few
observations, and closes two operational gaps. No engine rule changes: the fingerprint of the rule-defining
files stays `sha256:e3bf4320c45c` (the same test pins it), so the week report keeps pooling the new commit
with `150ca73cb354`. What changes on the server:

* **`X_MODE`** (new `.env` name, default `off`): with `X_BEARER_TOKEN` set and `X_MODE` empty or `shadow`,
  `fart-social` runs the whole watch (mega accounts every 60 s, KOLs every 180 s, refresh, confirmation counts
  every 15 min per key, fast-post matching) within `X_DAILY_BUDGET_USD`, but **nothing of it reaches the
  decisions**: a fast post becomes a row in `shadow_decisions` (never in `decisions`, so Telegram never sends
  it and the tracker never measures it), a confirmation count a row in `shadow_confirmations` (never in
  `social_counts`), and the engine passes no mega-post feature and neutral X fields to every decision, so the
  decisions of a shadow day are byte for byte those of a day without X (a test compares the two). `live` (the
  milestone-2 behaviour) is only honoured together with `"x_live": true` in `config/engine.json`, which is a
  rules change (a new fingerprint, a new segment in the week report): not during the week. A requested `live`
  without that key runs as shadow and says so (`fart status` → `x mode note`).
* **Schema v6**: three new tables, no new columns, no cleanup: `shadow_decisions` and `shadow_confirmations`
  (append-only like `decisions`) and `engine_candidates` (one row per watched coin and per ranked meta per UTC
  day with the highest threshold features the engine saw; pruned after 30 days by retention). The engine
  writes the candidate rows in its decision transaction from the first tick after the upgrade.
* **New commands**, all read-only or writing only under `data/`: `fart engine explain --day D`, `fart social
  dryrun`, `fart backup verify`, `fart budget calibrate --day D --dashboard-cu N`.
* **Reports**: the daily report gains "Waarom wel of geen observaties" (candidate maxima per threshold, the
  distribution per feature, probation data availability per hour next to the trade-stream gaps) and
  "X-schaduw" (what X would have added, how many existing observations it confirmed within 1 h / 24 h, the
  outcome of the shadow decisions under the hit-rate denominator rules); the week report gains "X-schaduw".
* **Backup**: `deploy/backup.sh` runs `fart backup verify` after every backup; a failed check fails the unit.

Step by step:

1. **Back up first** (the migration only creates tables, but the habit stays):
   ```bash
   sudo systemctl start fart-backup.service; systemctl show -p Result fart-backup.service   # Result=success
   ls -la /opt/fart/data/backup/
   ```
2. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer opens the database once as the `fart` user (schema migration 5 → 6: the three tables above;
   it prints `user_version 6`), installs the units (unchanged) and restarts the daemons. `config/ingest.json`
   gains `retention.candidates_days` 30 and `config/x.json` a `dryrun` block with the assumptions of the dry run;
   neither is a rule file.
3. **`.env`**: add the new name from `.env.example` (the installer never edits the file):
   ```
   X_MODE=
   ```
   Leave it empty (= `off` without a token, `shadow` with one). Jim's own rule applies: the key goes in only
   after a day of watching without X. When the day comes, set `X_BEARER_TOKEN` in `/opt/fart/.env`, leave
   `X_MODE` empty, and `sudo systemctl restart fart-social fart-engine`: the watch runs in shadow and the
   measurement week is untouched. Never set `X_MODE=live` during the week; the engine would refuse it anyway
   until `config/engine.json` carries `"x_live": true` (after the week, in a commit, with the new fingerprint
   written into `config/measure.json` → `community_gate.frozen_rules` on purpose).
4. **Verify after 15 minutes:**
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   schema_version: 6
   #   x: bearer token not configured, mode off (X_MODE unset; live needs "x_live": true in config/engine.json), daily budget $3.00
   #     -> with the token set and X_MODE empty: `mode shadow (X_MODE unset; ...)`; with X_MODE=live during the week an extra line
   #        `x mode note: X_MODE=live refused: ... running shadow`
   #   x shadow: decisions total=0 today=0; confirmations today=0 (confirmed within 1 h 0, within 24 h 0); daemons report social=off engine=off
   #     -> the daemons report the mode they run (kv social.x_mode / engine.x_mode): both must say the same; `not started` means the
   #        daemon has not run since the upgrade. In shadow the counters grow as fast posts and confirmation counts arrive.
   #   engine candidates today: coins=N (with trades N, passed all thresholds N), metas=N (passed all N)  [fart engine explain --day ...]
   #     -> coins counts up with every new probation coin; `with trades` is the data availability of the coin branch
   #   backup: last verify ok at ... (fart-<stamp>.db.gz; coins N, migrations N, decisions N, outcomes N)   <- after the first backup run
   #   failures: none; rules fingerprints (engine): <new commit>=sha256:e3bf4320c45c
   journalctl -u fart-engine --since -15min --no-pager | grep -c tick_failed      # 0
   journalctl -u fart-social -n 5 --no-pager                                       # social.started x_mode=off (or shadow with a token)
   ```
5. **Smoke tests** (nothing is sent, nothing is called; only `backup verify` writes, and only under `data/`):
   ```bash
   sudo -u fart /opt/fart/bin/fart social dryrun                      # the X calls and USD per kind per day, no HTTP
   sudo -u fart /opt/fart/bin/fart engine explain --day 2026-10-27    # why (no) observations: candidates per threshold, distributions
   sudo -u fart /opt/fart/bin/fart backup verify                      # unpacks the newest archive, quick_check, counts vs live
   ```
   Expected: the dry run shows `mega_search` 1440 requests/day, `kol_search` 480, `ca_counts` 96 per key, a
   total under or over the `$3.00` cap with the reasons, and the hour table of the next 24 h (an asterisk marks
   the peak hours; refused kinds name the rule: `hourly_envelope`, `account_watch_reserve`, `kol_daily_cap`,
   `daily_cost_cap`). `engine explain` for 27 Oct shows the candidate counts of the first full measurement day
   (`geen kandidaatgegevens` for a day before the upgrade is correct: the table starts with M6). `backup verify`
   ends with `backup verify: OK` and exit 0; `systemctl start fart-backup.service` now ends with
   `backup: verify ok` in `journalctl -u fart-backup` and `Result=success`; a failed check is `Result=exit-code`
   with `VERIFY FAILED` in the journal (section 7).
6. **The calibration** with the Alchemy dashboard (section 5c, step 5, and docs/UNVERIFIED.md) now has a
   command: for one whole UTC day of M4+ running, read the dashboard's compute units of that day and run
   ```bash
   sudo -u fart /opt/fart/bin/fart budget calibrate --day 2026-10-27 --dashboard-cu <N>
   ```
   It prints our ledger per method, the implied CU per byte of the websocket frames, the documented value it is
   closest to (0, 0.0002 or 0.04) and the `config/budget.json` line that follows; it writes nothing. Change
   `rpc.cu_per_byte` in a commit (not a rule file: the fingerprint stays) and write the dashboard figures into
   docs/UNVERIFIED.md.
7. **The daily report** of 27 Oct (written 28 Oct 06:00 UTC) carries the two new sections; the X-schaduw section
   says `Geen schaduwgegevens` until the token is set. The week report of the measurement week shows the X-schaduw
   totals per day; the go/no-go test does not use them.
8. **Rollback** to M5 code after the migration: M5 ignores the three new tables (nothing was changed in the old
   ones); `fart-backup.service` on M5 code does not run the verify step. The pre-upgrade `.db` from step 1 is the
   clean way back if you want the tables gone.

## 5f. Upgrade to milestone 7 (M7)

M7 turns the X confirmation layer into a cost-aware planner that gets the most confirming information out of a daily cap
of $10 to $20 and spends nothing that adds no information. No engine rule changes: the fingerprint of the rule-defining
files stays `sha256:e3bf4320c45c` (the same test pins it; `config/x.json` is not a rule file), so the week report keeps
pooling the new commit with `150ca73cb354`. What changes on the server:

* **Keys and order**: confirmation keys come only from observations (per `observe` decision the mint, a non-generic
  `$TICKER` of >= 3 characters, the exact multi-word name; of the themes only the 5 strongest `observe_meta` terms of
  >= 8 characters), youngest coin observation first, themes last, at most 20 keys at once. A key stops as soon as it is
  decided and `x_keys` (schema v7) says why: `confirmed` (last-hour count >= 10, then exactly one search), `refuted`
  (three consecutive counts without growth: a silent coin costs 4 counts, 45 minutes, $0.02), `expired` (6 h),
  `graduated`, `dead`, `displaced`, `exhausted`.
* **Schedule**: counts at 0, 15, 30, ..., 120 minutes after the decision and hourly after that (at most 12), one search
  of at most 10 posts with a user lookup of at most 3 authors (`GET /2/users`, kind `ca_authors`); mega watch 60 s in the
  peak hours (13:00 to 02:00 UTC) and 120 s outside, KOL watch 180 s / 300 s, refresh unchanged. Full load: 60 keys a day
  x (12 x $0.005 + 10 x $0.005 + 3 x $0.010) = **$8.40 a day** as the ceiling of the confirmation layer, $9.89 with the
  account watches = 66% of a $15.00 cap (docs/UNVERIFIED.md has the formula; M6 would have cost about $119 a day).
* **Planner**: the hard rules (the cap, the 30% reserve for mega + KOL, the $1 KOL sub-cap) are checked against the
  ceiling cost of every call, so the cap can never be exceeded; the hourly envelope now **defers** a call to the next hour
  instead of refusing it (counted in kv, logged once per hour, never a blocked row). Inside a round the search of a
  confirmed key goes first, then the counts (youngest coin first), then the account watches.
* **Reports**: the "X-schaduw" section of the daily and the week report gains the quality-per-dollar lines (cost per
  confirmed observation, share confirmed within 1 h, counts after the first confirmation, keys stopped per reason with
  the cost of the refuted ones, the 24 h outcome of confirmed versus unconfirmed coin observations under the hit-rate
  rules). No advice: Jim decides on these figures after the week.
* **New command**: `fart social calibrate --day D --console-usd N` (read-only). `fart social dryrun` prints the full-load
  formula and the planned versus actual USD per kind; `fart status` gains `x planner` and `x plan vs spend today`.
* **Schema v7**: the table `x_keys` and the column `x_spend.unit`; no cleanup, idempotent.

Step by step:

1. **Back up first** (the migration only adds a table and a column, but the habit stays):
   ```bash
   sudo systemctl start fart-backup.service; systemctl show -p Result fart-backup.service   # Result=success
   ls -la /opt/fart/data/backup/
   ```
2. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer opens the database once as the `fart` user (schema migration 6 → 7; it prints `user_version 7`),
   installs the units (unchanged) and restarts the daemons. `config/x.json` changes shape: `mega.interval_peak_s` /
   `interval_off_s` and `kol.interval_peak_s` / `interval_off_s` replace the single intervals, `confirm` loses
   `interval_s` and `max_keys_per_loop`, the new blocks `planner` and `dryrun` carry the planner's constants and the dry
   run's assumptions. It is not a rule file. Rows in `x_spend` written before M7 keep working (no unit: `mixed`).
3. **`.env`**: set the daily cap the planner is tuned to and leave the mode empty (the installer never edits the file):
   ```
   X_DAILY_BUDGET_USD=15.00
   X_MODE=
   ```
   15.00 is inside Jim's $10 to $20; the planner never exceeds the cap and the full load is 66% of it. Any value between
   10.00 and 20.00 works the same way (Jim adjusts it with his helper); below about 10.00 the dry run says the full load
   does not fit, and without the line the code falls back to the milestone-2 default of 3.00, under which only a few keys
   a day fit. `X_MODE` stays empty: `off` without a token, `shadow` as soon as `X_BEARER_TOKEN` is set (milestone 6:
   nothing reaches the decisions during the week; `live` needs the `x_live` rules key, after the week). Then
   `sudo systemctl restart fart-social fart-engine`.
4. **Check the plan against the cap before the key goes in** (no HTTP, nothing written):
   ```bash
   sudo -u fart /opt/fart/bin/fart social dryrun
   ```
   Expected: `cap $15.00/day`; a `planner:` line with the count schedule `0, 15, 30, 45, 60, 75, 90, 105, 120, 180, 240,
   300 min after the decision (max 12)`; the kinds table with `mega_search 60/120 s 1110` requests a day, `kol_search
   180/300 s 392`, `ca_counts 900/3600 s`, `ca_search`, `ca_authors`; the `total` line ending in `fits the cap of
   $15.00`; the `full load:` line `60 keys/day x (12 counts + 1 search of 10 posts and 3 authors) = $8.40/day for the
   confirmation layer [60 x (12 x $0.005 + 10 x $0.005 + 3 x $0.010) = 60 x $0.140 = $8.40]` followed by
   `+ account watches $1.49 ... = $9.89/day = 66% of the cap $15.00 -> fits; against the planner's target cap $15.00:
   fits`; the hour table with `deferred` only in the morning hours (if at all) and `refused` empty; `observed rates:
   none (assumptions used)` until the daemon ran with a token. A note `De planner is afgestemd op X_DAILY_BUDGET_USD
   15.00; .env zegt ...` means step 3 was skipped.
5. **Verify after 15 minutes:**
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   schema_version: 7
   #   x: bearer token not configured, mode off (X_MODE unset; ...), daily budget $15.00
   #   x planner: active keys 0, search pending 0, due now 0; started today 0; stopped today: none; deferred today: none
   #     -> with the token set: active keys N (mint a, meta b) with a <= 20, stopped today: confirmed n ($..), refuted n ($..), expired ...;
   #        deferred today: ca_counts N only in the morning hours; `blocked` must not grow in the x budget row
   #   x plan vs spend today (planned/actual USD, % of the $15.00 cap): mega_search $0.49/$0.00 (3%/0%), refresh $0.00/$0.00 (0%/0%),
   #     kol_search $1.13/$0.00 (8%/0%), ca_counts $.../$0.00 (..%/0%), ca_search $.../$0.00, ca_authors $.../$0.00
   #     -> planned = the dry-run model for a day with the current keys and rates; actual = x_spend; both as a share of the cap
   #   x spend today: (empty until the token runs) -> then per kind with the units: ca_counts $0.030 (6) ...
   #   x shadow: decisions total=0 today=0; confirmations today=0 (...); daemons report social=off engine=off
   #   failures: none; rules fingerprints (engine): <new commit>=sha256:e3bf4320c45c
   journalctl -u fart-social -n 5 --no-pager                                       # social.started x_mode=off (or shadow with a token)
   journalctl -u fart-engine --since -15min --no-pager | grep -c tick_failed      # 0
   ```
6. **When the token goes in** (Jim's rule: after a day of watching without X; `X_BEARER_TOKEN` in `.env`, `X_MODE` empty,
   `sudo systemctl restart fart-social`): the watch runs in shadow, the planner admits the current observations as keys
   (`x planner: active keys ...` in `fart status`, `x.key_confirmed` / `x.key_stopped` lines in `journalctl -u
   fart-social`), and `x spend today` shows per kind what it costs. After the first full UTC day:
   ```bash
   sudo -u fart /opt/fart/bin/fart social dryrun          # "observed rates: ... stopped keys: ..% confirmed, ... counts/key" from 5 stopped keys on
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select stop_reason, count(*), round(avg(counts_n),1), round(sum(usd),2) from x_keys where stopped_at is not null group by 1"
   ```
   Expected: a day's spend well under the $8.40 ceiling of the confirmation layer; refuted keys with small `counts_n`
   (4 to 6), confirmed keys with `searches_n` 1 and `counts_n` small; no `hourly_envelope` rows in `api_ledger`
   (`select blocked_reason, count(*) from api_ledger where source='x' and blocked_reason is not null group by 1` shows
   at most `kol_daily_cap` in the evening).
7. **Calibrate the prices with the X developer console** (docs/UNVERIFIED.md, "X (Twitter) API v2", has the full
   procedure): read the console's amount for one whole UTC day with the token and run
   ```bash
   sudo -u fart /opt/fart/bin/fart social calibrate --day 2026-10-28 --console-usd <N>
   ```
   It prints our `x_spend` rows per kind and unit next to the console's amount, the gap, the uniform factor and per
   component the price that would explain the gap, each with the `config/x.json` → `prices` line it implies; `verdict:
   confirmed` within 5% (write the console figures into docs/UNVERIFIED.md and tick the item), `verdict: adjust`
   otherwise (a commit of `config/x.json`, not a rule file: the fingerprint stays; which lines, the console's
   per-endpoint breakdown decides). It writes nothing.
8. **The daily report** of the first shadow day carries the new "Kwaliteit per dollar" lines in its "X-schaduw" section
   (cost per confirmed observation, share confirmed within 1 h, waste, stopped keys per reason, outcomes by confirmation);
   the week report has the same as totals and a table per day. Neither gives advice; the go/no-go test does not use them.
9. **Rollback** to M6 code after the migration: M6 ignores `x_keys` and `x_spend.unit`, but it reads `confirm.interval_s`
   and `mega.interval_s` from `config/x.json`, which the M7 checkout no longer has: roll back the whole checkout
   (`git checkout <M6 commit> && sudo deploy/install.sh`), never only the code. The pre-upgrade `.db` from step 1 is the
   clean way back if you want the table gone. Put `X_DAILY_BUDGET_USD` back to 3.00 when rolling back: the M6 layer
   spends up to the cap.

## 5g. Milestone 8: website (M8)

M8 adds the website `https://kingdegen.dev`: a dashboard as its first page (health and freshness of the daemons and
timers, the coverage of the last 24 hours per hour, today's decisions, the X state, the planner and plan versus
spend, the budgets, the key points of the latest daily report and the go/no-go section of the latest week report),
`/observaties` (the last 200 decisions with features, rules_version and outcome status), `/rapporten` (every
`out/recall/*.md` rendered as HTML) and `/instellingen` (read-only: rules_version, fingerprint, method_version, the
frozen go/no-go criteria, the active configuration without secrets), plus the same data as JSON under `/api/status`,
`/api/coverage`, `/api/decisions`, `/api/x`, `/api/report/latest` and `/api/week/latest`. Everything is behind a
login (one owner account), everything is read-only, also for the owner: there is no button that changes anything.
No rule file changes (the fingerprint stays `sha256:e3bf4320c45c`; the same test pins it), no schema migration, no new
dependency (standard library only), none of the existing daemons is touched.

How it is built on the VPS, and why: the site runs **on the VPS itself**, as `fart-web.service` (user `fart`, the
hardening of the other units, `127.0.0.1:8792` only, the database opened read-only, writing nothing but
`logs/web.log`), behind the **existing shared Caddy** (`larp-scanner-https.service`, which already holds the
certificate machinery and the ports 80/443 for the other projects). Nothing is pushed to another host, no new port is
opened. The login is a password whose scrypt hash lives in `.env` next to the other secrets, with a signed session
cookie (HttpOnly, Secure, SameSite=Strict, 12 hours), a CSRF token on the form, and a login guard (5 failed attempts
per 10 minutes per IP and per account, then 15 minutes of waiting; counted in memory and replayed from `web.log` at a
restart, because the website may not write the database's `kv`). Every response carries a strict
Content-Security-Policy (`default-src 'self'`, no inline scripts: one small CSS and one small JS file of our own, no
external script, font or tracker), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`; requests over 64 KB are refused; the log never carries a cookie, a password or a token. The
honesty rules of the reports apply to every number on the site: observation, no advice; unknown stays unknown; an hour
or day NIET GEDEKT stays empty; no profit or return figure anywhere.

What the deployer does, step by step (all as root on the VPS unless stated; `.env` is edited by Jim's helper, see
step 3):

1. **Pull and install** (the usual update; no migration this time):
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer installs `fart-web.service` but **does not start it**: it prints `fart-web.service installed but NOT
   started: set FART_WEB_PASSWORD_HASH ... and FART_WEB_SECRET ...` until both values exist in `/opt/fart/.env`. The
   other daemons restart as always. `fart status` gains the line `web: password hash not configured, secret not
   configured, bind 127.0.0.1:8792`.
2. **Make the two values** (nothing is written anywhere by these commands; they only print):
   ```bash
   sudo -u fart /opt/fart/bin/fart web hash-password     # asks the password twice, invisibly; prints ONLY the hash (scrypt:16384:8:1:<salt>:<key>)
   sudo -u fart /opt/fart/bin/fart web new-secret        # prints 64 hex characters
   ```
   Jim types the password himself at the first prompt (at least 12 characters); the hash is what goes into `.env`,
   never the password. The hash carries no `$`, `=` or quote, so a shell helper can pass it as it is.
3. **Put them in `.env` with Jim's helper.** `/root/fart-setsecret.sh` does not know the two new names yet. What to add
   to it: the names `FART_WEB_PASSWORD_HASH` and `FART_WEB_SECRET` to its list (or `case`) of allowed keys, and
   `fart-web.service` to the units it restarts after a change (only `fart-web` needs a restart for these two names; the
   other daemons never read them). The two lines in `/opt/fart/.env` are then:
   ```
   FART_WEB_PASSWORD_HASH=scrypt:16384:8:1:<salt hex>:<key hex>
   FART_WEB_SECRET=<64 hex characters>
   ```
   (`FART_WEB_BIND` may stay absent: the default is `127.0.0.1:8792`; `.env` stays `root:fart 0640`.) Without the
   helper the same two lines go in with an editor as root, after which `chmod 0640 /opt/fart/.env && chown root:fart
   /opt/fart/.env`. A new secret logs every browser out; a new hash changes the password; neither touches anything else.
4. **Start the website on the loopback and check it before Caddy knows it:**
   ```bash
   sudo systemctl enable --now fart-web
   systemctl status fart-web --no-pager                       # active (running)
   journalctl -u fart-web -n 5 --no-pager                     # web.started bind="127.0.0.1:8792" ... readonly=true
   curl -sI http://127.0.0.1:8792/login | head -8             # HTTP/1.1 200, Content-Security-Policy, X-Frame-Options: DENY, Set-Cookie: fart_csrf=...
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8792/api/status   # 401 without a session
   ```
   A unit that ends at once with `fart web: FART_WEB_PASSWORD_HASH is ...` in the journal means step 3 is incomplete
   (exit status 2, no restart loop: `RestartPreventExitStatus=2`); after fixing `.env`, `systemctl restart fart-web`.
   (Re-running `sudo deploy/install.sh` after step 3 also enables and starts the unit.)
5. **Give Caddy the site file.** Caddy runs as a `DynamicUser` and cannot read `/opt/fart` (`root:fart 0750`), so the
   file is copied to a place it can read instead of imported from `/opt/fart/app` directly (an import from `/opt/fart`
   would make `caddy reload` from a root shell succeed and the next restart of Caddy fail for every site):
   ```bash
   install -d -m 0755 -o root -g root /srv/fart
   install -m 0644 -o root -g root /opt/fart/app/deploy/caddy/fart.caddy /srv/fart/fart.caddy
   ```
   From now on every `deploy/install.sh` run refreshes `/srv/fart/fart.caddy` when the repository's copy changed (it
   prints a line saying so) and never reloads Caddy itself. The file holds one site block for `kingdegen.dev,
   www.kingdegen.dev` with `encode zstd gzip`, `request_body { max_size 64KB }`, HSTS plus the three simple security
   headers as defaults (`?`, deferred until the upstream answered), `-Server`, and `reverse_proxy 127.0.0.1:8792`:
   plain Caddy 2.6.2, no plugin. If `www.kingdegen.dev` has no A record, remove it from the first line before
   validating (Caddy would otherwise keep trying to obtain a certificate for it and log the failures).
6. **Edit the shared Caddyfile, with a copy first:**
   ```bash
   STAMP=$(date -u +%Y%m%d-%H%M%S)
   cp -a /srv/larp-scanner/https/Caddyfile /srv/larp-scanner/https/Caddyfile.before-fart-$STAMP
   cp -a /srv/meme-radar-launch/launch.caddy /srv/meme-radar-launch/launch.caddy.before-fart-$STAMP
   grep -n kingdegen /srv/meme-radar-launch/launch.caddy /srv/larp-scanner/https/Caddyfile
   ```
   In `/srv/meme-radar-launch/launch.caddy` take **only** `kingdegen.dev` out of the site address line of the old
   meme-radar-launch block (the "tijdelijk offline" page): `launch.kingdegen.dev` and the sslip host stay exactly as
   they are (mind the comma that separated the hosts). Then add the import at the end of
   `/srv/larp-scanner/https/Caddyfile`, next to the imports of the other projects:
   ```bash
   echo 'import /srv/fart/fart.caddy' >> /srv/larp-scanner/https/Caddyfile
   ```
   A host that is still in both blocks makes the validation of the next step fail (`ambiguous site definition`), so a
   forgotten edit cannot go live.
7. **Validate before anything is reloaded** (`caddy validate`), as a user that cannot read `/opt/fart` (that mimics the DynamicUser):
   ```bash
   CADDY=/srv/larp-scanner/https/extracted/usr/bin/caddy
   sudo -u nobody $CADDY validate --config /srv/larp-scanner/https/Caddyfile --adapter caddyfile
   ```
   `Valid configuration` is the only acceptable answer (if `nobody` cannot run that binary, validate as root and check
   `ls -l /srv/fart/fart.caddy` is `-rw-r--r-- root root` in a `0755` directory by hand). Fix and repeat until valid.
8. **Reload.** Preferably through the admin API, which touches no other site:
   ```bash
   $CADDY reload --config /srv/larp-scanner/https/Caddyfile --adapter caddyfile
   ```
   `caddy reload` reads the Caddyfile as root, adapts it and posts the result to the admin endpoint (localhost:2019
   by default); if that endpoint is off in this Caddy (`admin off`, or the command says it cannot connect), the
   alternative is `systemctl restart larp-scanner-https`, which **interrupts every site on that Caddy for a few
   seconds** (the other projects included): say so in the channel before doing it, and prefer a quiet hour. Caddy then
   obtains the certificate for `kingdegen.dev` (and `www`) on first use; the first request may take a few seconds.
9. **Check from outside:**
   ```bash
   curl -sI https://kingdegen.dev | head -12
   #   HTTP/2 401 (the login page: correct without a session), content-security-policy, x-content-type-options: nosniff,
   #   referrer-policy: no-referrer, x-frame-options: DENY, strict-transport-security, no `server:` line
   curl -sI https://kingdegen.dev/login | head -3          # HTTP/2 200
   curl -sI https://launch.kingdegen.dev | head -3         # still the old block (200 or whatever it served before)
   journalctl -u larp-scanner-https --since -10min --no-pager | tail -20   # certificate obtained; no error for kingdegen.dev
   journalctl -u fart-web -n 20 --no-pager                 # web.request lines with ip=<Jim's address>, never a cookie or token
   ```
   Then Jim opens `https://kingdegen.dev` in a browser, logs in with his password and sees the dashboard: the banner
   "Observatie, geen advies. Alles wordt achteraf gemeten.", the health table (every daemon `vers`; the report and week
   timers `oud` or `nooit` until their first run after the upgrade is in the past), the coverage table with the hours
   fart was down as `NIET GEDEKT` and empty, today's decisions, the X card (`uit` until the key goes in), the budgets, the
   latest daily report's key points and the week report's go/no-go table. The page refreshes its numbers every 60 s;
   five wrong passwords lock the login for 15 minutes (the page says so with the minutes left).
10. **Rollback** (any step after 6 goes wrong): put the saved copies back and reload or restart Caddy the same way:
    ```bash
    cp -a /srv/larp-scanner/https/Caddyfile.before-fart-$STAMP /srv/larp-scanner/https/Caddyfile
    cp -a /srv/meme-radar-launch/launch.caddy.before-fart-$STAMP /srv/meme-radar-launch/launch.caddy
    sudo -u nobody $CADDY validate --config /srv/larp-scanner/https/Caddyfile --adapter caddyfile && $CADDY reload --config /srv/larp-scanner/https/Caddyfile --adapter caddyfile
    ```
    `kingdegen.dev` then shows the "tijdelijk offline" page again. The website itself is stopped with `systemctl disable
    --now fart-web`; removing the two names from `.env` is not needed for that. A rollback of the code to M7 (`git
    checkout <M7 commit> && sudo deploy/install.sh`) leaves `fart-web.service` in `/etc/systemd/system/` as a unit
    without a program: disable it first (`systemctl disable --now fart-web`), then roll back.

Notes for the owner, in Dutch, are in `README.md` ("wat milestone 8 toevoegt") and `docs/DECISIONS.md` (milestone
8); the design is in `docs/ARCHITECTURE.md` ("Website"); the assumptions about this Caddy to verify at the rollout are
in `docs/UNVERIFIED.md` ("Website / Caddy").

## 5h. Milestone 9: website v2 (M9)

M9 rebuilds the website as a public product. Without a login `https://kingdegen.dev` shows a **live dashboard** of what the
radar sees right now (where the money goes on the watched coins in the last five minutes, the newest coins and their tempo,
the themes that come up in the coin names, the latest graduations with the tracker's newest sample, what goes round on X
once that layer runs, today's observations, one line of measurement), the observations with their outcome (`/observaties`),
the measurement (`/meting`: the frozen week with the go/no-go test, the coverage of the last 24 hours, the latest reports,
how it works), every report as a page (`/rapporten`, `/rapporten/<day>`, `/rapporten/week-<day>`), the method with the
glossary and the full disclaimer (`/methode`) and a placeholder for the tools (`/tools`); the same data as JSON under
`/api/public/*`. The owner section moved to `/eigenaar` (health and freshness, budgets, the X planner, the settings, a log
summary; the raw Markdown of every report under `/rapporten/<file>.md`; the milestone-8 JSON under `/api/*`) and stays
behind the login exactly as in M8 (password, signed cookie, CSRF, lockout). `/instellingen` redirects to `/eigenaar`.

New on the server: `config/web.json` (the brand: site name, product word, tagline, domain, accent colour `#rrggbb`; not a
rule file; edit it in a commit, never on the server), the `.env` name `FART_WEB_PUBLIC` (1 = public, the default even
without the line; 0 = everything behind the login as in M8), an in-memory cache per view (15 s for the live views, 30 s for
the rest) so public traffic never multiplies the read-only queries, `Cache-Control` and `ETag` on every public answer (a
matching `If-None-Match` is a 304), a limit of 120 requests per minute per client address on the public routes (then 429
with `Retry-After`; the owner's session is never limited), and pages that refresh themselves every 15 s (the dashboard)
or 30 s. The Caddy site file lost `www.kingdegen.dev` (no DNS record; add it back in a commit once the A record exists).
No rule file changes (the fingerprint stays `sha256:e3bf4320c45c`; the same test pins it), no schema migration, no new
dependency, the daemons untouched, the service still read-only.

1. **Pull and install** (no migration):
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer refreshes `/srv/fart/fart.caddy` (it prints a line saying so) and restarts `fart-web`. `config/web.json`
   comes with the checkout.
2. **`.env`** (optional): nothing is needed. `FART_WEB_PUBLIC=0` (Jim's helper: add the name to its allowed keys; only
   `fart-web` needs a restart) puts everything behind the login again at once; `1` or no line is the public site.
3. **Caddy**: the site file changed, so validate and reload exactly as in section 5g, steps 7 and 8 (`sudo -u nobody $CADDY
   validate ...`, then `$CADDY reload ...`). `www.kingdegen.dev` is no longer served by the fart block; nothing else changes.
4. **Check from outside:**
   ```bash
   curl -sI https://kingdegen.dev/ | head -12                 # HTTP/2 200, cache-control: public, max-age=15, etag, content-security-policy, no server line
   curl -sI https://kingdegen.dev/meting | head -3            # HTTP/2 200
   curl -sI https://kingdegen.dev/eigenaar | head -3          # HTTP/2 401 (the login page)
   curl -s -o /dev/null -w '%{http_code}\n' https://kingdegen.dev/api/status          # 401
   curl -s https://kingdegen.dev/api/public/status | head -c 600; echo              # the public JSON: site, live numbers, freshness, week
   curl -s https://kingdegen.dev/api/public/live | head -c 400; echo                # the live hero: tempo, money on the watched coins, graduations
   ```
5. **Smoke test: no owner field in the public JSON** (the test suite proves it on the fixture world; this is the same check
   on the real data):
   ```bash
   for ep in status live hot new themes graduations social coverage observations reports week; do
     curl -s "https://kingdegen.dev/api/public/$ep" | grep -Eio 'budget|secret|password|token|planner|heartbeat|x_mode|/opt/|8792|daemon' \
       && echo "LEAK in $ep" || echo "$ep clean"
   done
   ```
   Every line must say `clean`. The `/api/public/status` document carries `site`, `day`, `refresh_s`, `warning`,
   `banner`, `week`, `freshness`, `live`, `totals` and `headline`: nothing operational.
6. **The rate limit** (from one address; wait a minute afterwards):
   ```bash
   for i in $(seq 1 125); do curl -s -o /dev/null -w '%{http_code}\n' https://kingdegen.dev/api/public/status; done | sort | uniq -c
   #   120 200 and 5 429
   ```
   Behind Caddy the client address is the last `X-Forwarded-For` entry; if every visitor seems to share one address, check
   that Caddy's `reverse_proxy` passes the header (it does by default) and that `fart-web` sees the loopback as its peer.
7. **In the browser**: the dashboard with the live cards and the `live` pill, the numbers updating every 15 s (the footer
   line says when), `/meting` with the week, the coverage strip and the reports, `/methode` with the glossary, and
   `/eigenaar` after the login with everything operational. Dark by default, light with the system setting; everything
   usable at 375 px wide without horizontal scrolling. `fart status` says `public section on`.
8. **Rollback**: `FART_WEB_PUBLIC=0` plus `systemctl restart fart-web` hides the public site at once (no deploy). A code
   rollback to M8 (`git checkout <M8 commit> && sudo deploy/install.sh`) brings back the M8 pages; its Caddy file carries
   `www.kingdegen.dev` again, so validate before reloading.

## 5i. Milestone 10: performance (M10)

M10 makes the public dashboard fast and robust without changing its design or its content. Measured on the VPS right
after the M9 rollout (26 Oct 2026, 21:50 to 22:00 UTC; 174,305 trades, 34,635 coins, 66,229 mcap samples, 884 decisions):
`GET /` timed out twice after 60 s and then answered with 35 KB; cold, `/api/public/themes` took more than 150 s, `status`,
`live` and `coverage` about 7 s each. Three causes, three changes:

* **The themes came from the website, not from the engine.** `themes_view` repeated the engine's whole clustering on every
  cache miss (7,486 coins in the three-hour window, one baseline scan per term: minutes per visit). Now the engine writes
  the ranked metas it computes every tick anyway as JSON in kv (`engine.metas_latest`, in the same short decision
  transaction as `engine.meta_burst_sets`; `fart/engine/themes.py`), and the themes card reads only that value and computes
  nothing. A snapshot older than 5 minutes is never shown as current: the card says "Thema’s worden nu niet berekend" with
  the age of the last one and no figure. The daily report and `fart engine explain` keep their own path.
* **No index for time windows.** Every window over all trades (`... FROM trades WHERE received_at >= ?`) scanned the table
  through `trades_mint_received` (720 ms per query). Schema **v8** (`user_version 8`) adds `trades(received_at)` and
  `trades(block_time)`; every time window of the public views is bounded on the indexed `received_at` first (a minute wider
  than the window) and applies the event time `COALESCE(block_time, received_at)` to the rows in that range, so no window
  scans a table (a test proves it with `EXPLAIN QUERY PLAN`). The coverage of the last 24 hours comes from the `coverage`
  table the ingest already recomputes every minute, instead of being counted again per view.
* **The cache made every request wait.** With a TTL of 15 s and computations of seconds to minutes the dashboard was
  computing nearly all the time, and every request waited for it. Now one background thread in `fart web` computes the
  public views on their schedule (at start, then every 15 s for the live views and every 30 s for the rest, with its own
  read-only connection and short transactions) and a request only reads the last value: it never waits. A view that was
  never computed shows "wordt berekend" on the page and `{"state": "computing"}` in the JSON (HTTP 200, `Cache-Control:
  public, max-age=2`). A computation longer than 10 s is logged (`web.view_slow` with the view's name and its duration);
  the last duration per view is on `/eigenaar` ("Weergaven van de website") and in `fart status` (`web views: themes
  0.02 s, live 0.4 s, ...`) through `logs/web-views.json`, the one file the website writes next to its log. The database
  stays read-only for the website.

No rule file changes (the fingerprint stays `sha256:e3bf4320c45c`; the same test pins it), no new dependency, no change
of design or content: every card shows what it showed, plus the state "wordt berekend" where a value does not exist yet.

1. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer applies schema v8 before the daemons restart (`database schema: user_version 8 ...`): the two indexes on
   174,000 trade rows take seconds, inside one short write transaction (the running daemons retry a busy write, as always).
   It then restarts `fart-ingest`, `fart-engine` and the optional services, `fart-web` included. Both restarts matter: the
   engine writes the kv metas only from this version on, the web service runs the new cache only after its restart. If the
   installer ran with `START_SERVICES=0`, do it by hand:
   ```bash
   sudo systemctl restart fart-engine fart-web
   ```
2. **Check the schema and the engine:** `sudo -u fart /opt/fart/bin/fart status` shows `schema_version: 8`; after one engine
   tick (15 s) the kv value exists and is fresh:
   ```bash
   sudo -u fart sqlite3 /opt/fart/data/fart.db "select length(value), datetime(updated_at, 'unixepoch') from kv where key='engine.metas_latest'"
   journalctl -u fart-engine -n 5 --no-pager                  # engine.tick, no engine.tick_failed
   ```
3. **Check the dashboard from outside, cold and warm**, both under one second:
   ```bash
   curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://kingdegen.dev/      # 200 and a time under 1 s, right after the restart (cold)
   curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://kingdegen.dev/      # 200 and a time under 1 s again (warm)
   for ep in status live coverage themes hot new graduations observations; do
     curl -s -o /dev/null -w "$ep %{http_code} %{time_total}s\n" "https://kingdegen.dev/api/public/$ep"
   done                                                                                  # every line 200 and well under 1 s
   curl -s https://kingdegen.dev/api/public/themes | head -c 300; echo                   # "state": "fresh" with rows, after the engine's first tick
   ```
   In the first seconds after the restart a card can say "wordt berekend" (the thread's first cycle) and the themes card
   "Thema’s worden nu niet berekend" until the engine has ticked once; both disappear by themselves within a minute.
4. **The view times:** `fart status` prints one line with the last duration per view, as the website wrote it:
   ```
   web views: themes 0.02 s, live 0.4 s, hot 0.1 s, new 0.05 s, ..., report pages 14 (slowest 0.01 s)  [written 7 s ago]
   ```
   `status`, `live` and `coverage` must stay under 0.3 s; a view above 10 s appears in `journalctl -u fart-web` as
   `web.view_slow`; `/eigenaar` → "Weergaven van de website" has the same table with the errors per view. `no timings yet`
   means `fart-web` has not completed a cycle since the restart (or is not running).
5. **Rollback:** a code rollback to M9 (`git checkout <M9 commit> && sudo deploy/install.sh`) works on the v8 database: the
   two indexes are harmless to M9, which never drops them, and M9 ignores the kv value `engine.metas_latest`.

## 5j. Milestone 11: engine performance (M11)

M11 brings the engine tick back from minutes to seconds without changing one outcome. Measured on the VPS after the M10
rollout (26 Oct 2026, 22:58 to 23:19 UTC; 199,000 trades, 37,500 coins, about 7,100 coins in the three-hour window, about
1,200 new coins per hour): `tick_s` in the `engine.tick` log line was 166 to 337 s (4 s at 14:00 UTC), the engine process
sat at 97% CPU, the heartbeat aged up to 565 s, decisions and `engine.metas_latest` came every three minutes instead of every
15 s, and the themes card of the website said "nu niet berekend" most of the time. `features_s` was 0.35 s; the time was in
step 5 (the metas): `meta_features` fetched the baseline coins of every term (6 to 48 hours before `as_of`, tens of thousands
of rows) and tokenized them again per term, hundreds of times per tick. Two changes, one proof:

* **The baseline once per tick** (`fart/engine/features.py` → `meta_baseline`, `MetaBaseline`): the baseline coins are
  fetched once per tick as an index range on the event time (the `coins_block_time` index serves both the rows with a chain
  time and the received-only rows; the old form walked every coin through `coins_received_at`), tokenized once and counted
  per term (`collections.Counter`); `meta_features` reads its `base_n` from that counter. The definition is unchanged: the
  same window on the same event time `COALESCE(block_time, received_at)`, the same `tokenize` with the same stopwords and
  `min_token_len`, the same `base_hours`, the same rounding. `recent_coins`, the clustering, the per-term trade aggregate
  (through the mint index) and the ranking are what they were. On the fixture database of the proof (20,000 coins, 50,000
  trades) step 5 goes from 13 s to 0.3 s; on the VPS database the whole meta step is expected under 5 s, linear in the number
  of coins and no longer in coins times terms.
* **A tick budget that is visible** (`fart/engine/service.py`): every tick logs the duration of each of its six steps
  (`engine.tick_steps` with `expire_s`, `features_s`, `promote_s`, `rpc_s`, `metas_s`, `decisions_s` and `tick_s`; the
  `engine.tick` summary carries them too), a tick longer than the loop interval (15 s) is counted in kv (`engine.tick_slow`,
  `engine.tick_slow.<day>`, the last one in `engine.tick_slow.last`) and logged at most once per hour (`engine.tick_slow`,
  with the number of slow ticks since the previous line), and the durations of the last completed tick go to kv
  `engine.tick_last` inside the next tick's decision transaction (no write outside the existing ones). `fart status` prints
  them and `/eigenaar` has the card "Engine-tick (stappen en budget)". No work is skipped when a tick is slow: it becomes
  visible, never hidden.

**The rules did not change, provably.** `features.py` is a rule file, so the fingerprint of the rule-defining files changes:
from `sha256:e3bf4320c45c` (the frozen measurement week) to `sha256:d45c41793c64`. That is allowed only with proof and
registration: `tests/test_m11_engine_performance.py::test_meta_step_old_and_new_implementation_are_byte_equal` runs the
milestone-10 rule file (kept byte for byte as `tests/reference/features_m10.py`, its sha256 pinned) next to the new one on
a fixture database of 20,000 coins over 48 hours and 50,000 trades and proves the same metas (every term, every field, the
rounding, the order), the same coin features and the same verdicts of `decide`; the other rule files are byte-identical to
the frozen week (their partial fingerprint is pinned). The new fingerprint is registered in `config/measure.json` →
`community_gate.frozen_rules_equivalents` with the date, the PR and the name of that test; the week report pools its
rules_versions with the frozen fingerprint and says so under the segment title ("Bewezen gelijkwaardige berekening in dit
segment: ..."), and the website keeps calling the rules frozen and names the equivalence on `/methode` and `/eigenaar`. No
schema migration, no new dependency, no change of the website's content. On the side: the public headline no longer fails
on a day without coverage (it joins the parts it has instead of expecting three).

1. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   No migration (`user_version` stays 8). The installer restarts `fart-ingest`, `fart-engine` and the optional services; the
   engine restart is the one that matters. By hand, or if the installer ran with `START_SERVICES=0`:
   ```bash
   sudo systemctl restart fart-engine
   ```
2. **Check the tick in the engine log** (within a minute; every tick writes one line):
   ```bash
   grep engine.tick_steps /opt/fart/logs/engine.log | tail -n 3
   #   {"t": "...", "event": "engine.tick_steps", "as_of": ..., "tick_s": 1.8, "ok": true, "slow": false, "expire_s": 0.01, "features_s": 0.5,
   #    "promote_s": 0.0, "rpc_s": 0.3, "metas_s": 0.9, "decisions_s": 0.1, "coins": 200, "metas": 50, "recent_coins": 7100, "baseline_coins": 50000, ...}
   grep -c '"engine.tick_slow"' /opt/fart/logs/engine.log      # 0 after the restart
   ```
   `tick_s` must be seconds, well under 15, and `metas_s` under 5 s. A tick above 15 s carries `"slow": true`, is counted and
   appears at most once per hour as `engine.tick_slow` (with the number of slow ticks since the previous line). The engine's
   CPU share drops from 97% to a few percent: `top -b -n 1 -p "$(systemctl show -p MainPID --value fart-engine)"`.
3. **Check the step durations and the fingerprint in `fart status`:**
   ```bash
   sudo -u fart /opt/fart/bin/fart status
   #   freshness (s): ... engine.heartbeat=<under 30> ...
   #   engine tick: last 1.8 s (expire 0.01 s, features 0.5 s, promote 0.00 s, rpc 0.3 s, metas 0.9 s, decisions 0.1 s; 200 watched coins, 7100 in the themes window,
   #                50000 baseline coins; as_of 12 s ago); slow ticks (> 15 s): total=0 today=0
   #   rules fingerprints (engine): ..., <new commit>=sha256:d45c41793c64   <- the registered equivalent of the frozen sha256:e3bf4320c45c
   ```
   The `engine tick:` line appears after the second tick (the engine writes the previous tick's durations in every decision
   write); before that it says "no completed tick recorded yet". `/eigenaar` → "Engine-tick (stappen en budget)" shows the
   same figures per step with their share of the tick; "Gezondheid en versheid" shows the engine heartbeat "vers" again.
4. **Check the website:** the themes card on `https://kingdegen.dev/` says "berekend door de engine ... (N s geleden)" with N
   under 30 instead of "nu niet berekend":
   ```bash
   curl -s https://kingdegen.dev/api/public/themes | head -c 200; echo     # "state": "fresh"
   ```
   `/methode` says the running code has a proven equivalent fingerprint (PR #11); the pill "regels bevroren" stays on `/meting`.
5. **The week report:** the next `fart measure week` (06:30 UTC) pools the new rules_version with the frozen week. Under the
   segment title ``## Regels `sha256:e3bf4320c45c` (bevroren meetweek) · method_version `m1.0` `` it prints the line
   ```
   Bewezen gelijkwaardige berekening in dit segment: rules_version(s) `<new commit>` met vingerafdruk `sha256:d45c41793c64` tellen mee bij de bevroren regels. De regels zijn niet veranderd, alleen de rekentijd (PR #11 van 2026-10-29; bewijs: `tests/test_m11_engine_performance.py::test_meta_step_old_and_new_implementation_are_byte_equal`).
   ```
   and "## Versies" lists the equivalent fingerprint. One segment, never two. To see it at once:
   `sudo -u fart /opt/fart/bin/fart measure week --end $(date -u -d yesterday +%F) --days 2` and read `out/recall/week-<day>.md`.
6. **Rollback:** a code rollback to M10 works on the same database (no migration); M10 ignores the kv keys `engine.tick_last`
   and `engine.tick_slow*`. The tick is minutes again then, and the week report would open a second segment for a rules_version
   running M10 only if its fingerprint were unknown (it is the frozen one, so no).

## 5k. Milestone 12: terminal and research (M12)

M12 adds the Terminal (`/terminal`, a live table of every coin in view over one server-sent-events stream), the research
harness (`fart research`: the pre-registered hypotheses H4, H5, H1, H3, H7, H2, H6 back-tested on our own trades with the
costs a human pays; `fart research autopsy`; `fart research promote`), two bounded data steps in the track timer (creator
statistics; GeckoTerminal minute candles for the hour after each graduation) and the plays layer (`fart plays`,
`deploy/fart-plays.service`: proven rules from `config/plays.json` against the live trades, paper positions, `/plays`, Telegram
alerts to the owner at most 6 per hour). Schema v9 adds the tables `creator_stats`, `pool_candles`, `candle_jobs` and `plays`
(additive; the engine's tables are untouched). The frozen rule files are byte-identical (fingerprint `sha256:d45c41793c64`,
the registered equivalent of the frozen week). `config/plays.json` ships empty: nothing is proven, nothing fires, the plays
daemon idles. No new `.env` name. Design and the definition of "bewezen": `docs/DECISIONS.md`, "Milestone 12".

1. **Pull and install:**
   ```bash
   cd /opt/fart-src && git pull --ff-only && sudo deploy/install.sh
   ```
   The installer applies the v9 migration before the restarts (`user_version` 8 → 9; `fart status` prints it), installs and
   enables `fart-plays.service`, restarts `fart-ingest`, `fart-engine`, `fart-plays` and the optional services, and updates
   the Caddy site file at `/srv/fart/fart.caddy` when it differs (validate and reload, step 4). Check the schema:
   ```bash
   sudo -u fart /opt/fart/bin/fart --json status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["schema_version"])'   # 9
   ```
2. **The plays daemon idles with the empty configuration:**
   ```bash
   systemctl status fart-plays --no-pager
   journalctl -u fart-plays -n 20 --no-pager          # plays.started ... rules=0; then a plays.tick every 15 s with rules=0
   sudo -u fart /opt/fart/bin/fart plays --once        # one tick: {"rules": 0, "candidates": 0, "signals": 0, ...}
   sudo -u fart /opt/fart/bin/fart status | grep -E '^(plays|disk|candles|creator stats|research):'
   #   disk: 23.1 GB free of 240.0 GB (90% used); database 4.4 GB; trades 1,980,000 rows (~0.36 GB est.), 190,000 rows in the last 24 h -> ~1.03 GB at 30 days retention (...)
   #   candles (GeckoTerminal, 1-minute, once per graduation an hour after it): pending 0 (due now 0), done 0 with candles, ... never run
   #   creator stats: 0 creators (0 with a graduation or winner), updated today 0, backlog no; never run
   #   plays: 0 active rule(s) in config/plays.json (nothing proven yet: the layer idles); heartbeat 3 s ago; open 0, closed 0 (pnl +0.0000 SOL), unknown 0, no fill 0; alerts today 0
   #   research: no reports yet (fart research run --hypothesis H5 --days 3)
   ```
   The disk line warns under 20 GB free: the shared disk is at 91%, and the trades retention went from 14 to 30 days
   (`config/ingest.json`); the projection on that line says what 30 days cost in GB. If the warning shows, free space on the
   other projects or lower `trades_days` again (the harness then has a shorter window; it says so in its reports).
3. **The track timer's new steps** run after the outcomes every 5 minutes: creator statistics (bounded to 2,000 creators and
   60 s per run; the first runs backfill the whole database, `backlog yes` until done) and the candle queue (at most 20
   fetches per run, behind the `geckoterminal` gate shared with the tracker). After two runs:
   ```bash
   grep -E 'creator_stats.done|candles.done' /opt/fart/logs/measure.log | tail -n 4
   sudo -u fart /opt/fart/bin/fart status | grep -E '^(candles|creator stats):'
   sqlite3 /opt/fart/data/fart.db 'SELECT pool, MIN(minute), MAX(minute), COUNT(*) FROM pool_candles GROUP BY pool LIMIT 5;'
   ```
   A `pool_unknown` count in the candles line means the migrations carry no pool address for those graduations: the jobs are
   closed without candles, honestly; the research falls back to `mcap_samples` for the post-graduation path.
4. **Caddy: the stream must not be buffered or compressed.** The site file now compresses only the listed content types and
   proxies `/api/public/stream` with `flush_interval -1`. Validate and reload as in section 5g:
   ```bash
   caddy validate --config /srv/larp-scanner/https/Caddyfile --adapter caddyfile
   systemctl reload larp-scanner-https        # or: caddy reload --config ... (the admin API must be on)
   curl -s -N -m 3 https://kingdegen.dev/api/public/stream | head -c 600; echo
   #   retry: 3000
   #   id: 1234
   #   event: snapshot
   #   data: {"t": "...", "rows": [...], "n": 57, "engine_age_s": 4, "sol_usd": {...}, ...}
   ```
   The first event must arrive within a second; `curl -s -o /dev/null -w '%{http_code}\n' https://kingdegen.dev/terminal`
   is 200. If `caddy validate` refuses `match` inside `encode` or `flush_interval`, this Caddy is older than documented: drop
   the `match` block (then the stream is gzip-compressed; Caddy still flushes `text/event-stream` responses by default from
   2.4 on) and the `@stream` block, and note it in `docs/UNVERIFIED.md`.
5. **The Terminal in a browser:** open `https://kingdegen.dev/terminal`. The pill says "live", the count shows the coins in
   view (the watchlist plus the creates of the last 30 minutes), a new trade changes its row within a second, `j`/`k` move,
   `enter` opens pump.fun, `c` copies the mint, `f` follows, `1` to `4` open Axiom, Photon, BullX and GMGN (the patterns are
   not verified by hand: `docs/UNVERIFIED.md`, and a wrong one is a one-line change in `config/web.json`), the presets filter
   ("organisch" needs an explicit "niet gebundeld", so coins with few trades stay out), the Plays panel says that
   `config/plays.json` is empty. `/plays` says the same and shows the plays layer's heartbeat. The pages carry no owner
   vocabulary and no buy button; the test suite checks both.
6. **The first research report, on real data** (the sandbox has no copy of the database, so this is the first real run):
   ```bash
   sudo -u fart /opt/fart/bin/fart research run --hypothesis H5 --days 3 --limit 300     # a bounded smoke run; the report says so
   sudo -u fart /opt/fart/bin/fart research list
   sudo -u fart /opt/fart/bin/fart research run --hypothesis all --days 7                 # the pre-registered order H4, H5, H1, H3, H7, H2, H6; minutes to an hour
   sudo -u fart /opt/fart/bin/fart research autopsy --days 7
   ls -la /opt/fart/out/research/
   ```
   Read `out/research/<day>-<H>.md` in Dutch: the pre-registered hypothesis, the data and execution, the verdict ("Bewezen"
   only when the pre-chosen variant meets all four criteria on the test half: lower bound of the 95% interval of the expected
   value after costs above 0, at least 100 cases, unknown share under 20%, median lead time at least 2 minutes), every
   variant's figures, the per-group table, the conditioning on time of day / SOL trend / coins per hour, the sensitivity to the
   entry delay and the size, the unknown and no-fill counts, and the honesty section. A report that says "Niet bewezen" is a
   finished deliverable: the harness worked, the edge did not show. The runtime on the VPS is unknown; the run logs progress
   every 5,000 coins (`research.progress`). The 30-day window exists a month after the rollout (the retention was 14 days).
7. **Promotion, only with proof:**
   ```bash
   sudo -u fart /opt/fart/bin/fart research promote H2 m5_ts10     # refuses an unproven or unchosen variant, with the reason
   sudo systemctl restart fart-plays                               # the daemon reloads config/plays.json at start (and every minute)
   sudo -u fart /opt/fart/bin/fart status | grep '^plays:'          # plays: 1 active rule(s) in config/plays.json (H2-v1); ...
   ```
   A promoted rule fires live as it was measured (the same execution model), writes `plays` rows, shows on `/terminal` (the
   Plays panel and column) and `/plays`, and alerts the owner chat (kind `play`, at most 6 per hour; the message carries our
   Terminal link, never a venue). The community chat receives nothing from this layer until the go/no-go of the measurement
   week.
8. **Rollback:** a code rollback to M11 works on the same database (v9 is additive; M11 ignores the new tables and the kv keys
   `plays.*`, `candles.*`, `creator_stats.*`); disable the new unit first: `systemctl disable --now fart-plays`. The retention
   of 30 days is a config value; an M11 checkout carries 14 again and prunes back on its next weekly run (archived, as always).
   Restore the previous Caddy site file from `Caddyfile.before-fart-*` or re-copy the M11 `fart.caddy` and reload.

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
   not billed): `fart budget calibrate --day <day> --dashboard-cu <N>` prints the implied factor and the config
   line; adjust `config/budget.json` → `rpc.cu_per_byte` in a commit (docs/UNVERIFIED.md, "Solana RPC /
   Alchemy", step by step in section 5c).
5. Watch `coverage --hours 24` for a day: `grads_without_create` should be close to zero.
6. X: compare the prices in `config/x.json` with the developer console (docs/UNVERIFIED.md says how);
   `fart social dryrun` shows what the watch would cost per day before the token goes in (with the planner's full-load
   ceiling against the cap), `fart status` → `x spend today` and `x plan vs spend today` what it costs once it runs (in
   shadow mode, during the week), and `fart social calibrate --day D --console-usd N` which price fits the console's
   amount (milestone 7).
7. Telegram: `fart notify test`, then watch `fart notify outbox` for `sent` rows.
8. Week report (M5): `fart measure week --end <yesterday> --days 1` after 06:30 UTC; compare its universe row with
   the daily report of the same day; the bootstrap says `te weinig data` until the window has 3 covered days and
   20 winners.

## 7. Backup and restore

`fart-backup.timer` runs `deploy/backup.sh` daily: SQLite online backup API into
`/opt/fart/data/backup/fart-<stamp>.db.gz` after `PRAGMA quick_check`, keeping 14 days. Since milestone 6 the
script then runs `fart backup verify`: the newest archive is unpacked into `data/backup/.verify-<pid>/`,
`PRAGMA quick_check` runs on the copy, the row counts of `coins`, `migrations`, `decisions` and `outcomes` are
compared with the live database (live ≥ archive: those tables only grow), the copy is removed and the result
printed and kept in kv (`fart status` → `backup: last verify ...`). A failed check exits 1 and the unit ends
`failed`:

```bash
systemctl show -p Result,ExecMainStatus fart-backup.service   # Result=success ExecMainStatus=0, or Result=exit-code ExecMainStatus=1
journalctl -u fart-backup -n 12 --no-pager                     # "backup verify: OK ..." and "backup: verify ok", or "VERIFY FAILED"
sudo -u fart /opt/fart/bin/fart backup verify                  # by hand, any time; exit 0 ok, 1 failed, 2 no archive
```

A failed verify means the newest archive does not restore (bad gzip, `quick_check` not ok) or the live
database has fewer rows than the archive in a table that never shrinks (rows were lost, or the archive is not
ours): keep the previous archives, stop the daemons, look before restoring anything.

**Offsite copy (manual, by the deployer).** The archives live on the same disk as the database. Copy the newest
one to a second location after the daily run (05:30 UTC) from your own machine, never from the VPS outwards:

```bash
# on your machine: the newest archive and its size, then the copy
NEWEST=$(ssh root@<vps> 'ls -1 /opt/fart/data/backup/fart-*.db.gz | sort | tail -1')
ssh root@<vps> "ls -la $NEWEST"
scp "root@<vps>:$NEWEST" ~/fart-offsite/
gzip -t ~/fart-offsite/$(basename "$NEWEST") && echo "offsite copy ok"
```

Keep the last few; one copy per week is the minimum, one per day is cheap (an archive is a few tens of MB).

**Restore drill (do it once after the M6 upgrade, and after every schema migration).** Nothing here touches
the live database or the daemons; the archive is restored into an empty `FART_ROOT` and read with `fart status`:

```bash
# 1. an empty root the fart user can write to
sudo -u fart mkdir -p /tmp/fart-restore/data
# 2. the newest archive, unpacked as the database of that root
NEWEST=$(ls -1 /opt/fart/data/backup/fart-*.db.gz | sort | tail -1)
sudo -u fart bash -c "gunzip -c $NEWEST > /tmp/fart-restore/data/fart.db"
# 3. status against the restored copy: FART_ROOT points at the empty root, the code is the installed app
sudo -u fart env FART_ROOT=/tmp/fart-restore /opt/fart/bin/fart status
#    expected: schema_version 6, counts: coins=N, migrations=N, decisions=N, outcomes=N ... the same numbers (or slightly
#    lower: the archive is older) as `fart backup verify` printed for that archive; `freshness` empty (nothing runs there)
sudo -u fart env FART_ROOT=/tmp/fart-restore /opt/fart/bin/fart measure report --day $(date -u -d yesterday +%F)
#    optional: yesterday's report from the copy, written to /tmp/fart-restore/out/recall/ (sends nothing)
# 4. clean up
sudo rm -rf /tmp/fart-restore
```

Note that `/tmp/fart-restore` is a plain directory: the systemd units never see it (`PrivateTmp`), and the
drill works equally well in `/opt/fart/data/restore-drill` (inside `ReadWritePaths`) when `/tmp` is small.
The real restore (below) is the same `gunzip -c` into `/opt/fart/data/fart.db` with the daemons stopped.

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
| `fart-backup.service` `Result=exit-code` with `VERIFY FAILED` in the journal (M6) | the newest archive does not restore, or the live database has fewer rows than the archive in a table that only grows | `fart backup verify` by hand shows which; keep the older archives; a bad archive after a good backup line points at the disk, lost live rows at something that deleted (`decisions`/`outcomes` cannot be deleted: triggers) |
| `x mode note: X_MODE=live refused` in `status` (M6) | `X_MODE=live` without `"x_live": true` in `config/engine.json` | expected during the measurement week: the watch runs in shadow; after the week the key is a commit (new fingerprint) |
| `engine.tick_slow` in the engine log, `slow ticks (> 15 s): total=N` in `status` (M11) | a tick took longer than the loop interval: look at the step durations in the same line (`metas_s`: the themes window grew; `features_s`: the watchlist and its trades; `rpc_s`: the network; `decisions_s`: a busy database) | nothing is skipped; if `tick_s` stays above 15 s, the step named tells where to look (`engine.tick_steps` every tick, `/eigenaar` → Engine-tick) |
| `x shadow: ... daemons report social=shadow engine=off` (M6) | the two daemons read different `.env` states (one was not restarted) | `sudo systemctl restart fart-social fart-engine`; both must report the same mode |
| `x planner: ... deferred today: ca_counts N` growing through the peak hours (M7) | the envelope paces more than the morning: the day's plan is bigger than the cap allows (many keys, or a cap under 10.00) | `fart social dryrun`: compare `total` and `full load` with the cap; raise `X_DAILY_BUDGET_USD` within 10.00 to 20.00, or lower `planner.max_active_keys` in a commit; a deferred count is done later, never lost until the key's window ends |
| a visitor gets 429 on `https://kingdegen.dev` (M9) | more than 120 requests per minute from one address on the public routes | expected for a scraper; a page refreshes itself twice a minute at most; the owner's session is never limited; `journalctl -u fart-web | grep rate_limited` names the address (once a minute) |
| the public pages answer `cache-control: no-store` (M9) | the request carried a session cookie (the owner's browser) | expected: only anonymous answers are shared-cacheable; `curl` without a cookie shows `public, max-age=15` or `30` |
| the dashboard says `De X-laag staat uit` (M9) | no `X_BEARER_TOKEN` in `.env` | expected until the key goes in (section 5e); the card fills itself in shadow mode and says so |
| the themes card says no word comes up (M9) | fewer than 3 coins of the last 3 hours share a word that is not a stopword | expected on a quiet hour; the card uses the engine's own clustering and thresholds |
| the themes card says `Thema’s worden nu niet berekend` with an age (M10) | the engine has not written `engine.metas_latest` for more than 5 minutes: it is down, or it was not restarted since M10 | `systemctl status fart-engine`, `journalctl -u fart-engine -n 20`; after a restart the card fills within 15 s (the dashboard refreshes itself) |
| a card says `wordt berekend` for longer than a minute (M10) | the background thread of `fart-web` has not finished the view's first computation, or the view fails every time | `journalctl -u fart-web | grep -E 'view_slow|view_failed'`; `fart status` → `web views:` (a `FAILED` view is named); `/eigenaar` → Weergaven shows the last error |
| `fart status` → `web views: no timings yet` (M10) | `fart-web` has not completed a cycle since M10 (not running, or restarted a second ago) | `systemctl status fart-web`; the line appears after the first cycle (seconds); `logs/web-views.json` is its source |
| `fart-web` inactive right after `install.sh` (M8) | both `.env` names are not set yet: the installer never starts the website without them | steps 2 and 3 of section 5g; then `systemctl enable --now fart-web` |
| `journalctl -u fart-web` says `fart web: FART_WEB_PASSWORD_HASH is not a hash made by ...` or `... is too short` (M8) | a value in `.env` was pasted incompletely or edited | re-run `fart web hash-password` / `fart web new-secret`, paste the whole line; `systemctl restart fart-web` |
| `https://kingdegen.dev` gives 502 (Caddy) while `curl -sI http://127.0.0.1:8792/login` works (M8) | Caddy cannot reach the loopback port: a different `FART_WEB_BIND`, or the unit is down | `systemctl status fart-web`; `FART_WEB_BIND` must be `127.0.0.1:8792` (the Caddy site file names that port) |
| `web.request ... status=500` in `logs/web.log` with `web.request_failed` and `unable to open database file` (M8) | the read-only open could not use the WAL files (the daemons were all stopped, so `-wal`/`-shm` were removed and `ReadOnlyPaths` forbids creating them) and the `immutable` fallback failed too | start `fart-ingest` (it recreates the WAL files); the next page load works; a page error is never a data error |
| the login says `Te veel mislukte pogingen. Wacht N minuten` for Jim himself (M8) | five wrong passwords within 10 minutes from his IP, or from anyone for the single account (the lock is per IP and per account) | wait the minutes shown; `journalctl -u fart-web | grep login_failed` shows the IPs; a restart of `fart-web` does not reset the lock (it is replayed from `web.log`) |
| Caddy fails to start after `systemctl restart larp-scanner-https` with `permission denied` on the fart import (M8) | the Caddyfile imports `/opt/fart/app/deploy/caddy/fart.caddy` directly (unreadable for the DynamicUser) instead of `/srv/fart/fart.caddy` | step 5 and 6 of section 5g; validate as `nobody`; `caddy reload` would have hidden this |
| `budget x ... blocked N` growing with `daily_cost_cap` or `account_watch_reserve` rows in `api_ledger` (M7) | a hard rule refused calls: the cap or the 70% confirmation share is spent | not expected at 15.00 (the full load is 66% of it); check `x spend today` per kind and `select stop_reason, count(*) from x_keys ...`; a run of `exhausted` keys means the stop rules do not bite (docs/UNVERIFIED.md, "Milestone 7") |
| `x planner: active keys 20` for hours and `stopped today: displaced N` (M7) | more observations than slots; the youngest keep displacing the oldest | expected only on a very busy day; `planner.max_active_keys` is a commit; the displaced keys' counts stay in `shadow_confirmations` |
| `fart social calibrate` says `verdict: adjust` (M7) | the console's amount differs from our ledger by more than 5% | read the console's per-endpoint breakdown, change `config/x.json` → `prices` in a commit (not a rule file), write the figures into docs/UNVERIFIED.md; a unit mismatch (per request instead of per post) is a code change |
| `ws_subscriptions.notifications` grows but `trades` with `source='rpc_trades'` stays 0 | TradeEvents do not arrive on the bonding-curve address | set `config/ingest.json` → `rpc_trades.mention` to `"mint"` and restart |
| `decode.pump_amm_pool_mismatch` counting up | the pump-amm BuyEvent/SellEvent layout differs from `fart/chain/events.py` | inspect `raw_undecoded` / `fart events classify`; fix the layout (docs/PUMPFUN-EVENTS.md) |
| `global.check_mismatch` at ingest start / `fart chain global` exit 1 | config/pumpfun.json differs from the chain | update the config (one commit = new `rules_version`); a mismatch never stops the daemons |
| `api.budget_cap_reached source=rpc` | daily CU cap hit | raise `RPC_DAILY_BUDGET_CU` or lower sweep/enrich limits in `config/ingest.json` |
| `api.budget_cap_reached source=x` | daily X USD cap hit | expected: the watch pauses until the next UTC day; raise `X_DAILY_BUDGET_USD` if wanted |
| open gap for `rpc_logs` with `budget_cap` | RPC stream paused until the next UTC day | expected degradation; PumpPortal continues |
| open gap for `rpc_sigs` with `sweep_error` | the signature sweep fails (RPC down or budget) | check the `rpc` budget row and `sweep.rpc_unavailable` in the log |
| `grads_without_create` high | creates missed by both streams | check `sweep.done missed=` and `enrich.done`; RPC key present? |
| `enrich.done ... errors=N` every run, `enrich_queue` rows with `last_error` `rpc error -32015: Transaction version (1) is not supported by the requesting client ...`, `fetch_error` on `fart events classify --fetch` | the node served a transaction of a higher version than the client's `maxSupportedTransactionVersion` (mainnet served version 1 on 31 Oct 2026; until then the client asked for 0) | upgrade: the client asks for 1 (`fart/chain/rpc.py` → `MAX_SUPPORTED_TRANSACTION_VERSION`); rows that reached 3 attempts are retried after `update enrich_queue set attempts=0 where done=0 and attempts>=3 and last_error like 'rpc error -32015:%'` (docs/UNVERIFIED.md, "Solana RPC / Alchemy"); the same message after the upgrade names the version to bump to, in a commit |
| `decode.truncated_or_drift` counting up | pump.fun changed an event layout | inspect `raw_undecoded`, update `fart/chain/events.py` LAYOUTS |
| `outbox` rows stuck in `pending` with `error` | Telegram unreachable or wrong token/chat id | `fart notify outbox`; fix `.env`; rows retry 15 s → 1 h then `gave_up` |
| `outbox` rows `gave_up` with `chat not allowed` | a row was queued for a chat that is not the owner's while the community channel is off (or was switched off) | expected and safe: nothing left the server; `fart status` → `telegram community` shows the state; set both community names on purpose or leave it |
| `tracker: skipped mints total=N` growing in `status` | more than 600 subjects in a tracker run (`max_mints_per_run`) or DexScreener refused batches | the reports count the affected graduations as `uitkomst onbekend`; raise `max_mints_per_run` in `config/measure.json` (a commit) or accept thinner coverage |
| `counts: ... meta_burst` above 40 for more than one engine tick | the trim did not run (engine down) or the frozen sets hold more than 40 | `journalctl -u fart-engine`; `meta_burst_trimmed` in the tick log; the frozen sets are bounded by `meta_burst_max_total` as well |
| week report says `te weinig data` everywhere | fewer than 3 covered days or fewer than 20 winners in the window | expected early in the week; the Monday report of a full week has the data or says why not |
| `database is locked` | another connection held the write lock for more than 15 s (busy timeout), or a writer used a plain `BEGIN` after reading | every writer uses `BEGIN IMMEDIATE` with 3 retries (`fart/db.py`); a `sqlite3` shell left in a transaction is the usual culprit |
