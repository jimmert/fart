"""python -m fart <command>

  ingest                 daemon: PumpPortal + RPC streams (creates, migrations, per-mint trades), sweep, coverage
  ingest replay --source S --since T [--until T]
                         re-feed the raw hour files of one source (pumpportal | rpc_logs | rpc_trades) to its handler,
                         data writes only, idempotent; T = ISO-8601 (2026-10-26T08:00Z) or unix seconds
  engine                 daemon: candidate engine v0 (15 s loop), observations only
  engine explain --day D read-only: why the coin and theme branches gave (no) observations on day D: candidate maxima per
                         threshold, the distribution per feature, probation data availability per hour next to the trade-stream
                         gaps (milestone 6; the daily report carries the same table). Thresholds unchanged, no advice.
  social [--once]        daemon: X mega-account / KOL watch + CA confirmation (15 s loop); --once runs one tick.
                         X_MODE off | shadow (default with a token: nothing reaches decisions) | live (needs the x_live rules key)
  social dryrun [--hours N] [--arrival uniform|envelope]
                         no HTTP: requests and USD per kind per day from config/x.json, the current confirmation keys and the
                         observed rates; the planner's full-load ceiling against the cap; the next N hours through the planner
                         (hard cap, reserve, KOL cap refuse; the envelope defers) (milestones 6 and 7)
  social calibrate --day D --console-usd N
                         read-only: the day's x_spend ledger per kind and unit next to the X developer console's amount for that
                         UTC day; the price per unit that fits (uniform or one component) and the config/x.json lines a commit
                         would need; writes nothing (milestone 7)
  backup verify          unpack the newest data/backup/fart-*.db.gz under data/, quick_check, row counts vs the live database
                         (live >= archive), clean up; exit 1 on failure, 2 without an archive (deploy/backup.sh calls it)
  budget calibrate --day D --dashboard-cu N
                         implied CU per byte of the websocket frames from the day's api_ledger and the Alchemy dashboard total;
                         prints the config/budget.json value that follows and what it means per day; writes nothing
  notify [--once]        daemon: Telegram outbox builder + sender to the owner chat (20 s loop)
  notify test            send one test message to the owner chat (only when TELEGRAM_NOTIFY_ENABLED=1)
  notify outbox          list the last 20 outbox rows
  measure track          one tracker run (systemd timer, every 5 min)
  measure report --day   daily recall report (systemd timer, 06:00 UTC)
  measure week --end D --days N
                         week report over N days ending at D (default: yesterday, 7): universe, hit rate, recall, lead time,
                         bootstrap intervals, costs and the frozen go/no-go test (systemd timer, 06:30 UTC daily; Monday = calendar week)
  events classify        counts per undecoded event discriminator with the best-guess name and emitting program
  chain global           decode the pump.fun Global account, compare with config/pumpfun.json (exit 1 on mismatch)
  chain pda <mint>       derive the bonding-curve PDA (and the unverified pump-amm pool PDA) for a mint
  status                 health, budgets, counts (works on an empty database); since milestone 10 also the last duration per
                         public view of the website (logs/web-views.json, written by fart web)
  coverage --hours N     per-hour creates/grads/gaps
  web                    daemon: the read-only website on FART_WEB_BIND (127.0.0.1:8792) behind the shared Caddy: the public
                         dashboard, observations, reports, method and tools pages plus /api/public/* (FART_WEB_PUBLIC=1, the
                         default; 0 = everything behind the login) and the owner section /eigenaar behind the login; needs
                         FART_WEB_PASSWORD_HASH and FART_WEB_SECRET in .env, opens the database read-only and writes nothing
                         but logs/web.log (milestones 8 and 9; the brand is config/web.json)
  web hash-password      ask for the owner's password invisibly (twice) and print only its scrypt hash for .env
  web new-secret         print a fresh cookie-signing secret for .env (a new secret logs every browser out)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from . import db
from .config import load_settings
from .engine import tickstats
from .log import setup_logging
from .util import day_key, day_start, now, utc_iso
from .version import rules_version


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fart", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="FART_ROOT override (default: $FART_ROOT or the repository)")
    p.add_argument("--json", action="store_true", help="machine-readable output where applicable")
    p.add_argument("--quiet", action="store_true", help="no stderr log lines")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("icmd", nargs="?", choices=["replay"], help="replay (default: run the daemon)")
    ing.add_argument("--source", choices=["pumpportal", "rpc_logs", "rpc_trades"], help="replay: which raw store")
    ing.add_argument("--since", help="replay: start (ISO-8601 or unix seconds)")
    ing.add_argument("--until", help="replay: end (ISO-8601 or unix seconds; default now)")
    en = sub.add_parser("engine")
    en.add_argument("ecmd", nargs="?", choices=["explain"], help="explain (default: run the daemon)")
    en.add_argument("--day", help="explain: UTC day YYYY-MM-DD (default: today)")
    so = sub.add_parser("social")
    so.add_argument("scmd", nargs="?", choices=["dryrun", "calibrate"], help="dryrun | calibrate (default: run the daemon)")
    so.add_argument("--once", action="store_true", help="run one tick and print the summary")
    so.add_argument("--day", help="calibrate: UTC day YYYY-MM-DD of the console amount")
    so.add_argument("--console-usd", type=float, help="calibrate: the X developer console's amount (USD) for that UTC day")
    so.add_argument("--hours", type=int, default=24, help="dryrun: hours to walk through the envelope from the current UTC hour (default 24)")
    so.add_argument("--arrival", choices=["uniform", "envelope"], default="uniform",
                    help="dryrun: how the day's posting volume arrives over the hours (default uniform)")
    bk = sub.add_parser("backup")
    bks = bk.add_subparsers(dest="bcmd", required=True)
    bks.add_parser("verify")
    bu = sub.add_parser("budget")
    bus = bu.add_subparsers(dest="bucmd", required=True)
    cal = bus.add_parser("calibrate")
    cal.add_argument("--day", required=True, help="UTC day YYYY-MM-DD of the Alchemy dashboard figure")
    cal.add_argument("--dashboard-cu", type=float, required=True, help="the dashboard's compute units for that UTC day (all methods together)")
    no = sub.add_parser("notify")
    no.add_argument("ncmd", nargs="?", choices=["test", "outbox"], help="test | outbox (default: run the daemon)")
    no.add_argument("--once", action="store_true", help="run one tick and print the summary")
    m = sub.add_parser("measure")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("track")
    r = ms.add_parser("report")
    r.add_argument("--day", help="UTC day YYYY-MM-DD (default: yesterday)")
    wk = ms.add_parser("week")
    wk.add_argument("--end", help="last UTC day of the window, YYYY-MM-DD (default: yesterday)")
    wk.add_argument("--days", type=int, help="number of days in the window (default: config/measure.json -> week.days = 7)")
    e = sub.add_parser("events")
    es = e.add_subparsers(dest="ecmd", required=True)
    ec = es.add_parser("classify")
    ec.add_argument("--fetch", type=int, default=0, help="fetch up to N sample transactions (RPC, budget-gated) to attribute old rows without a program")
    ch = sub.add_parser("chain")
    chs = ch.add_subparsers(dest="chcmd", required=True)
    chs.add_parser("global")
    pda = chs.add_parser("pda")
    pda.add_argument("mint")
    sub.add_parser("status")
    c = sub.add_parser("coverage")
    c.add_argument("--hours", type=int, default=24)
    w = sub.add_parser("web")
    w.add_argument("wcmd", nargs="?", choices=["hash-password", "new-secret"], help="hash-password | new-secret (default: run the server)")
    return p


def _kv_prefixed(conn, prefixes: tuple[str, ...]) -> dict[str, str]:
    where = " OR ".join("key LIKE ?" for _ in prefixes)
    return {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM kv WHERE {where}", tuple(f"{p}%" for p in prefixes))}


_DAY_SUFFIX = re.compile(r"\.\d{4}-\d{2}-\d{2}$")
FAILURE_PREFIXES = ("engine.tick_failed", "ingest.handle_failed.", "ingest.rejected.", "rpc_trades.subscription_capped", "rpc_trades.envelope_paused")


def _failure_counters(kv: dict[str, str]) -> dict[str, str]:
    """All-time failure counters (milestone 3), without the per-day keys and the .last* details."""
    out = {}
    for k, v in kv.items():
        if not k.startswith(FAILURE_PREFIXES) or _DAY_SUFFIX.search(k) or k.endswith(".last") or k.endswith(".last_at") or k.endswith("_at"):
            continue
        out[k] = v
    return dict(sorted(out.items()))


def cmd_status(settings, as_json: bool) -> int:
    from .net.budget import Budget

    out: dict[str, Any] = {"rules_version": rules_version(), "settings": settings.describe(), "db": str(settings.db_path)}
    if not settings.db_path.exists():
        out["note"] = "database does not exist yet (ingest has never run)"
        out["counts"] = {}
    else:
        conn = db.connect(settings.db_path)
        try:
            t = now()
            out["schema_version"] = db.schema_version(conn)
            out["counts"] = db.table_counts(conn, ("coins", "migrations", "trades", "watchlist", "decisions", "outcomes", "paper",
                                                   "coverage_gaps", "raw_undecoded", "social_posts", "outbox", "rejected_messages"))
            out["watchlist_active"] = conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0]
            out["trades_by_source_today"] = {r[0]: r[1] for r in conn.execute(
                "SELECT source, COUNT(*) FROM trades WHERE received_at >= ? GROUP BY source", (day_start(t),))}
            kv = _kv_prefixed(conn, ("ingest.", "engine.", "measure.", "retention.", "decode.", "pp.", "chain.", "social.", "notify.", "x.",
                                     "rpc_trades.", "enrich.", "schema.", "backup."))
            ages = {}
            for k in ("ingest.heartbeat", "ingest.pumpportal.last_msg_at", "ingest.rpc_logs.last_msg_at", "ingest.rpc_trades.last_msg_at",
                      "engine.heartbeat", "measure.track.last_run", "measure.report.last_run", "measure.week.last_run", "social.heartbeat",
                      "notify.heartbeat"):
                v = kv.get(k)
                ages[k] = None if v is None else round(t - float(v))
            out["age_seconds"] = ages
            out["counters"] = {k: v for k, v in kv.items() if k.startswith("decode.") or k.startswith("pp.")}
            out["failures"] = _failure_counters(kv)
            out["rejected_messages"] = {r[0]: r[1] for r in conn.execute(
                "SELECT reason, COUNT(*) FROM rejected_messages WHERE received_at >= ? GROUP BY reason", (day_start(t),))}
            out["schema_v3_cleanup"] = json.loads(kv["schema.v3.cleanup"]) if kv.get("schema.v3.cleanup") else None
            from .ingest.sweep import enrich_backlog, enrich_daily_limit

            icfg = settings.config("ingest")
            bcfg = dict(icfg.get("backfill", {}))
            bcfg.update(icfg.get("enrich", {}))
            rpc_src_cfg = settings.config("budget")["sources"].get("rpc_public" if settings.rpc_is_public else "rpc", {})
            tx_cost = float((rpc_src_cfg.get("cost_table") or {}).get("getTransaction", rpc_src_cfg.get("cost_default", 40)))
            out["enrich"] = {"used_today": int(kv.get(f"enrich.tx.{day_key(t)}", "0") or 0),
                             "limit": enrich_daily_limit(bcfg, None if settings.rpc_is_public else float(settings.rpc_daily_budget_cu), tx_cost),
                             "skipped_today": int(kv.get(f"enrich.skipped_budget.{day_key(t)}", "0") or 0),
                             "backlog": enrich_backlog(conn, int(bcfg.get("max_attempts", 3)))}
            out["pumpportal_last_http_status"] = kv.get("ingest.pumpportal.last_http_status")
            out["open_gaps"] = [dict(r) for r in conn.execute("SELECT source, start, reason FROM coverage_gaps WHERE end IS NULL")]
            budget = Budget.from_settings(conn, settings)
            out["budget"] = budget.snapshot()
            rpc_source = "rpc_public" if settings.rpc_is_public else "rpc"
            out["cu_by_method_today"] = [dict(r) for r in conn.execute(
                "SELECT method, COUNT(*) AS requests, ROUND(SUM(cost)) AS cost, SUM(bytes) AS bytes FROM api_ledger"
                " WHERE source=? AND ts>=? AND blocked_reason IS NULL GROUP BY method ORDER BY SUM(cost) DESC", (rpc_source, day_start(t)))]
            out["cu_per_byte"] = rpc_src_cfg.get("cu_per_byte")
            # milestone 4: only OPEN subscriptions here; what closed today is a separate line with its reasons
            out["top_subscriptions"] = [dict(r) for r in conn.execute(
                "SELECT mint, kind, reason, notifications, trades, since FROM ws_subscriptions"
                " WHERE until IS NULL ORDER BY notifications DESC LIMIT 5")]
            out["subscriptions_open"] = conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NULL").fetchone()[0]
            out["subscriptions_open_by_reason"] = {r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(reason, 'probation'), COUNT(*) FROM ws_subscriptions WHERE until IS NULL GROUP BY 1")}
            out["subscriptions_closed_today"] = {r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(close_reason, 'unknown'), COUNT(*) FROM ws_subscriptions WHERE until >= ? GROUP BY 1 ORDER BY 2 DESC", (day_start(t),))}
            out["watchlist_by_reason"] = {r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(reason, 'probation'), COUNT(*) FROM watchlist WHERE active=1 GROUP BY 1")}
            out["migrations_coin_unknown"] = {
                "total": conn.execute("SELECT COUNT(*) FROM migrations WHERE coin_unknown=1").fetchone()[0],
                "today": conn.execute("SELECT COUNT(*) FROM migrations WHERE coin_unknown=1 AND received_at >= ?", (day_start(t),)).fetchone()[0]}
            out["schema_v4_cleanup"] = json.loads(kv["schema.v4.cleanup"]) if kv.get("schema.v4.cleanup") else None
            out["schema_v5_cleanup"] = json.loads(kv["schema.v5.cleanup"]) if kv.get("schema.v5.cleanup") else None
            # milestone 5: the tracker's skipped mints and the engine's rules fingerprint
            out["tracker"] = {"skipped_total": int(kv.get("measure.track.skipped", "0") or 0),
                              "skipped_today": int(kv.get(f"measure.track.skipped.{day_key(t)}", "0") or 0),
                              "skipped_last": json.loads(kv["measure.track.skipped.last"]) if kv.get("measure.track.skipped.last") else None,
                              "max_mints_per_run": settings.config("measure")["sampling"].get("max_mints_per_run")}
            out["rules_fingerprints"] = {k[len("engine.rules_fingerprint."):]: v for k, v in kv.items() if k.startswith("engine.rules_fingerprint.")}
            # milestone 11: the budget of the last completed engine tick (every step) and the slow-tick counters
            out["engine_tick"] = tickstats.read(kv, t, float(settings.config("engine").get("loop_interval_s", 15)))
            out["week_report_last_end_day"] = kv.get("measure.week.last_end_day")
            out["global_check"] = {"status": kv.get("chain.global.status"), "checked_at": utc_iso(float(kv["chain.global.checked_at"])) if kv.get("chain.global.checked_at") else None,
                                   "mismatches": json.loads(kv.get("chain.global.mismatches") or "[]")}
            out["x_spend_today"] = [dict(r) for r in conn.execute(
                "SELECT kind, SUM(n) AS n, ROUND(SUM(usd), 4) AS usd FROM x_spend WHERE day=? GROUP BY kind ORDER BY usd DESC", (day_key(t),))]
            # milestone 7: the confirmation planner (active keys, stop reasons, deferrals) and the planned versus actual USD per kind
            from .social import planner as xplanner
            from .social.dryrun import planned_by_kind
            from .social.xbudget import XBudget

            out["x_planner"] = xplanner.status_counters(conn, t)
            xb = XBudget(conn, budget, settings.config("x"), settings.x_daily_budget_usd)
            try:
                planned = planned_by_kind(conn, settings, t)
            except Exception as e:  # the status must never die on the plan model
                planned = None
                out["x_plan_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            out["x_plan"] = xb.plan_view(t, planned)
            out["outbox"] = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status")}
            last_dec = conn.execute("SELECT MAX(as_of) FROM decisions").fetchone()[0]
            out["last_decision_at"] = utc_iso(last_dec) if last_dec else None
            # milestone 6: the X mode as the daemons run it, the shadow counters, the engine's candidate rows, the last backup check
            from .social.shadow import status_counters

            out["x_mode"] = {"effective": settings.x_mode, "requested": settings.x_mode_requested or "unset", "note": settings.x_mode_note,
                             "live_allowed": settings.x_live_allowed, "social_daemon": kv.get("social.x_mode"), "engine_daemon": kv.get("engine.x_mode")}
            out["x_shadow"] = status_counters(conn, t)
            out["engine_candidates_today"] = {r[0]: {"rows": int(r[1]), "with_trades": int(r[2] or 0), "passed_all": int(r[3] or 0)} for r in conn.execute(
                "SELECT kind, COUNT(*), SUM(COALESCE(max_trades_total, 0) > 0), SUM(passed_all) FROM engine_candidates WHERE day=? GROUP BY kind", (day_key(t),))}
            out["backup_verify_last"] = json.loads(kv["backup.verify.last"]) if kv.get("backup.verify.last") else None
        finally:
            conn.close()
    # milestone 10: the last duration per public view, as the website wrote it next to its log (never from the database)
    out["web_views"] = web_views_status(settings, now())
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    s = out["settings"]
    print(f"fart status  rules_version={out['rules_version']}  root={settings.root}")
    print(f"  rpc: {s['rpc_http']} ({'PUBLIC fallback, no key' if s['rpc_is_public'] else 'keyed'})  daily CU cap {s['rpc_daily_budget_cu']}")
    print(f"  pumpportal: {s['pumpportal']}  api key: {s['pumpportal_api_key']}  trades: {s['pumpportal_trades']}")
    print(f"  x: bearer token {s['x_bearer_token']}, mode {s['x_mode']} (X_MODE {s['x_mode_requested']}"
          + ("; live allowed by config/engine.json x_live" if s.get("x_live_allowed") else "; live needs \"x_live\": true in config/engine.json")
          + f"), daily budget ${s['x_daily_budget_usd']:.2f}   telegram: {s['telegram']}, notify "
          f"{'enabled' if s['telegram_notify_enabled'] else 'disabled (TELEGRAM_NOTIFY_ENABLED)'}")
    if s.get("x_mode_note"):
        print(f"  x mode note: {s['x_mode_note']}")
    print(f"  telegram community: {s.get('telegram_community', 'not configured')}, "
          f"{'ENABLED' if s.get('telegram_community_enabled') else 'disabled (TELEGRAM_COMMUNITY_ENABLED=0 or no chat id)'}, "
          f"preview to owner {'on' if s.get('telegram_community_preview') else 'off'}")
    print(f"  web: password hash {s.get('web_password_hash', 'not configured')}, secret {s.get('web_secret', 'not configured')}, "
          f"bind {s.get('web_bind')} (fart-web.service, read-only, behind Caddy), public section {'on' if s.get('web_public', True) else 'OFF (FART_WEB_PUBLIC=0)'}")
    if "note" in out:
        print(f"  {out['note']}")
        return 0
    print(f"  schema_version: {out['schema_version']}")
    print("  counts: " + ", ".join(f"{k}={v}" for k, v in out["counts"].items()) + f", watchlist_active={out['watchlist_active']}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(out["watchlist_by_reason"].items())) + ")" if out["watchlist_by_reason"] else ""))
    if out["trades_by_source_today"]:
        print("  trades today by source: " + ", ".join(f"{k}={v}" for k, v in sorted(out["trades_by_source_today"].items())))
    print("  freshness (s): " + ", ".join(f"{k}={v}" for k, v in out["age_seconds"].items() if v is not None) or "  freshness (s): none")
    if out["open_gaps"]:
        print("  open gaps: " + "; ".join(f"{g['source']} since {utc_iso(g['start'])} ({g['reason']})" for g in out["open_gaps"]))
    for name, b in out["budget"].items():
        u = b["usage_today"]
        cap = u["daily_cost_cap"]
        print(f"  budget {name:<13} today: {u['requests']} req, cost {u['cost']:.2f}" + (f"/{cap:.2f}" if cap else "") +
              f", blocked {u['blocked']}, errors {u['errors']}; tokens {b['tokens_available']}/{b['burst']}" +
              (f"; BACKOFF until {utc_iso(b['backoff_until'])}" if b["backoff_until"] and b["backoff_until"] > now() else "") +
              (f"; last_err: {str(b['last_err_msg'])[:60]}" if b["last_err_msg"] else ""))
    if out["cu_by_method_today"]:
        def _cu(r: dict[str, Any]) -> str:
            if r.get("bytes") is not None:
                return f"{r['method']} {r['cost']:.0f} CU = {int(r['bytes']):,} bytes ({r['requests']} batches)"
            return f"{r['method']} {r['cost']:.0f} ({r['requests']} req)"

        print("  rpc CU today by method: " + ", ".join(_cu(r) for r in out["cu_by_method_today"])
              + (f"  [cu_per_byte {out['cu_per_byte']}]" if out.get("cu_per_byte") is not None else ""))
    print(f"  subscriptions open: {out['subscriptions_open']}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(out["subscriptions_open_by_reason"].items())) + ")" if out["subscriptions_open_by_reason"] else "")
          + "; closed today: " + (", ".join(f"{k} {v}" for k, v in out["subscriptions_closed_today"].items()) or "none"))
    if out["top_subscriptions"]:
        print("  top open subscriptions (notifications): " + "; ".join(
            f"{r['mint'][:8]}… {r['kind']}/{r['reason']} {r['notifications']} (trades {r['trades']})" for r in out["top_subscriptions"]))
    if out["x_spend_today"]:
        print("  x spend today: " + ", ".join(f"{r['kind']} ${r['usd']:.3f} ({r['n']})" for r in out["x_spend_today"]))
    xp = out.get("x_planner") or {}
    if xp:
        act = xp.get("active") or {}
        stopped = xp.get("stopped_today") or {}
        deferred = {p["kind"]: p["deferred"] for p in (out.get("x_plan") or []) if p.get("deferred")}
        print(f"  x planner: active keys {xp.get('active_total', 0)}" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(act.items())) + ")" if act else "")
              + f", search pending {xp.get('search_pending', 0)}, due now {xp.get('due_now', 0)}; started today {xp.get('started_today', 0)}; stopped today: "
              + (", ".join(f"{k} {v['n']} (${v['usd']:.2f})" for k, v in sorted(stopped.items())) or "none")
              + "; deferred today: " + (", ".join(f"{k} {v}" for k, v in sorted(deferred.items())) or "none"))
    if out.get("x_plan"):
        cap = s["x_daily_budget_usd"]
        print(f"  x plan vs spend today (planned/actual USD, % of the ${cap:.2f} cap): " + ", ".join(
            f"{p['kind']} " + ("?" if p["planned_usd"] is None else f"${p['planned_usd']:.2f}") + f"/${p['actual_usd']:.2f} ("
            + ("?" if p["planned_share"] is None else f"{100 * p['planned_share']:.0f}%") + f"/{100 * (p['actual_share'] or 0):.0f}%)"
            for p in out["x_plan"]) + (f"  [plan error: {out['x_plan_error']}]" if out.get("x_plan_error") else ""))
    if out["outbox"]:
        print("  outbox: " + ", ".join(f"{k}={v}" for k, v in sorted(out["outbox"].items())))
    g = out["global_check"]
    if g["status"]:
        print(f"  global account check: {g['status']} at {g['checked_at']}" + (f" mismatches={json.dumps(g['mismatches'])}" if g["mismatches"] else ""))
    if out["counters"]:
        print("  counters: " + ", ".join(f"{k}={v}" for k, v in sorted(out["counters"].items())))
    print("  failures: " + (", ".join(f"{k}={v}" for k, v in out["failures"].items()) or "none"))
    if out["rejected_messages"]:
        print("  rejected messages today: " + ", ".join(f"{k}={v}" for k, v in sorted(out["rejected_messages"].items())))
    e = out["enrich"]
    print(f"  enrich (getTransaction): {e['used_today']}/{e['limit']} today, skipped {e['skipped_today']}, backlog migrations={e['backlog'].get('migrate_tx', 0)} creates={e['backlog'].get('create_tx', 0)}")
    if out["schema_v3_cleanup"]:
        print(f"  schema v3 cleanup (system-mint rows removed at {utc_iso(out['schema_v3_cleanup'].get('at'))}): {json.dumps(out['schema_v3_cleanup'].get('deleted'))}")
    if out["schema_v4_cleanup"]:
        print(f"  schema v4 cleanup (graduation rows closed at {utc_iso(out['schema_v4_cleanup'].get('at'))}): {json.dumps(out['schema_v4_cleanup'].get('counts'))}")
    if out.get("schema_v5_cleanup"):
        print(f"  schema v5 cleanup (meta_burst rows trimmed at {utc_iso(out['schema_v5_cleanup'].get('at'))}): {json.dumps(out['schema_v5_cleanup'].get('counts'))}")
    tr = out.get("tracker") or {}
    print(f"  tracker: skipped mints total={tr.get('skipped_total', 0)} today={tr.get('skipped_today', 0)} (measure.track.skipped; max_mints_per_run {tr.get('max_mints_per_run')})"
          + (f"  last={json.dumps(tr['skipped_last'])}" if tr.get("skipped_last") else ""))
    if out.get("rules_fingerprints"):
        print("  rules fingerprints (engine): " + ", ".join(f"{k}={str(v)[:19]}" for k, v in sorted(out["rules_fingerprints"].items())))
    if out.get("engine_tick"):
        print("  " + tickstats.status_line(out["engine_tick"]))
    if out.get("week_report_last_end_day"):
        print(f"  week report: last end day {out['week_report_last_end_day']}")
    xm = out.get("x_mode") or {}
    xs = out.get("x_shadow") or {}
    print(f"  x shadow: decisions total={xs.get('decisions_total', 0)} today={xs.get('decisions_today', 0)}; confirmations today={xs.get('confirmations_today', 0)}"
          f" (confirmed within 1 h {xs.get('confirmed_1h_today', 0)}, within 24 h {xs.get('confirmed_24h_today', 0)});"
          f" daemons report social={xm.get('social_daemon') or 'not started'} engine={xm.get('engine_daemon') or 'not started'}"
          + (f"; last shadow decision {utc_iso(xs['last_decision_at'])}" if xs.get("last_decision_at") else ""))
    ec = out.get("engine_candidates_today") or {}
    cc, mc = ec.get("coin") or {}, ec.get("meta") or {}
    print(f"  engine candidates today: coins={cc.get('rows', 0)} (with trades {cc.get('with_trades', 0)}, passed all thresholds {cc.get('passed_all', 0)}),"
          f" metas={mc.get('rows', 0)} (passed all {mc.get('passed_all', 0)})  [fart engine explain --day {day_key(now())}]")
    bv = out.get("backup_verify_last")
    if bv:
        cts = bv.get("counts") or {}
        detail = (str(bv.get("archive") or "").rsplit("/", 1)[-1] + "; " + ", ".join(f"{k} {v.get('archive')}" for k, v in cts.items())) if bv.get("archive") else "no archive"
        print(f"  backup: last verify {bv.get('status', '?')} at {bv.get('checked_at')} ({detail})" + (f" errors={json.dumps(bv.get('errors'))}" if bv.get("errors") else ""))
    else:
        print("  backup: never verified (fart backup verify; deploy/backup.sh runs it after every backup)")
    mu = out["migrations_coin_unknown"]
    if mu["total"]:
        print(f"  graduations without a known create (coin_unknown, never watched): {mu['total']} total, {mu['today']} today")
    if out["pumpportal_last_http_status"]:
        print(f"  pumpportal last HTTP refusal: {out['pumpportal_last_http_status']}")
    print(f"  last decision: {out['last_decision_at']}")
    print("  " + web_views_line(out["web_views"]))
    return 0


WEB_VIEWS_FILE = "web-views.json"
WEB_VIEWS_ORDER = ("themes", "live", "hot", "new", "graduations", "social", "today", "status", "coverage", "observations", "reports", "week", "method")


def web_views_status(settings, t: float) -> dict[str, Any]:
    """`logs/web-views.json` (milestone 10): `fart web` writes the last duration per public view after every cycle of
    its background thread; the website itself never writes the database. Missing: the website has not run since M10."""
    p = settings.logs_dir / WEB_VIEWS_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        written = float(d.get("written_at") or 0.0)
        views = d.get("views") or {}
        if not isinstance(views, dict):
            raise ValueError("views is not an object")
    except (OSError, ValueError, TypeError) as e:
        return {"file": str(p), "exists": p.exists(), "error": None if not p.exists() else f"{type(e).__name__}: {str(e)[:80]}", "age_s": None, "views": {}}
    return {"file": str(p), "exists": True, "error": None, "written_at": utc_iso(written), "age_s": round(max(0.0, t - written)), "views": views}


def _secs(s: Any) -> str:
    """'0.02 s', '0.4 s', '12 s'; under five milliseconds '0.00 s' (the same figure /eigenaar shows)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return "?"
    if v < 0.005:
        return "0.00 s"
    return f"{v:.2g} s" if v < 10 else f"{v:.0f} s"


