#!/usr/bin/env bash
# Idempotent installer for Ubuntu 24.04. Run as root from a checkout:  sudo deploy/install.sh
# - system user `fart` (no login), /opt/fart/{app,data,out,logs}
# - copies the checkout to /opt/fart/app (never touches data/out/logs/.env)
# - python venv at /opt/fart/venv with websockets
# - systemd units fart-* installed, enabled and (re)started
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
mkdir -p "$FART_ROOT" "$APP" "$FART_ROOT/data" "$FART_ROOT/out" "$FART_ROOT/logs"
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

# --- .env (never overwritten) ------------------------------------------------------------------
if [ ! -f "$FART_ROOT/.env" ]; then
  cp "$APP/.env.example" "$FART_ROOT/.env"
  log "created $FART_ROOT/.env from .env.example -> fill in RPC_HTTP_URL / RPC_WSS_URL (or leave empty for the public RPC)"
fi
chown root:"$FART_USER" "$FART_ROOT/.env"
chmod 0640 "$FART_ROOT/.env"

# --- systemd units -----------------------------------------------------------------------------
for unit in "$APP"/deploy/fart.slice "$APP"/deploy/fart-*.service "$APP"/deploy/fart-*.timer; do
  install -m 0644 -o root -g root "$unit" /etc/systemd/system/
done
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify /etc/systemd/system/fart-*.service /etc/systemd/system/fart-*.timer 2>&1 | grep -v -e 'is not loaded properly' || true
fi
systemctl enable fart-ingest.service fart-engine.service fart-measure-track.timer fart-measure-report.timer fart-backup.timer >/dev/null
if [ "$START_SERVICES" = "1" ]; then
  systemctl restart fart-ingest.service
  systemctl restart fart-engine.service
  systemctl start fart-measure-track.timer fart-measure-report.timer fart-backup.timer
  log "services (re)started"
fi

# --- verification -------------------------------------------------------------------------------
sleep 2
systemctl --no-pager --plain list-units 'fart-*' || true
echo
log "verify with:"
echo "  sudo -u $FART_USER env FART_ROOT=$FART_ROOT $VENV/bin/python -m fart status"
echo "  journalctl -u fart-ingest -n 50 --no-pager"
echo "  sudo -u $FART_USER env FART_ROOT=$FART_ROOT $VENV/bin/python -m fart coverage --hours 3"
echo "  tail -f $FART_ROOT/logs/ingest.log"
