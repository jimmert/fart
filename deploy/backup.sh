#!/usr/bin/env bash
# SQLite online backup (backup API) + quick_check; keeps 14 days under $FART_ROOT/data/backup.
# Runs as the fart user (systemd fart-backup.timer) or manually: sudo -u fart deploy/backup.sh
set -euo pipefail
FART_ROOT="${FART_ROOT:-/opt/fart}"
PY="${PY:-$FART_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY=python3
DB="$FART_ROOT/data/fart.db"
DEST="$FART_ROOT/data/backup"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$DEST"
if [ ! -f "$DB" ]; then
  echo "backup: no database at $DB (nothing to do)"
  exit 0
fi
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$DEST/fart-$STAMP.db"
"$PY" - "$DB" "$OUT" <<'PYEOF'
import sqlite3, sys, os
src_path, out_path = sys.argv[1], sys.argv[2]
tmp = out_path + ".tmp"
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(tmp)
with dst:
    src.backup(dst, pages=1024)
src.close()
res = dst.execute("PRAGMA quick_check").fetchone()[0]
n_coins = dst.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
n_dec = dst.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
dst.close()
if res != "ok":
    os.unlink(tmp)
    print(f"backup: quick_check FAILED: {res}", file=sys.stderr)
    sys.exit(1)
os.replace(tmp, out_path)
print(f"backup: ok quick_check=ok coins={n_coins} decisions={n_dec} -> {out_path}")
PYEOF
gzip -f "$OUT"
find "$DEST" -name 'fart-*.db.gz' -type f -mtime "+$KEEP_DAYS" -print -delete | sed 's/^/backup: removed /' || true
echo "backup: done $(ls -1 "$DEST" | wc -l) file(s) kept"