def web_views_line(wv: dict[str, Any]) -> str:
    """`web views: themes 0.02 s, live 0.4 s, ...` plus the age of the file, or why there is nothing."""
    if not wv.get("exists"):
        return "web views: no timings yet (fart web writes logs/web-views.json after its first cycle; milestone 10)"
    if wv.get("error"):
        return f"web views: unreadable ({wv['error']})"
    views = wv.get("views") or {}
    parts = []
    for name in WEB_VIEWS_ORDER:
        v = views.get(name)
        if isinstance(v, dict):
            parts.append(f"{name} {_secs(v.get('seconds'))}" + ("" if v.get("ok", True) and v.get("state") != "failed" else " (FAILED)"))
    reports = [k for k, v in views.items() if k.startswith("report:") and isinstance(v, dict)]
    if reports:
        slowest = max(float(views[k].get("seconds") or 0.0) for k in reports)
        parts.append(f"report pages {len(reports)} (slowest {_secs(slowest)})")
    failed = sum(1 for v in views.values() if isinstance(v, dict) and (v.get("state") == "failed" or not v.get("ok", True)))
    tail = f"  [written {wv.get('age_s')} s ago" + (f", {failed} failing" if failed else "") + "]"
    return "web views: " + (", ".join(parts) if parts else "nothing computed yet") + tail


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
    print(f"{'hour (UTC)':<14}{'pp':>6}{'rpc':>6}{'union':>7}{'grads':>7}{'no-create':>11}{'gaps(s)':>9}{'recov(s)':>10}  covered  complete")
    for r in rows:
        gap_s = sum(g["seconds"] for g in r["gaps"] if not g.get("recovered"))
        rec_s = sum(g["seconds"] for g in r["gaps"] if g.get("recovered"))
        print(f"{r['hour']:<14}{r['creates_pp']:>6}{r['creates_rpc']:>6}{r['creates_union']:>7}{r['grads']:>7}{r['grads_without_create']:>11}"
              f"{gap_s:>9.0f}{rec_s:>10.0f}  {'yes' if r.get('covered') else 'no':<7}  {'yes' if r['complete'] else 'no'}")
    return 0


