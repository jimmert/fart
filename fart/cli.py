"""python -m fart <command>

  ingest                 daemon: PumpPortal + RPC streams, sweep, coverage, retention
  engine                 daemon: candidate engine v0 (15 s loop), observations only
  measure track          one tracker run (systemd timer, every 5 min)
  measure report --day   daily recall report (systemd timer, 06:00 UTC)
  status                 health, budgets, counts (works on an empty database)
  coverage --hours N     per-hour creates/grads/gaps
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import db
from .config import load_settings
from .log import setup_logging
from .util import day_key, now, utc_iso
from .version import rules_version


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fart", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="FART_ROOT override (default: $FART_ROOT or the repository)")
    p.add_argument("--json", action="store_true", help="machine-readable output where applicable")
    p.add_argument("--quiet", action="store_true", help="no stderr log lines")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    sub.add_parser("engine")
    m = sub.add_parser("measure")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("track")
    r = ms.add_parser("report")
    r.add_argument("--day", help="UTC day YYYY-MM-DD (default: yesterday)")
    sub.add_parser("status")
    c = sub.add_parser("coverage")
    c.add_argument("--hours", type=int, default=24)
    return p


def cmd_status(settings, as_json: bool) -> int:
    from .net.budget import Budget

    out: dict[str, Any] = {"rules_version": rules_version(), "settings": settings.describe(), "db": str(settings.db_path)}
    if not settings.db_path.exists():
        out["note"] = "database does not exist yet (ingest has never run)"
        out["counts"] = {}
    else:
        conn = db.connect(settings.db_path)
        try:
            out["schema_version"] = db.schema_version(conn)
            out["counts"] = db.table_counts(conn, ("coins", "migrations", "trades", "watchlist", "decisions", "outcomes", "paper", "coverage_gaps", "raw_undecoded"))
            out["watchlist_active"] = conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0]
            kv = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'ingest.%' OR key LIKE 'engine.%' OR key LIKE 'measure.%' OR key LIKE 'retention.%' OR key LIKE 'decode.%'")}
            t = now()
            ages = {}
            for k in ("ingest.heartbeat", "ingest.pumpportal.last_msg_at", "ingest.rpc_logs.last_msg_at", "engine.heartbeat", "measure.track.last_run", "measure.report.last_run"):
                v = kv.get(k)
                ages[k] = None if v is None else round(t - float(v))
            out["age_seconds"] = ages
            out["counters"] = {k: v for k, v in kv.items() if k.startswith("decode.") or k.startswith("pp.")}
            out["open_gaps"] = [dict(r) for r in conn.execute("SELECT source, start, reason FROM coverage_gaps WHERE end IS NULL")]
            budget = Budget.from_settings(conn, settings)
            out["budget"] = budget.snapshot()
            last_dec = conn.execute("SELECT MAX(as_of) FROM decisions").fetchone()[0]
            out["last_decision_at"] = utc_iso(last_dec) if last_dec else None
        finally:
            conn.close()
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"fart status  rules_version={out['rules_version']}  root={settings.root}")
    s = out["settings"]
    print(f"  rpc: {s['rpc_http']} ({'PUBLIC fallback, no key' if s['rpc_is_public'] else 'keyed'})  daily CU cap {s['rpc_daily_budget_cu']}")
    print(f"  pumpportal: {s['pumpportal']}")
    if "note" in out:
        print(f"  {out['note']}")
        return 0
    print("  counts: " + ", ".join(f"{k}={v}" for k, v in out["counts"].items()) + f", watchlist_active={out['watchlist_active']}")
    print("  freshness (s): " + ", ".join(f"{k}={v}" for k, v in out["age_seconds"].items()))
    if out["open_gaps"]:
        print("  open gaps: " + "; ".join(f"{g['source']} since {utc_iso(g['start'])} ({g['reason']})" for g in out["open_gaps"]))
    for name, b in out["budget"].items():
        u = b["usage_today"]
        cap = u["daily_cost_cap"]
        print(f"  budget {name:<13} today: {u['requests']} req, cost {u['cost']:.0f}" + (f"/{cap:.0f}" if cap else "") +
              f", blocked {u['blocked']}, errors {u['errors']}; tokens {b['tokens_available']}/{b['burst']}" +
              (f"; BACKOFF until {utc_iso(b['backoff_until'])}" if b["backoff_until"] and b["backoff_until"] > now() else "") +
              (f"; last_err: {str(b['last_err_msg'])[:60]}" if b["last_err_msg"] else ""))
    if out["counters"]:
        print("  counters: " + ", ".join(f"{k}={v}" for k, v in sorted(out["counters"].items())))
    print(f"  last decision: {out['last_decision_at']}")
    return 0


def cmd_coverage(settings, hours: int, as_json: bool) -> int:
    from .ingest import coverage

    if not settings.db_path.exists():
        print("no database yet")
        return 0
    conn = db.connect_ro(settings.db_path)
    try:
        rows = coverage.report(conn, hours, now())
    finally:
        conn.close()
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'hour (UTC)':<14}{'pp':>6}{'rpc':>6}{'union':>7}{'grads':>7}{'no-create':>11}{'gaps(s)':>9}  complete")
    for r in rows:
        gap_s = sum(g["seconds"] for g in r["gaps"])
        print(f"{r['hour']:<14}{r['creates_pp']:>6}{r['creates_rpc']:>6}{r['creates_union']:>7}{r['grads']:>7}{r['grads_without_create']:>11}{gap_s:>9.0f}  {'yes' if r['complete'] else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(root=args.root)
    settings.ensure_dirs()
    daemon = args.cmd in ("ingest", "engine") or (args.cmd == "measure")
    setup_logging(settings.logs_dir if daemon else None, args.cmd if args.cmd != "measure" else f"measure-{args.mcmd}", stderr=not args.quiet)
    if args.cmd == "status":
        return cmd_status(settings, args.json)
    if args.cmd == "coverage":
        return cmd_coverage(settings, args.hours, args.json)
    if args.cmd == "ingest":
        from .ingest.service import run_ingest

        return run_ingest(settings)
    if args.cmd == "engine":
        from .engine.service import run_engine

        return run_engine(settings)
    if args.cmd == "measure":
        if args.mcmd == "track":
            from .measure.outcomes import run_track

            return run_track(settings)
        day = args.day or day_key(now() - 86400)
        from .measure.report import run_report

        return run_report(settings, day)
    return 2


if __name__ == "__main__":
    sys.exit(main())
