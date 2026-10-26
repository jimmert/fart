# Deploying fart on the VPS (Ubuntu 24.04)

Target layout (shared host, unprivileged user, outbound HTTPS/WSS only, no inbound ports):

```
/opt/fart/            root:fart 0750
  app/                the git checkout copy (read-only for the service)
  venv/               python3.12 venv with websockets
  data/               fart.db (SQLite WAL), raw/<source>/YYYYMMDD/HH.jsonl.gz, archive/, backup/
  out/recall/         daily reports <day>.json + <day>.md
  logs/               ingest.log, engine.log, measure-*.log (JSON lines, 5 MB x 5)
  .env                root:fart 0640 - the ONLY place where keys live
```

systemd units (all in `deploy/`, installed to `/etc/systemd/system/`):

| unit | what | schedule |
|---|---|---|
| `fart.slice` | MemoryMax=1G, CPUQuota=100% for everything below | - |
| `fart-ingest.service` | PumpPortal + RPC log streams, signature sweep, coverage, retention | daemon, Restart=always |
| `fart-engine.service` | candidate engine v0, observations only | daemon, 15 s loop |
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
`/opt/fart/app/VERSION` (the git commit = `rules_version` of every decision), builds the venv,
installs the units, and restarts the daemons. Set `START_SERVICES=0` to install without starting.

## 3. Configure `/opt/fart/.env`

The installer copies `.env.example` to `/opt/fart/.env` when it does not exist. Edit it as root:

```
RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/<key>
RPC_WSS_URL=wss://solana-mainnet.g.alchemy.com/v2/<key>
RPC_DAILY_BUDGET_CU=3000000
PUMPPORTAL_WSS_URL=wss://pumpportal.fun/api/data
FART_ROOT=/opt/fart
```

Leave `RPC_HTTP_URL`/`RPC_WSS_URL` empty to run on the public RPC (`api.mainnet-beta.solana.com`,
strict rate limits, no key). The file is read by the processes directly (`root:fart 0640`); it is
never logged and never copied anywhere. After editing: `sudo systemctl restart fart-ingest fart-engine`.

`RPC_DAILY_BUDGET_CU` is a hard daily cap on *estimated* compute units (cost table in
`config/budget.json`). Reconcile the estimate with the Alchemy dashboard after the first days and
adjust the table or the cap (see `docs/UNVERIFIED.md`). At the cap the RPC side stops (log stream
disconnects, enrichment pauses); PumpPortal keeps running; a warning is logged once per hour.

## 4. Verify

```bash
systemctl status fart-ingest fart-engine --no-pager
systemctl list-timers 'fart-*' --no-pager
journalctl -u fart-ingest -n 50 --no-pager
sudo -u fart env FART_ROOT=/opt/fart /opt/fart/venv/bin/python -m fart status
sudo -u fart env FART_ROOT=/opt/fart /opt/fart/venv/bin/python -m fart coverage --hours 3
tail -f /opt/fart/logs/ingest.log            # JSON lines: event, source, counts
ls /opt/fart/data/raw/pumpportal/$(date -u +%Y%m%d)/
```

Healthy after 10 minutes: `status` shows `ingest.pumpportal.last_msg_at` a few seconds old, coins
counting up, no open gaps, budget rows with `errors 0`; `coverage` shows `pp` and `rpc` counts of the
same order of magnitude for the current hour (the hour is only `complete` once it is over).

The 60-second live smoke test (one extra PumpPortal connection, no key) from the checkout:

```bash
cd /opt/fart-src && WITH_DEV=1 sudo deploy/install.sh   # installs pytest into the venv once
sudo -u fart env FART_ROOT=/tmp/fart-smoke FART_LIVE=1 /opt/fart/venv/bin/python -m pytest -q tests/test_live_smoke.py -s
```

Run it while `fart-ingest` is stopped if you want to be strict about PumpPortal's one-connection
rule for the shared VPS IP (the test itself only holds a connection for 60 s).

## 5. First-day checklist (docs/UNVERIFIED.md)

1. Compare one real create transaction (`getTransaction`, jsonParsed) with `docs/PUMPFUN-EVENTS.md`
   and look at `raw_undecoded` / the `decode.*` counters in `fart status`. Zero or near-zero
   `decode.truncated_or_drift` means the layouts hold.
2. Check the pump.fun Global account parameters against `config/pumpfun.json`.
3. Compare `fart status` RPC `cost` with the Alchemy dashboard; adjust `config/budget.json`.
4. Watch `coverage --hours 24` for a day: `grads_without_create` should be close to zero.

## 6. Backup and restore

`fart-backup.timer` runs `deploy/backup.sh` daily: SQLite online backup API into
`/opt/fart/data/backup/fart-<stamp>.db.gz` after `PRAGMA quick_check`, keeping 14 days.

Restore (stops the daemons; raw gz files and out/ are untouched):

```bash
sudo systemctl stop fart-ingest fart-engine fart-measure-track.timer
sudo -u fart bash -c 'cd /opt/fart/data && mv fart.db fart.db.broken && gunzip -c backup/fart-<stamp>.db.gz > fart.db'
sudo systemctl start fart-ingest fart-engine fart-measure-track.timer
```

## 7. Rollback

Code rollback is a checkout of the previous commit/tag followed by `install.sh`:

```bash
cd /opt/fart-src && git checkout <previous-commit> && sudo deploy/install.sh
```

Data is forward-compatible within a schema version (`PRAGMA user_version`, see `fart/db.py`).
Rolling back across a schema migration requires restoring the backup taken before the upgrade.
Decisions and outcomes carry `rules_version`/`method_version`, so a rollback never rewrites history:
old rows stay, new rows get the old version string again.

## 8. Troubleshooting

| symptom (`fart status` / logs) | meaning | action |
|---|---|---|
| `pp.connect_gated reason=rate_limit` | more than 12 PumpPortal connects in an hour | wait; check why sessions drop (`pp.disconnected`) |
| `pp.disconnected ... 403/429` repeatedly | PumpPortal refusing the shared IP | stop other PumpPortal clients on the host; one connection only |
| `api.budget_cap_reached source=rpc` | daily CU cap hit | raise `RPC_DAILY_BUDGET_CU` or lower sweep/enrich limits in `config/ingest.json` |
| open gap for `rpc_logs` with `budget_cap` | RPC stream paused until the next UTC day | expected degradation; PumpPortal continues |
| `grads_without_create` high | creates missed by both streams | check `sweep.done missed=` and `enrich.done`; RPC key present? |
| `decode.truncated_or_drift` counting up | pump.fun changed an event layout | inspect `raw_undecoded`, update `fart/chain/events.py` LAYOUTS |
| database is locked | another process holds a long write | all writers use 5 s busy timeout; check for a stuck `sqlite3` shell |