def _attribute_from_transaction(tx: dict[str, Any] | None, disc_hex: str) -> str | None:
    """Program that emitted the payload with this discriminator in a getTransaction response."""
    from .chain.events import ANCHOR_EVENT_CPI_DISC, attribute_program_data

    if not isinstance(tx, dict):
        return None
    for program, payload in attribute_program_data((tx.get("meta") or {}).get("logMessages")):
        if payload[:8] == ANCHOR_EVENT_CPI_DISC:
            payload = payload[8:]
        if payload[:8].hex() == disc_hex:
            return program
    return None


def cmd_events_classify(settings, as_json: bool, fetch_n: int) -> int:
    from .chain.events import PUMP_AMM_PROGRAM, PUMP_PROGRAM, classify_discriminator

    if not settings.db_path.exists():
        print("no database yet; nothing to classify")
        return 0
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT discriminator, program, COUNT(*) AS n, MIN(received_at) AS first_seen, MAX(received_at) AS last_seen,"
            " MIN(signature) AS sample_signature, MAX(LENGTH(payload_b64)) AS max_b64_len,"
            " GROUP_CONCAT(DISTINCT source) AS sources, MIN(reason) AS reason"
            " FROM raw_undecoded WHERE reason NOT LIKE 'ignored_event%' GROUP BY discriminator, program ORDER BY n DESC"
        ).fetchall()
        items = []
        fetched = 0
        rpc = None
        for r in rows:
            d = dict(r)
            d["reason"] = (d["reason"] or "").split(":")[0]
            d.update({k: v for k, v in classify_discriminator(d["discriminator"] or "").items() if k != "discriminator"})
            program = d.get("program")
            if program is None and fetch_n and fetched < fetch_n and d.get("sample_signature"):
                if rpc is None:
                    from .chain.rpc import RpcClient
                    from .net.budget import Budget

                    rpc = RpcClient(Budget.from_settings(conn, settings), settings.rpc_http_url, settings.rpc_is_public)
                try:
                    program = _attribute_from_transaction(rpc.get_transaction(d["sample_signature"]), d["discriminator"] or "")
                    d["program_source"] = "fetched"
                except Exception as e:  # Blocked / HttpError / RpcError: report, never crash
                    d["fetch_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                fetched += 1
            d["program"] = program
            d["program_label"] = {PUMP_PROGRAM: "pump.fun", PUMP_AMM_PROGRAM: "pump-amm"}.get(program or "", program)
            items.append(d)
    finally:
        conn.close()
    if as_json:
        print(json.dumps(items, indent=2, default=str))
        return 0
    if not items:
        print("no undecoded payloads recorded yet (raw_undecoded is empty)")
        return 0
    print(f"{'discriminator':<17}{'n':>6}  {'reason':<19}{'emitting program':<46}{'best guess':<44}{'status':<14}first seen            sample signature")
    for d in items:
        guess = (d.get("name") or "-") + (f" [{'/'.join(d['programs'])}]" if d.get("programs") else "")
        prog = d.get("program_label") or ("? (run with --fetch N on the VPS)" if d.get("program") is None else d["program"])
        print(f"{d['discriminator'] or '-':<17}{d['n']:>6}  {d['reason']:<19}{str(prog)[:45]:<46}{guess[:43]:<44}{d['status']:<14}"
              f"{utc_iso(d['first_seen'])}  {d['sample_signature'] or '-'}")
    print("\nstatus: decoded = we have a layout now (re-run after upgrading to see it disappear); ignored = known, skipped;"
          " other_program = the name matches a Jupiter/Raydium/Orca/Meteora event (guess); unknown = no candidate name."
          " The emitting program comes from the log nesting (rows written by milestone 2) or from --fetch.")
    return 0


def cmd_chain_global(settings, as_json: bool) -> int:
    from .chain.global_account import TO_VERIFY_LAYOUT, VERIFIED_LAYOUT, fetch_and_check
    from .chain.rpc import RpcClient
    from .net.budget import Budget

    conn = db.connect(settings.db_path)
    try:
        rpc = RpcClient(Budget.from_settings(conn, settings), settings.rpc_http_url, settings.rpc_is_public)
        res = fetch_and_check(rpc, settings.config("pumpfun"))
    finally:
        conn.close()
    if as_json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"pump.fun Global {res['address']}  status={res['status']}" + (f"  error={res.get('error')}" if res.get("error") else ""))
        dec = res.get("decoded")
        if dec:
            cfg = settings.config("pumpfun")
            from .chain.global_account import CONFIG_MAP

            inv = {v: k for k, v in CONFIG_MAP.items()}
            print(f"  owner={res.get('owner')}  discriminator_ok={dec['discriminator_ok']}  bytes={dec['raw_len']} consumed={dec['consumed']} extra={dec['extra_bytes']}")
            print(f"  {'field':<34}{'chain':<48}{'config':<22}layout")
            for name, _ in VERIFIED_LAYOUT:
                if name not in dec["verified"]:
                    continue
                cfg_key = inv.get(name)
                cfg_val = cfg.get(cfg_key) if cfg_key else ""
                print(f"  {name:<34}{str(dec['verified'].get(name))[:47]:<48}{str(cfg_val if cfg_key else '-'):<22}verified (VPS + IDL)")
            for name, _ in TO_VERIFY_LAYOUT:
                if name not in dec["to_verify"]:
                    continue
                cfg_key = inv.get(name)
                cfg_val = cfg.get(cfg_key) if cfg_key else ""
                print(f"  {name:<34}{str(dec['to_verify'].get(name))[:47]:<48}{str(cfg_val if cfg_key else '-'):<22}IDL (newer field)")
            if dec["suspect"]:
                print("  suspect: " + "; ".join(dec["suspect"]))
        for m in res.get("mismatches", []):
            print(f"  MISMATCH {m['field']}: chain={m['chain']} config[{m['config_key']}]={m['config']}" + ("" if m.get("verified_layout") else " (IDL-only field)"))
    return {"ok": 0, "mismatch": 1}.get(res["status"], 2)


