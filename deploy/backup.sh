#!/usr/bin/env bash
# SQLite online backup (backup API) + quick_check; keeps $KEEP_DAYS days under $FART_ROOT/data/backup.
# Runs as the fart user (systemd fart-backup.timer) or manually: sudo -u fart deploy/backup.sh
#
# Compression never uses `gzip -f FILE`: gzip then copies the ownership of the input onto the output
# with fchown(2), which the unit's SystemCallFilter (~@privileged contains @chown) kills with SIGSYS
# (exit 159) - the archive was complete but the retention step never ran (VPS, 26 Oct 2026).
# `gzip -n -c` writes to stdout and calls no chown; the output file is created by the shell.
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
# compress without fchown (see the header); verify the archive before the plain copy goes
gzip -n -c "$OUT" > "$OUT.gz.tmp"
mv -f "$OUT.gz.tmp" "$OUT.gz"
if ! gzip -t "$OUT.gz"; then
  echo "backup: gzip -t FAILED for $OUT.gz (plain copy kept at $OUT)" >&2
  rm -f "$OUT.gz"
  exit 1
fi
rm -f "$OUT"
echo "backup: compressed $OUT.gz (gzip -t ok)"
# retention: archives older than KEEP_DAYS days
find "$DEST" -name 'fart-*.db.gz' -type f -mtime "+$KEEP_DAYS" -print -delete | sed 's/^/backup: removed /' || true
echo "backup: done $(ls -1 "$DEST" | wc -l) file(s) kept"
