"""The plays daemon (`fart plays`, fart-plays.service): every 15 s it tests the proven rules of config/plays.json on the
coins the radar follows right now, opens a paper position per new signal, follows the open positions through the new
trades, records the outcome after costs and queues one Telegram alert per play to the owner (kind `play`, at most six per
hour) through the existing outbox. It runs next to the engine, never inside it; the rule files are imported where the
engine's own feature definitions are needed (read-only) and never changed.

Per tick:
1. the candidates per rule family: the first-minutes hypotheses (H1, H4, H5, H7) look at the active watchlist coins
   younger than the rule's horizon; H2 at the watched coins whose curve passed 70 SOL; H6 at the graduations of 28 to 35
   minutes ago; H3 at the coins of the bursting themes of the engine's own snapshot (kv `engine.metas_latest`);
2. the features of every candidate as-of now (`fart/research/features.py`: the replay features, the bundle flags, the
   creator's sells; the creator's history and the theme roles only when a rule needs them), with the previous tick's
   features kept in memory for the "rising" test;
3. for every active rule and candidate without a `plays` row for (rule, version, mint): if the rule fires, a row with the
   features and the curve state at the signal, a `Position` (the shared execution model) and an alert;
4. every open position gets the trades (and after graduation the tracker samples) that arrived since its last point; the
   position decides fill, partial exit, exit; the row carries the state. A position that is still open 24 hours after its
   fill is closed as UNKNOWN (no price path to an exit), a signal without a fill within ten minutes is NO FILL.

Without an active rule the daemon idles with a heartbeat (kv `plays.heartbeat`, `plays.rules_active` = 0) and the website
says so. `fart status` shows the heartbeat, the rules, the open positions and the alerts of the day.

Milestone 13: the daemon also FOLLOWS signals (`fart/plays/follow.py`). Every tick it evaluates the pre-registered follow
triggers of `fart/research/hypotheses.py::FOLLOW_TRIGGERS` on its candidates (with or without an active rule), follows every
coin that meets one, every coin a rule fired on and every coin with an observe decision of the engine, and polls the
BondingCurve accounts of the followed coins outside the trade stream, so every signal keeps a price path for 24 hours. The
open positions are fed the combined path (trades, polls, samples, candles): a live play never turns unknown while the coin
exists. The follow step reports in `fart status` (`follow:` line) and kv `follow.*`.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import time
from typing import Any

from .. import db
from ..chain.curve import CurveParams, CurveState
from ..config import Settings
from ..log import get_logger
from ..measure.paper import Costs, Position, PricePoint
from ..net.budget import Budget
from ..notify import outbox
from ..notify.format import age as age_label, esc, fmt_time, n as fmt_n, with_footer
from ..research import features as rf
from ..research import harness as rh
from ..research import hypotheses as hyps
from ..util import day_key, now as wall_now, utc_iso
from ..version import rules_version
from . import follow as follow_mod
from . import rules as rules_mod

log = get_logger("plays")

KV_HEARTBEAT = "plays.heartbeat"
KV_RULES = "plays.rules_active"
KV_LAST_TICK = "plays.last_tick"
KV_ALERTS = "plays.alerts"
OPEN_STATES = ("signal", "open")
UNKNOWN_AFTER_S = 86400.0 + 600.0     # an open position without an exit this long after the signal is closed as unknown
ALERT_KIND = "play"
MAX_CANDIDATES = 400


class PlaysEngine:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, clock=wall_now, rpc: Any = None, follow_cfg: follow_mod.FollowConfig | None = None):
        self.conn = conn
        self.settings = settings
        self.clock = clock
        self.params = CurveParams.from_config(settings.config("pumpfun"))
        mcfg = settings.config("measure") or {}
        rcfg = mcfg.get("research") or {}
        self.costs = Costs.from_config(rcfg)
        self.method_version = str(mcfg.get("method_version", "m1.0"))
        self.h24 = max(int(h) for h in mcfg.get("horizons_s") or [86400])
        self.dev_sell_window_s = float((mcfg.get("creator_stats") or {}).get("dev_sell_window_s", rf.DEV_SELL_WINDOW_S))
        self.tick_s = float(rcfg.get("tick_s", 15))
        self.reload_rules()
        self.prev: dict[str, tuple[float, dict[str, Any]]] = {}
        self.creator_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.theme_roles: dict[str, dict[str, Any]] = {}
        self.theme_seen: dict[str, float] = {}
        self.rules_version = rules_version()
        self.stats: dict[str, int] = {}
        # milestone 13: the follow step (its own budget gate for the keyed RPC; a test injects a fake `rpc`)
        fcfg = follow_cfg or follow_mod.FollowConfig.from_settings(settings)
        budget = None
        if rpc is None and fcfg.enabled and not (settings.rpc_is_public and fcfg.require_keyed_rpc):
            budget = Budget.from_settings(conn, settings, clock=clock)
        self.follower = follow_mod.Follower(conn, settings, budget=budget, rpc=rpc, clock=clock, cfg=fcfg)
        self.follow_enabled = fcfg.enabled and follow_mod.table_exists(conn)

    def reload_rules(self) -> None:
        self.rules = rules_mod.load_rules(self.settings)
        self.cfg = rules_mod.settings_of(self.settings)

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    # ----- candidates -------------------------------------------------------------------------------------------------
    def families(self) -> set[str]:
        """The hypothesis families the tick needs candidates for: those of the active rules, plus (milestone 13) those of the
        follow triggers while the follow step is on."""
        fams = {r.hypothesis for r in self.rules}
        if self.follow_enabled:
            for t in hyps.FOLLOW_TRIGGERS:
                fams.update(t.hypotheses)
        return fams

    def candidates(self, now: float, fams: set[str] | None = None) -> dict[str, rh.Coin]:
        fams = self.families() if fams is None else fams
        out: dict[str, rh.Coin] = {}
        if fams & {"H1", "H4", "H5", "H7"}:
            horizon = max(hyps.get(h).horizon_s for h in fams & {"H1", "H4", "H5", "H7"})
            rows = self.conn.execute("SELECT w.mint FROM watchlist w JOIN coins c ON c.mint = w.mint WHERE w.active=1 AND COALESCE(c.block_time, c.received_at) >= ? LIMIT ?",
                                     (now - horizon - 60, MAX_CANDIDATES)).fetchall()
            for r in rows:
                out.setdefault(str(r[0]), None)  # type: ignore[arg-type]
        if "H2" in fams:
            # the follow trigger of H2 starts at 60 SOL in the curve (virtual 90 SOL), the rule itself at the 75 SOL crossing
            rows = self.conn.execute("SELECT DISTINCT mint FROM trades WHERE received_at >= ? AND virtual_sol >= ? AND mint IN (SELECT mint FROM watchlist WHERE active=1) LIMIT ?",
                                     (now - 900, int((30.0 + hyps.FOLLOW_CURVE_SOL) * 1e9), MAX_CANDIDATES)).fetchall()
            for r in rows:
                out.setdefault(str(r[0]), None)  # type: ignore[arg-type]
        if "H6" in fams:
            rows = self.conn.execute("SELECT mint FROM migrations WHERE COALESCE(block_time, received_at) BETWEEN ? AND ? LIMIT ?",
                                     (now - hyps.H6_AFTER_S - 300, now - hyps.H6_AFTER_S + 300, MAX_CANDIDATES)).fetchall()
            for r in rows:
                out.setdefault(str(r[0]), None)  # type: ignore[arg-type]
        if "H3" in fams:
            self.refresh_theme_roles(now)
            for mint in self.theme_roles:
                out.setdefault(mint, None)  # type: ignore[arg-type]
        loaded: dict[str, rh.Coin] = {}
        for mint in list(out)[:MAX_CANDIDATES]:
            c = rh.load_coin(self.conn, mint, with_path=("H6" in fams or "H2" in fams))
            if c is not None:
                c.trades = rf.knowable(c.trades, now)
                loaded[mint] = c
        return loaded

    def refresh_theme_roles(self, now: float) -> None:
        """H3 live: the bursting terms of the engine's own snapshot; a term's roles are assigned when it first bursts
        (the coins of the window with the term: the strongest and the first are leaders, later coins are copies)."""
        from ..engine import themes as engine_themes
        from ..engine.meta import load_stopwords, tokenize

        snap = engine_themes.read(self.conn)
        if snap is None or now - float(snap["as_of"]) > engine_themes.MAX_AGE_S:
            return
        ecfg = self.settings.config("engine")
        mcfg = ecfg.get("meta") or {}
        window_s = float(mcfg.get("window_s", 10800))
        min_len = int(mcfg.get("min_token_len", 4))
        stop = load_stopwords(self.settings.config("stopwords"))
        for m in snap.get("metas") or []:
            term = str(m.get("term") or "")
            if not term or not m.get("bursting") or term in self.theme_seen:
                continue
            self.theme_seen[term] = now
            cands = []
            for r in self.conn.execute("SELECT mint, name, symbol, COALESCE(block_time, received_at) AS t FROM coins WHERE received_at >= ? AND COALESCE(block_time, received_at) > ? ORDER BY t",
                                       (now - window_s - 60, now - window_s)):
                if term in tokenize(r["name"], r["symbol"], stop, min_len):
                    cands.append((str(r["mint"]), float(r["t"])))
            scored = []
            for mint, t in cands:
                tr = rf.knowable(rf.load_trades(self.conn, mint), now)
                if tr:
                    scored.append((mint, t, rf.window_stats([x for x in tr if x.t <= t + 180], t + 180, 180.0).unique_buyers))
            if not scored:
                continue
            strongest = max(scored, key=lambda s: (s[2], -s[1]))
            first = min(scored, key=lambda s: s[1])
            for mint, t, _ in scored:
                role = "leader_strongest" if mint == strongest[0] else ("leader_first" if mint == first[0] else ("copy" if t > first[1] + 600 else None))
                if role and mint not in self.theme_roles:
                    self.theme_roles[mint] = {"role": role, "term": term, "decision_as_of": now, "signal_t": now if role != "copy" else t + 120.0, "leader_created_t": first[1]}
        # forget roles older than the window
        for mint in [m for m, r in self.theme_roles.items() if now - float(r["decision_as_of"]) > window_s]:
            self.theme_roles.pop(mint, None)
        for term in [t for t, ts in self.theme_seen.items() if now - ts > window_s]:
            self.theme_seen.pop(term, None)

    # ----- features -------------------------------------------------------------------------------------------------------
    def context(self, coin: rh.Coin, now: float, needs_creator: bool, needs_theme: bool) -> hyps.Ctx:
        f = rf.replay_features(coin.trades, now, coin.created_t, self.params) if coin.trades else {"as_of": now, "age_s": round(now - coin.created_t, 1), "trades_total": 0, "unique_buyers_5m": 0, "net_sol_5m": 0.0}
        if coin.points:
            rf.apply_poll_state(f, coin.points, now, self.params)   # milestone 13: a newer poll of the curve account carries the state (H2's crossing may be seen on it)
        prev = self.prev.get(coin.mint)
        f_prev = prev[1] if prev is not None and now - prev[0] <= 2 * self.tick_s + 1 else None
        bundle = rf.bundle_flags(coin.trades, now, coin.created_t, coin.dev_buy_sol, coin.slot, graduation_sol=self.params.graduation_real_sol / self.params.sol_unit, creator=coin.creator)
        dev = rf.dev_sell(coin.trades, now, coin.created_t, coin.creator, self.dev_sell_window_s)
        graduated = coin.grad_t is not None and coin.grad_t <= now
        ctx = hyps.Ctx(coin.mint, coin.created_t, now, f, f_prev, bundle, dev, graduated, coin.grad_t, None, self.theme_roles.get(coin.mint) if needs_theme else None, None, None,
                       int(time.gmtime(now).tm_hour))
        if needs_creator and int(f.get("trades_total") or 0) >= 3 and coin.creator:
            key = (coin.creator, day_key(now))
            if key not in self.creator_cache:
                self.creator_cache[key] = rh.creator_history_capped(self.conn, coin, now, self.method_version, self.h24, self.dev_sell_window_s, 50)
            ctx.creator = self.creator_cache[key]
        if graduated and coin.samples:
            target = coin.grad_t + hyps.H6_AFTER_S
            near = [s for s in coin.samples if abs(float(s["ts"]) - target) <= rh.SAMPLE_TOLERANCE_S and rh.sample_price_sol(s) is not None and float(s["ts"]) <= now]
            if near:
                s = min(near, key=lambda s: abs(float(s["ts"]) - target))
                ctx.sample = {"ts": float(s["ts"]), "price_sol": rh.sample_price_sol(s), "volume_h1_usd": rh.sample_volume_h1(s), "liquidity_sol": rh.sample_liquidity_sol(s)}
                last = rf.last_state(coin.trades, coin.grad_t)
                ctx.grad_price_sol = None if last is None else CurveState(int(last.virtual_sol), int(last.virtual_token)).price_sol(self.params)
        self.prev[coin.mint] = (now, f)
        return ctx

    # ----- the tick -------------------------------------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        summary: dict[str, Any] = {"as_of": now, "rules": len(self.rules), "candidates": 0, "signals": 0, "followed": 0, "closed": 0, "alerts": 0, "unknown": 0, "no_fill": 0,
                                   "follow_new": 0, "follow_triggers": 0, "polls": None}
        t0 = time.perf_counter()
        fams = self.families()
        triggers = list(hyps.FOLLOW_TRIGGERS) if self.follow_enabled else []
        follow_items: list[tuple[str, float, str]] = []
        if fams:
            coins = self.candidates(now, fams)
            summary["candidates"] = len(coins)
            needs_creator = any(r.hyp.needs_creator for r in self.rules)
            needs_theme = any(r.hyp.needs_theme for r in self.rules) or any(t.needs_theme for t in triggers)
            if needs_theme and not any(r.hyp.needs_theme for r in self.rules):
                self.refresh_theme_roles(now)
            existing = {(str(r["rule"]), int(r["version"]), str(r["mint"])) for r in self.conn.execute("SELECT rule, version, mint FROM plays WHERE as_of >= ?", (now - 2 * 86400,))}
            new_rows: list[tuple[rules_mod.Rule, rh.Coin, hyps.Ctx]] = []
            for mint, coin in coins.items():
                ctx = self.context(coin, now, needs_creator, needs_theme)
                for rule in self.rules:
                    if (rule.id, rule.version, mint) in existing:
                        continue
                    try:
                        fired = rule.fires(ctx)
                    except Exception:  # noqa: BLE001
                        log.error("plays.predicate_failed", rule=rule.id, mint=mint, exc_info=True)
                        fired = False
                    if fired:
                        new_rows.append((rule, coin, ctx))
                        follow_items.append((mint, now, f"play:{rule.id}"))
                for trig in triggers:
                    try:
                        hit = bool(trig.fires(ctx))
                    except Exception:  # noqa: BLE001  a trigger bug must never turn into a silent miss
                        log.error("follow.trigger_failed", trigger=trig.id, mint=mint, exc_info=True)
                        hit = False
                    if hit:
                        follow_items.append((mint, now, trig.id))
                        summary["follow_triggers"] += 1
            if new_rows:
                summary["signals"] = db.write_tx(self.conn, lambda c: sum(1 for rule, coin, ctx in new_rows if self.open_play(c, rule, coin, ctx, now)), what="plays.signals")
            # forget the previous features of coins that left the candidate set
            for mint in [m for m, (t, _) in self.prev.items() if now - t > 3 * self.tick_s]:
                self.prev.pop(mint, None)
        if self.follow_enabled:
            summary["follow_new"] = int(self.follower.follow_many(follow_items, now)["new"]) + self.follower.add_observe_decisions(now) + self.follower.add_plays(now)
            self.follower.refresh(now)
            polls = self.follower.poll(now)
            summary["polls"] = ({k: polls.get(k) for k in ("enabled", "active", "due", "calls", "polled", "points", "graduated", "errors", "blocked", "level", "paused", "over_cap")}
                                if polls.get("enabled") else {"enabled": False, "active": polls.get("active"), "reason": polls.get("disabled_reason")})
        if self.rules:
            summary.update(self.follow(now))
            summary["alerts"] = self.alerts(now)
        summary["seconds"] = round(time.perf_counter() - t0, 3)

        def _hb(c: sqlite3.Connection) -> None:
            db.kv_set(c, KV_HEARTBEAT, str(now), now)
            db.kv_set(c, KV_RULES, str(len(self.rules)), now)
            db.kv_set(c, KV_LAST_TICK, json.dumps(summary, sort_keys=True), now)

        db.write_tx(self.conn, _hb, what="plays.heartbeat")
        return summary

    def open_play(self, c: sqlite3.Connection, rule: rules_mod.Rule, coin: rh.Coin, ctx: hyps.Ctx, now: float) -> bool:
        f = ctx.f
        entry = {k: f.get(k) for k in ("virtual_sol", "virtual_token", "price_sol", "mcap_sol", "graduation_progress", "curve_sol", "trades_total")}
        if ctx.sample is not None:
            entry["sample"] = ctx.sample
        pos = Position(now, self.cfg["size_sol"], self.params, rule.exit_rules, self.costs, self.cfg["entry_delay_s"], None, rh.MAX_ENTRY_WAIT_S)
        feats = {**f, **ctx.bundle, **ctx.dev, **(ctx.creator or {}), "theme": ctx.theme}
        cur = c.execute("INSERT OR IGNORE INTO plays(rule, version, hypothesis, variant, mint, as_of, created_at, features_json, entry_json, size_sol, status, rules_version, json)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,'signal',?,?)",
                        (rule.id, rule.version, rule.hypothesis, rule.variant_id, coin.mint, now, now, json.dumps(feats, default=str), json.dumps(entry, default=str),
                         self.cfg["size_sol"], self.rules_version, json.dumps(pos.to_json(), default=str)))
        if cur.rowcount:
            log.info("plays.signal", rule=rule.id, mint=coin.mint, buyers=f.get("unique_buyers_5m"), net_sol=f.get("net_sol_5m"))
            return True
        return False

    def follow(self, now: float) -> dict[str, int]:
        """Feed every open position the points that arrived since its last one; write the state back."""
        out = {"followed": 0, "closed": 0, "unknown": 0, "no_fill": 0}
        rows = self.conn.execute("SELECT * FROM plays WHERE status IN ('signal', 'open') ORDER BY as_of").fetchall()
        updates: list[tuple[int, Position, str]] = []
        for r in rows:
            try:
                pos = Position.from_json(json.loads(r["json"]), self.params)
            except (TypeError, ValueError, KeyError):
                log.error("plays.state_unreadable", play=int(r["id"]))
                continue
            coin = rh.load_coin(self.conn, str(r["mint"]))
            if coin is None:
                continue
            since = pos.last_point.t if pos.last_point is not None else float(r["as_of"])
            path = [p for p in rh.price_path(coin, float(r["as_of"])) if p.t > since and p.t <= now]
            pos.feed_all(path)
            status = pos.status
            if status == "open" and now - float(r["as_of"]) > UNKNOWN_AFTER_S:
                status = "unknown"
            elif status == "signal" and now - float(r["as_of"]) > self.cfg["entry_delay_s"] + rh.MAX_ENTRY_WAIT_S:
                status = "no_fill"
                pos.status = "no_fill"
                pos.exit_reason = "no_trade_after_entry"
            updates.append((int(r["id"]), pos, status))
            out["followed"] += 1
            if status == "closed":
                out["closed"] += 1
            elif status == "unknown":
                out["unknown"] += 1
            elif status == "no_fill":
                out["no_fill"] += 1

        def _write(c: sqlite3.Connection) -> None:
            for pid, pos, status in updates:
                e = pos.entry
                last_spot = pos.last_point.spot(self.params) if pos.last_point is not None else None
                c.execute("UPDATE plays SET status=?, entry_time=?, entry_price=?, fill_price=?, tokens=?, fees_sol=?, peak_price=?, last_price=?, last_seen=?,"
                          " exit_time=?, exit_price=?, exit_reason=?, exit_sol_net=?, pnl_sol=?, pnl_pct=?, json=? WHERE id=?",
                          (status, None if e is None else e.t, None if e is None else e.spot, None if e is None else e.fill_price, None if e is None else e.tokens / 1e6,
                           round(pos.fees_sol, 9), pos.peak, last_spot, now, pos.exit_t, next((f.spot for f in reversed(pos.fills) if f.kind == "sell"), None) if status == "closed" else None,
                           pos.exit_reason, pos.sold_sol if status == "closed" else None, pos.pnl_sol if status == "closed" else None, pos.pnl_pct if status == "closed" else None,
                           json.dumps(pos.to_json(), default=str), pid))

        if updates:
            db.write_tx(self.conn, _write, what="plays.follow")
        return out

    # ----- alerts ------------------------------------------------------------------------------------------------------------
    def alerts(self, now: float) -> int:
        if not self.settings.telegram_enabled:
            return 0
        rows = self.conn.execute("SELECT * FROM plays WHERE alerted_at IS NULL AND status IN ('signal', 'open') AND as_of >= ? ORDER BY as_of", (now - 900,)).fetchall()
        if not rows:
            return 0
        used = int(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind=? AND created_at > ?", (ALERT_KIND, now - 3600)).fetchone()[0])
        room = max(0, self.cfg["alerts_per_hour"] - used)
        rules = {r.id: r for r in self.rules}
        footer = str((self.settings.config("notify") or {}).get("footer") or "Observatie, geen advies. Alles wordt achteraf gemeten.")
        domain = str(self.settings.web_site.get("domain") or "")
        items = []
        for r in rows:
            rule = rules.get(str(r["rule"]))
            if rule is None:
                continue
            coin = self.conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (r["mint"],)).fetchone()
            items.append((int(r["id"]), play_message(dict(r), rule, coin["name"] if coin else None, coin["symbol"] if coin else None, footer, domain, now)))

        def _write(c: sqlite3.Connection) -> int:
            n = 0
            for pid, text in items:
                if n < room and outbox.enqueue(c, ALERT_KIND, f"play:{pid}", self.settings.telegram_owner_chat_id, text, now):
                    n += 1
                    db.counter_bump_daily(c, KV_ALERTS, 1, now)
                c.execute("UPDATE plays SET alerted_at=? WHERE id=?", (now, pid))   # over the hourly bound: counted as not sent, never sent late
            return n

        n = db.write_tx(self.conn, _write, what="plays.alerts")
        if len(items) > n:
            self._bump("alerts_over_limit", len(items) - n)
        return n


def play_message(row: dict[str, Any], rule: rules_mod.Rule, name: str | None, symbol: str | None, footer: str, domain: str, now: float) -> str:
    """The Telegram text of a play, in plain Dutch: the rule and its measured figures, the coin, the signal, the entry state,
    the exit rule, the mint as text and the Terminal page of our own site. No link to a trading venue, no buy button."""
    try:
        f = json.loads(row.get("features_json") or "{}")
    except ValueError:
        f = {}
    try:
        e = json.loads(row.get("entry_json") or "{}")
    except ValueError:
        e = {}
    fig = rule.figures
    ci = ("" if fig.get("ci_low_sol") is None else f" (interval {fmt_n(fig['ci_low_sol'], 3)} tot {fmt_n(fig.get('ci_high_sol'), 3)} SOL)")
    lines = [
        f"<b>Play</b> {esc(rule.name)} (regel {esc(rule.id)}) · {fmt_time(float(row['as_of']))}",
        f"<b>{esc(name or f.get('name') or '?')}</b> (${esc(symbol or f.get('symbol') or '?')}) · leeftijd {age_label(f.get('age_s'))}",
        f"<code>{esc(row.get('mint'))}</code>",
        f"signaal: {fmt_n(f.get('unique_buyers_5m'))} unieke kopers en {fmt_n(f.get('net_sol_5m'), 2)} SOL netto in 5 min · grootste koper {('onbekend' if f.get('largest_buyer_share_15m') is None else str(round(100 * float(f['largest_buyer_share_15m']))) + '%')}"
        f" · dev-buy {('onbekend' if f.get('dev_buy_frac_of_curve') is None else str(round(100 * float(f['dev_buy_frac_of_curve']), 1)) + '%')} van de curve · gebundeld {('nee' if f.get('bundled_any') is False else ('ja' if f.get('bundled_any') else 'onbekend'))}",
        f"instapstand: {fmt_n(e.get('curve_sol'), 1)} SOL in de curve van 85 · marktwaarde {fmt_n(e.get('mcap_sol'), 1)} SOL · {('onbekend' if e.get('graduation_progress') is None else str(round(100 * float(e['graduation_progress']))) + '%')} naar graduatie",
        f"gemeten (rapport {esc(rule.report)}): trefkans {('onbekend' if fig.get('hit_rate') is None else str(round(100 * float(fig['hit_rate']))) + '%')}, verwachte waarde "
        f"{fmt_n(fig.get('ev_sol'), 3)} SOL per {fmt_n(fig.get('size_sol') or row.get('size_sol'), 1)} SOL na kosten{ci}, {fmt_n(fig.get('cases'))} gevallen",
        f"uitstap: {esc(rule.exit_text)}",
        f"papierpositie {fmt_n(row.get('size_sol'), 2)} SOL, instap {fmt_n(rule.raw.get('figures', {}).get('entry_delay_s', 20), 0)} s na dit signaal; de uitkomst komt op /plays.",
    ]
    if domain:
        lines.append(f"terminal: https://{esc(domain)}/terminal?mint={esc(row.get('mint'))}")
    lines.append(f"data: play #{row.get('id')} · rules_version {esc(row.get('rules_version'))}")
    return with_footer(lines, footer)


def run_plays(settings: Settings, once: bool = False) -> int:
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    eng = PlaysEngine(conn, settings)
    interval = float(eng.cfg.get("loop_interval_s", 15))
    if once:
        try:
            print(json.dumps(eng.tick(), indent=2, default=str))
        finally:
            conn.close()
        return 0
    stopping = {"v": False}

    def _stop(*_: Any) -> None:
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("plays.started", rules=len(eng.rules), interval_s=interval, size_sol=eng.cfg["size_sol"], entry_delay_s=eng.cfg["entry_delay_s"],
             telegram="enabled" if settings.telegram_enabled else "disabled",
             follow=("polls " + ("on" if eng.follower.polls_enabled else f"off: {eng.follower.disabled_reason}")) if eng.follow_enabled else "disabled")
    if not eng.rules:
        log.info("plays.idle", reason="config/plays.json has no active rule: nothing is proven yet; idling with a heartbeat" + (" and following signals" if eng.follow_enabled else ""))
    last_log = 0.0
    last_reload = 0.0
    failures = 0
    while not stopping["v"]:
        t0 = time.monotonic()
        try:
            if time.monotonic() - last_reload > 60:
                last_reload = time.monotonic()
                eng.reload_rules()
            s = eng.tick()
            if s.get("signals") or s.get("closed") or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log.info("plays.tick", **s)
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("plays.tick_failed", exc_info=True, failures=failures)
            db.record_failure(conn, "plays.tick_failed", wall_now(), detail=f"{type(e).__name__}: {str(e)[:150]}")
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("plays.stopped", failures=failures)
    return 0


def status(conn: sqlite3.Connection, settings: Settings, now: float) -> dict[str, Any]:
    """For `fart status` and the pages: the daemon's heartbeat, the rules, the open positions and today's alerts."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays'").fetchone() is None:
        return {"exists": False, "rules": 0}
    hb = db.kv_get(conn, KV_HEARTBEAT)
    rules = rules_mod.load_rules(settings)
    by = {str(r[0]): int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM plays GROUP BY status")}
    last = db.kv_get(conn, KV_LAST_TICK)
    try:
        last_tick = json.loads(last) if last else None
    except ValueError:
        last_tick = None
    return {"exists": True, "rules": len(rules), "rule_ids": [r.id for r in rules], "heartbeat": None if hb is None else float(hb),
            "heartbeat_age_s": None if hb is None else round(max(0.0, now - float(hb))), "by_status": by, "open": by.get("signal", 0) + by.get("open", 0),
            "closed": by.get("closed", 0), "unknown": by.get("unknown", 0), "no_fill": by.get("no_fill", 0),
            "alerts_today": int(db.kv_get(conn, f"{KV_ALERTS}.{day_key(now)}", "0") or 0), "last_tick": last_tick,
            "pnl_sol_closed": conn.execute("SELECT COALESCE(SUM(pnl_sol), 0) FROM plays WHERE status='closed'").fetchone()[0]}