def cmd_chain_pda(settings, mint: str, as_json: bool) -> int:
    from .chain.base58 import is_pubkey
    from .chain.pda import bonding_curve_address, pump_amm_pool_address

    if not is_pubkey(mint):
        print(f"not a valid base58 pubkey: {mint}")
        return 2
    pf = settings.config("pumpfun")
    out: dict[str, Any] = {"mint": mint, "bonding_curve_pda": bonding_curve_address(mint, pf["program_id"]),
                           "pump_amm_pool_pda_unverified": pump_amm_pool_address(mint, pf["program_id"], pf["pump_amm_program_id"], pf["wsol_mint"])}
    rc = 0
    if settings.db_path.exists():
        conn = db.connect_ro(settings.db_path)
        try:
            c = conn.execute("SELECT bonding_curve FROM coins WHERE mint=?", (mint,)).fetchone()
            m = conn.execute("SELECT pool_address FROM migrations WHERE mint=?", (mint,)).fetchone()
        finally:
            conn.close()
        out["db_bonding_curve"] = c["bonding_curve"] if c else None
        out["db_pool_address"] = m["pool_address"] if m else None
        if out["db_bonding_curve"] and out["db_bonding_curve"] != out["bonding_curve_pda"]:
            rc = 1
        out["pool_pda_matches_db"] = None if not out["db_pool_address"] else out["db_pool_address"] == out["pump_amm_pool_pda_unverified"]
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        for k, v in out.items():
            print(f"  {k}: {v}")
        if rc:
            print("  WARNING: the derived bonding curve differs from the one in the database (CreateEvent) - check fart/chain/pda.py")
    return rc


