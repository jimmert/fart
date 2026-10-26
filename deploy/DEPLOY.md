# Deploying fart on the VPS (Ubuntu 24.04)

Target layout (shared host, unprivileged user, outbound HTTPS/WSS only, no inbound ports):

```
/opt/fart/            root:fart 0750
  app/                the git checkout copy (read-only for the service)
  bin/fart            wrapper: cd /opt/fart/app && exec /opt/fart/venv/bin/python -m fart "$@" (FART_ROOT set)
  venv/               python3.12 venv with websockets
  data/               fart.db (SQLite WAL), raw/<source>/YYYYMMDD/HH.jsonl.gz, archive/, backup/
  out/recall/         daily reports <day>.json + <day>.md
  logs/               ingest.log, engine.log, social.log, notify.log, measure-*.log (JSON lines, 5 MB x 5)
  .env                root:fart 0640 - the ONLY place where keys live
```

Every command below uses the wrapper: `sudo -u fart /opt/fart/bin/fart <command>`. (`python -m fart`
only works from `/opt/fart/app`; the wrapper does the `cd` and sets `FART_ROOT=/opt/fart`.)

systemd units (all in `deploy/`, installed to `/etc/systemd/system/`):

| unit | what | schedule |
|---|---|---|
| `fart.slice` | MemoryMax=1G, CPUQuota=100% for everything below | - |
| `fart-ingest.service` | PumpPortal (creates + migrations) + RPC streams (creates, migrations, per-mint trades), signature sweep, coverage, retention, Global check | daemon, Restart=always |
| `fart-engine.service` | candidate engine v0, observations only | daemon, 15 s loop |
| `fart-social.service` | X mega-account / KOL watch, CA/ticker confirmation counts (milestone 2; idle without `X_BEARER_TOKEN`) | daemon, 15 s loop |
| `fart-notify.service` | Telegram outbox to the owner chat (milestone 2; idle unless `TELEGRAM_NOTIFY_ENABLED=1`) | daemon, 20 s loop |
| `fart-measure-track.timer` | price samples + due outcomes + paper trades | every 5 min |
| `fart-measure-report.timer` | daily recall report for the previous UTC day | 06:00 UTC |
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
FART_ROOT=/opt/fart
```

Leave `RPC_HTTP_URL`/`RPC_WSS_URL` empty to run on the public RPC (`api.mainnet-beta.solana.com`,
strict rate limits, no key, **no per-mint trade stream**). The file is read by the processes directly
(`root:fart 0640`); it is never logged and never copied anywhere. `fart status` prints only
`configured` / `not configured` per key. After editing:
`sudo systemctl restart fart-ingest fart-engine fart-social fart-notify`.

`RPC_DAILY_BUDGET_CU` is a hard daily cap on *estimated* compute units (cost table in
`config/budget.json`). Reconcile the estimate with the Alchemy dashboard after the first days and
adjust the table or the cap (see `docs/UNVERIFIED.md`). At 80% of the cap the trade stream drops the
probation subscriptions (keeps observe / early_buyers / meta_burst); at the cap the RPC side stops
(log streams disconnect, enrichment pauses); PumpPortal keeps running; a warning is logged once per hour.

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
empty, budget rows with `errors 0`; `coverage` shows `pp` and `rpc` counts of the same order of
magnitude for the current hour (the hour is only `complete` once it is over; a `recov(s)` column shows
gap seconds the sweep recovered).

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
   graduated), `top subscriptions` lists the busiest mints, `rpc CU today by method` shows what the
   trade stream costs - compare with the Alchemy dashboard and tune `config/budget.json`.
5. Telegram: with the token, chat id and `TELEGRAM_NOTIFY_ENABLED=1` in `.env`,
   `sudo -u fart /opt/fart/bin/fart notify test` sends one test message to the owner chat.

Rollback to milestone 1 code after the migration is possible (milestone-1 code ignores the new
tables and columns), but restoring the pre-upgrade backup is the clean way back.

## 6. First-day checklist (docs/UNVERIFIED.md)

1. `fart chain global`: reserves, supply and `fee_basis_points` 95 must match; note the
   `creator_fee_basis_points` the account carries and compare with `config/pumpfun.json`
   (`creator_fee_bps` 30, `total_curve_fee_bps` 125).
2. `fart events classify`: every remaining unknown discriminator should carry an emitting program;
   unknown ones emitted by the pump.fun or pump-amm program are real gaps (`fart/chain/events.py`).
   `--fetch 5` attributes rows written before milestone 2 (five budget-gated `getTransaction` calls).
3. Compare one real create transaction and one pump-amm trade (`trades` with `source='pump_amm'`) with
   `docs/PUMPFUN-EVENTS.md`; `decode.pump_amm_pool_mismatch` in `fart status` must stay 0.
4. Compare `fart status` → `rpc CU today by method` with the Alchemy dashboard; adjust `config/budget.json`.
5. Watch `coverage --hours 24` for a day: `grads_without_create` should be close to zero.
6. X: compare the prices in `config/x.json` with the developer console; `fart status` → `x spend today`.
7. Telegram: `fart notify test`, then watch `fart notify outbox` for `sent` rows.

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
| database is locked | another process holds a long write | all writers use 5 s busy timeout; check for a stuck `sqlite3` shell |
