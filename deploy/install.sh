#!/usr/bin/env bash
# Idempotent installer for Ubuntu 24.04. Run as root from a checkout:  sudo deploy/install.sh
# - system user `fart` (no login), /opt/fart/{app,bin,data,out,logs}
# - copies the checkout to /opt/fart/app (never touches data/out/logs/.env)
# - python venv at /opt/fart/venv with websockets
# - /opt/fart/bin/fart: executable wrapper (cd into the app, FART_ROOT set, exec the venv python -m fart)
# - applies the database schema migration (PRAGMA user_version) before the daemons restart
# - systemd units fart-* installed, enabled and (re)started (fart-social / fart-notify when present)
# - milestone 8: fart-web.service (the website on 127.0.0.1:8792) is installed always but enabled and started only when
#   /opt/fart/.env carries both FART_WEB_PASSWORD_HASH and FART_WEB_SECRET; the Caddy site file is copied to
#   /srv/fart/fart.caddy when that directory exists (the deployer created it at the milestone-8 rollout), never reloaded
# Never overwrites an existing /opt/fart/.env. Prints the verification commands at the end.
set -euo pipefail

FART_ROOT="${FART_ROOT:-/opt/fart}"
FART_USER="${FART_USER:-fart}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$FART_ROOT/app"
VENV="$FART_ROOT/venv"
START_SERVICES="${START_SERVICES:-1}"

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root (sudo)" >&2
  exit 1
fi

log() { echo "install: $*"; }

# --- user and directories -----------------------------------------------------------------
if ! id -u "$FART_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$FART_ROOT" --shell /usr/sbin/nologin --user-group "$FART_USER"
  log "created system user $FART_USER"
fi
mkdir -p "$FART_ROOT" "$APP" "$FART_ROOT/bin" "$FART_ROOT/data" "$FART_ROOT/out" "$FART_ROOT/logs"
chown root:"$FART_USER" "$FART_ROOT"
chmod 0750 "$FART_ROOT"
chown -R "$FART_USER":"$FART_USER" "$FART_ROOT/data" "$FART_ROOT/out" "$FART_ROOT/logs"
chmod 0750 "$FART_ROOT/data" "$FART_ROOT/out" "$FART_ROOT/logs"

# --- code -----------------------------------------------------------------------------------
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude .git --exclude data --exclude out --exclude logs --exclude .env \
    --exclude '__pycache__' --exclude '.pytest_cache' "$SRC"/ "$APP"/
else
  find "$APP" -mindepth 1 -maxdepth 1 ! -name data ! -name out ! -name logs -exec rm -rf {} +
  (cd "$SRC" && tar --exclude=.git --exclude=data --exclude=out --exclude=logs --exclude=.env \
    --exclude='__pycache__' -cf - .) | (cd "$APP" && tar -xf -)
fi
VERSION="$(git -C "$SRC" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
echo "$VERSION" > "$APP/VERSION"
chown -R root:"$FART_USER" "$APP"
find "$APP" -type d -exec chmod 0750 {} +
find "$APP" -type f -exec chmod 0640 {} +
chmod 0750 "$APP/deploy/install.sh" "$APP/deploy/backup.sh"
log "code installed at $APP (rules_version $VERSION)"

# --- python venv -------------------------------------------------------------------------------
PYBIN="$(command -v python3.12 || command -v python3)"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYBIN" -m venv "$VENV"
  log "created venv at $VENV with $("$VENV/bin/python" --version)"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet "websockets>=12,<18"
if [ "${WITH_DEV:-0}" = "1" ]; then
  "$VENV/bin/python" -m pip install --quiet "pytest>=8"
fi
chown -R root:"$FART_USER" "$VENV"
chmod -R o-rwx "$VENV"
log "venv ready: $("$VENV/bin/python" -c 'import websockets,sys; print("python", sys.version.split()[0], "websockets", websockets.__version__)')"

# --- /opt/fart/bin/fart wrapper --------------------------------------------------------------
# `python -m fart` only works from the app directory; the wrapper makes `sudo -u fart /opt/fart/bin/fart status`
# work from anywhere and pins FART_ROOT.
cat > "$FART_ROOT/bin/fart" <<WRAP
#!/usr/bin/env bash
export FART_ROOT="\${FART_ROOT:-$FART_ROOT}"
cd "$APP" && exec "$VENV/bin/python" -m fart "\$@"
WRAP
chown root:"$FART_USER" "$FART_ROOT/bin" "$FART_ROOT/bin/fart"
chmod 0750 "$FART_ROOT/bin" "$FART_ROOT/bin/fart"
log "wrapper installed: $FART_ROOT/bin/fart"

# --- .env (never overwritten) ------------------------------------------------------------------
if [ ! -f "$FART_ROOT/.env" ]; then
  cp "$APP/.env.example" "$FART_ROOT/.env"
  log "created $FART_ROOT/.env from .env.example -> fill in RPC_HTTP_URL / RPC_WSS_URL (or leave empty for the public RPC)"
fi
chown root:"$FART_USER" "$FART_ROOT/.env"
chmod 0640 "$FART_ROOT/.env"

# --- database schema (idempotent; a no-op when already current) --------------------------------
# Opening the database applies pending migrations (PRAGMA user_version). Doing it here, before the
# daemons restart, means every service starts on the new schema. Safe while the old daemons run (WAL).
# Milestone 3 (user_version 3) also removes the rows the USDC/PUMP "migrations" caused and prints
# the counts (kv schema.v3.cleanup); milestone 4 (user_version 4) closes the milestone-2 graduation
# watchlist rows and flags migrations without a create (kv schema.v4.cleanup); milestone 5 (user_version 5)
# trims the meta_burst watchlist rows above the engine's bound of 40 (kv schema.v5.cleanup); milestone 6
# (user_version 6) only creates the tables shadow_decisions, shadow_confirmations and engine_candidates;
# milestone 7 (user_version 7) adds the planner table x_keys and the column x_spend.unit; milestone 8 adds no migration
# (the website reads only); a second run changes nothing.
if [ -f "$FART_ROOT/data/fart.db" ]; then
  if STATUS_JSON="$(sudo -u "$FART_USER" "$FART_ROOT/bin/fart" --quiet --json status 2>/dev/null)"; then
    log "database schema: $(printf '%s' "$STATUS_JSON" | "$VENV/bin/python" -c 'import json,sys