def parse_time(value: str | None) -> float | None:
    """ISO-8601 (2026-10-26T08:00Z, 2026-10-26T08:00:00+00:00, 2026-10-26) or unix seconds -> unix seconds."""
    if value is None or not str(value).strip():
        return None
    v = str(value).strip()
    try:
        return float(v)
    except ValueError:
        pass
    import datetime as _dt

    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    d = _dt.datetime.fromisoformat(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def cmd_ingest_replay(settings, source: str | None, since: str | None, until: str | None, as_json: bool) -> int:
    from .ingest.service import replay_raw

    if not source or not since:
        print("ingest replay needs --source {pumpportal,rpc_logs,rpc_trades} and --since <ISO-8601 or unix seconds>")
        return 2
    if not settings.db_path.exists():
        print("no database yet")
        return 2
    try:
        t0 = parse_time(since)
        t1 = parse_time(until)
    except ValueError as e:
        print(f"bad time: {e}")
        return 2
    stats = replay_raw(settings, source, float(t0), t1)
    if as_json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"replay {source}: files={stats['files']} records={stats['records']} replayed={stats['replayed']} skipped={stats['skipped']}"
              f" errors={stats['errors']} kinds={json.dumps(stats['kinds'])}")
    return 0 if stats["errors"] == 0 else 1


def cmd_engine_explain(settings, day: str, as_json: bool) -> int:
    """Read-only: the day's candidate maxima against the thresholds (milestone 6)."""
    from .engine.explain import build_explain, render_text

    if not settings.db_path.exists():
        print("no database yet")
        return 0
    try:
        day_bounds_check(day)
    except ValueError:
        print(f"bad day: {day} (YYYY-MM-DD)")
        return 2
    conn = db.connect_ro(settings.db_path)
    try:
        if not _table_exists(conn, "engine_candidates"):
            print("the database has no engine_candidates table yet (schema v6): start the daemons once after the milestone 6 upgrade")
            return 2
        res = build_explain(conn, settings, day, now())
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str) if as_json else render_text(res))
    return 0


def day_bounds_check(day: str) -> float:
    from .util import day_bounds

    return day_bounds(day)[0]


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def cmd_social_dryrun(settings, hours: int, arrival: str, as_json: bool) -> int:
    """No HTTP: the X calls and USD per kind per day, and the next hours through the envelope (milestone 6)."""
    from .social.dryrun import build, render_text

    conn = db.connect(settings.db_path)  # creates an empty database when none exists: the estimate then rests on the assumptions
    try:
        res = build(conn, settings, now(), max(1, int(hours)), arrival)
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str) if as_json else render_text(res))
    return 0