d = json.load(sys.stdin)
c = d.get("schema_v3_cleanup") or {}
c4 = d.get("schema_v4_cleanup") or {}
c5 = d.get("schema_v5_cleanup") or {}
print("user_version", d.get("schema_version"), "| v3 cleanup of system-mint rows:", json.dumps(c.get("deleted")) if c else "not run",
      "| v4 cleanup (graduation rows closed, migrations flagged):", json.dumps(c4.get("counts")) if c4 else "not run",
      "| v5 cleanup (meta_burst rows trimmed):", json.dumps(c5.get("counts")) if c5 else "not run")')"
  else
    log "WARNING: schema check failed (fart status); the daemons will retry the migration on start"
  fi
fi

# --- systemd units -----------------------------------------------------------------------------
for unit in "$APP"/deploy/fart.slice "$APP"/deploy/fart-*.service "$APP"/deploy/fart-*.timer; do
  install -m 0644 -o root -g root "$unit" /etc/systemd/system/
done
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify /etc/systemd/system/fart-*.service /etc/systemd/system/fart-*.timer 2>&1 | grep -v -e 'is not loaded properly' || true
fi
systemctl enable fart-ingest.service fart-engine.service fart-measure-track.timer fart-measure-report.timer fart-measure-week.timer fart-backup.timer >/dev/null
OPTIONAL_SERVICES=""
for u in fart-social.service fart-notify.service; do
  if [ -f "/etc/systemd/system/$u" ]; then
    systemctl enable "$u" >/dev/null
    OPTIONAL_SERVICES="$OPTIONAL_SERVICES $u"
  fi
done
# --- milestone 8: the website runs only when both .env values exist (fart web refuses to start without them) -----
env_value() { grep -E "^[[:space:]]*$1=" "$FART_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]"'"'"; }
WEB_READY=0
if [ -n "$(env_value FART_WEB_PASSWORD_HASH)" ] && [ -n "$(env_value FART_WEB_SECRET)" ]; then
  WEB_READY=1
  systemctl enable fart-web.service >/dev/null
  OPTIONAL_SERVICES="$OPTIONAL_SERVICES fart-web.service"
else
  log "fart-web.service installed but NOT started: set FART_WEB_PASSWORD_HASH (fart web hash-password) and FART_WEB_SECRET (fart web new-secret) in $FART_ROOT/.env, then: systemctl enable --now fart-web"
fi
if [ -d /srv/fart ]; then
  if ! cmp -s "$APP/deploy/caddy/fart.caddy" /srv/fart/fart.caddy 2>/dev/null; then
    install -m 0644 -o root -g root "$APP/deploy/caddy/fart.caddy" /srv/fart/fart.caddy
    log "Caddy site file updated at /srv/fart/fart.caddy: validate and reload Caddy (deploy/DEPLOY.md, 'Milestone 8: website')"
  fi
fi
if [ "$START_SERVICES" = "1" ]; then
  systemctl restart fart-ingest.service
  systemctl restart fart-engine.service
  for u in $OPTIONAL_SERVICES; do systemctl restart "$u"; done
  systemctl start fart-measure-track.timer fart-measure-report.timer fart-measure-week.timer fart-backup.timer
  log "services (re)started:$OPTIONAL_SERVICES fart-ingest fart-engine + timers"
fi

# --- verification -------------------------------------------------------------------------------
sleep 2
systemctl --no-pager --plain list-units 'fart-*' || true
echo
log "verify with:"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart status"
echo "  journalctl -u fart-ingest -n 50 --no-pager"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart coverage --hours 3"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart chain global        # Global account vs config/pumpfun.json"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart events classify     # undecoded event discriminators"
echo "  systemctl start fart-backup.service && systemctl show -p Result,ExecMainStatus fart-backup.service   # backup + verify (M3, M6)"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart backup verify                                 # restore check of the newest archive (M6)"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart social dryrun                                 # X plan and USD per day, the full-load formula against the cap, no HTTP (M6, M7)"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart social calibrate --day <day> --console-usd <N>   # our X ledger against the console's amount, writes nothing (M7)"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart engine explain --day $(date -u +%F)             # why (no) observations today (M6)"
echo "  sudo -u $FART_USER $FART_ROOT/bin/fart measure week --end $(date -u -d yesterday +%F 2>/dev/null || date -u +%F) --days 1   # week report smoke test (M5), sends nothing"
echo "  systemctl list-timers 'fart-*' --no-pager                          # fart-measure-week.timer daily 06:30 UTC"
if [ "$WEB_READY" = "1" ]; then
  echo "  curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8792/login   # 200: fart-web answers on the loopback (M8); the site itself: https://kingdegen.dev"
  echo "  journalctl -u fart-web -n 20 --no-pager                              # web.started ... readonly=true"
else
  echo "  sudo -u $FART_USER $FART_ROOT/bin/fart web hash-password                # M8: the owner's password hash for .env (asked invisibly, hash only)"
  echo "  sudo -u $FART_USER $FART_ROOT/bin/fart web new-secret                   # M8: the cookie secret for .env; then: systemctl enable --now fart-web"
fi
echo "  tail -f $FART_ROOT/logs/ingest.log"