def cmd_social_calibrate(settings, day: str | None, console_usd: float | None, as_json: bool) -> int:
    """Read-only: the day's X ledger per kind and unit against the console's amount (milestone 7)."""
    from .social.calibrate import calibrate, render_text

    if not day or console_usd is None:
        print("social calibrate needs --day YYYY-MM-DD and --console-usd <the console's USD for that UTC day>")
        return 2
    if not settings.db_path.exists():
        print("no database yet")
        return 2
    try:
        day_bounds_check(day)
    except ValueError:
        print(f"bad day: {day} (YYYY-MM-DD)")
        return 2
    conn = db.connect_ro(settings.db_path)
    try:
        res = calibrate(conn, settings, day, float(console_usd))
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str) if as_json else render_text(res))
    return 0


def cmd_budget_calibrate(settings, day: str, dashboard_cu: float, as_json: bool) -> int:
    """Read-only: the implied CU per byte from the ledger and the dashboard total (milestone 6)."""
    from .net.calibrate import calibrate, render_text

    if not settings.db_path.exists():
        print("no database yet")
        return 2
    try:
        day_bounds_check(day)
    except ValueError:
        print(f"bad day: {day} (YYYY-MM-DD)")
        return 2
    conn = db.connect_ro(settings.db_path)
    try:
        res = calibrate(conn, settings, day, float(dashboard_cu))
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str) if as_json else render_text(res))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(root=args.root)
    settings.ensure_dirs()
    daemon = ((args.cmd == "ingest" and not args.icmd) or (args.cmd == "engine" and not args.ecmd) or (args.cmd == "social" and not args.scmd)
              or (args.cmd == "measure") or (args.cmd == "notify" and not args.ncmd) or (args.cmd == "web" and not args.wcmd))
    setup_logging(settings.logs_dir if daemon else None, args.cmd if args.cmd != "measure" else f"measure-{args.mcmd}", stderr=not args.quiet)
    if args.cmd == "status":
        return cmd_status(settings, args.json)
    if args.cmd == "coverage":
        return cmd_coverage(settings, args.hours, args.json)
    if args.cmd == "events":
        return cmd_events_classify(settings, args.json, args.fetch)
    if args.cmd == "chain":
        if args.chcmd == "global":
            return cmd_chain_global(settings, args.json)
        return cmd_chain_pda(settings, args.mint, args.json)
    if args.cmd == "ingest":
        if args.icmd == "replay":
            return cmd_ingest_replay(settings, args.source, args.since, args.until, args.json)
        from .ingest.service import run_ingest

        return run_ingest(settings)
    if args.cmd == "engine":
        if args.ecmd == "explain":
            return cmd_engine_explain(settings, args.day or day_key(now()), args.json)
        from .engine.service import run_engine

        return run_engine(settings)
    if args.cmd == "social":
        if args.scmd == "dryrun":
            return cmd_social_dryrun(settings, args.hours, args.arrival, args.json)
        if args.scmd == "calibrate":
            return cmd_social_calibrate(settings, args.day, args.console_usd, args.json)
        from .social.service import run_social

        return run_social(settings, once=args.once)
    if args.cmd == "backup":
        from .backup import cmd_verify

        return cmd_verify(settings, args.json)
    if args.cmd == "budget":
        return cmd_budget_calibrate(settings, args.day, args.dashboard_cu, args.json)
    if args.cmd == "notify":
        from .notify import service as ns

        if args.ncmd == "test":
            return ns.cmd_test(settings)
        if args.ncmd == "outbox":
            return ns.cmd_outbox(settings, args.json)
        return ns.run_notify(settings, once=args.once)
    if args.cmd == "web":
        from .web import server as ws

        if args.wcmd == "hash-password":
            return ws.cmd_hash_password()
        if args.wcmd == "new-secret":
            return ws.cmd_new_secret()
        return ws.run_web(settings)
    if args.cmd == "measure":
        if args.mcmd == "track":
            from .measure.outcomes import run_track

            return run_track(settings)
        if args.mcmd == "week":
            from .measure.week import run_week

            return run_week(settings, args.end, args.days)
        day = args.day or day_key(now() - 86400)
        from .measure.report import run_report

        return run_report(settings, day)
    return 2


if __name__ == "__main__":
    sys.exit(main())
