"""X API v2 watch: mega accounts every 60 s in the peak hours and 120 s outside, KOLs every 180 s / 300 s, velocity
refresh every 2 min, and the confirmation planner (milestone 7, `fart/social/planner.py`): counts per key every 15 min
in the first two hours after an observation and hourly after that, one search per confirmed key, fast-post matching.

Endpoints (pay-per-use, prices in config/x.json, TO VERIFY against the console):
  GET /2/tweets/search/recent   per post returned (deduplicated per UTC day) + per author expansion (mega/KOL only)
  GET /2/tweets/counts/recent   per request
  GET /2/users?ids=             per user (the author lookup of at most `search_max_authors` authors after a confirmation search)
  GET /2/tweets?ids=            assumed free for same-day re-reads (booked as `refresh`, TO VERIFY)

Every call: the budget planner (`XBudget.admit`: hard caps against the ceiling cost, the hourly envelope against the
expected cost, deferral instead of refusal) -> generic gate (rate, hard USD cap, backoff on 429 with
x-rate-limit-reset) -> urllib -> actual cost booked in x_spend (with its unit) and api_ledger, and against the key it
served (`x_keys.usd`). The bearer token travels only in the Authorization header and is never logged.

Milestone 6, shadow mode (`XClient(shadow=True)`, the default with a token until `X_MODE=live` is allowed): the same
calls and the same money, but a fast post becomes a `shadow_decisions` row (never a `decisions` row) and a
confirmation count a `shadow_confirmations` row (never a `social_counts` row), each linked to the observe /
observe_meta decision it would have confirmed (`fart/social/shadow.py`).
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import urllib.parse
from typing import Any, Callable

from .. import db
from ..db import kv_get, kv_set
from ..engine import decide
from ..engine.meta import normalize, words
from ..log import Throttle, get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError, request_json
from ..util import now as wall_now
from . import planner
from . import shadow as shadow_mod
from .planner import clean_ticker, confirmation_query, meta_term_query  # noqa: F401  (re-exported: the query builders live with the planner)
from .xbudget import SOURCE, XBudget, XVerdict

log = get_logger("x")

TWEET_FIELDS = "public_metrics,created_at,author_id"
USER_FIELDS = "username,public_metrics"
_FROM_RE = re.compile(r"from:([A-Za-z0-9_]+)")


# ----- pure helpers ----------------------------------------------------------------------------------
def parse_created_at(value: Any) -> float | None:
    """X ISO-8601 ('2026-10-26T13:00:00.000Z') -> unix seconds; None when unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso_utc(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kol_query(accounts: list[str]) -> str:
    return "(" + " OR ".join(f"from:{a}" for a in accounts) + ") -is:retweet"


def accounts_in_query(query: str) -> int:
    return len(set(_FROM_RE.findall(query or "")))


def interval_for(section: dict[str, Any], peak: bool, default_peak: float, default_off: float) -> float:
    """The interval of an account watch: `interval_peak_s` in the peak hours, `interval_off_s` outside (milestone 7);
    an old config with a single `interval_s` keeps it for both."""
    if "interval_peak_s" in section or "interval_off_s" in section:
        return float(section.get("interval_peak_s" if peak else "interval_off_s", default_peak if peak else default_off))
    return float(section.get("interval_s", default_peak if peak else default_off))


def rare_words(text: str | None, stopwords: set[str], min_len: int = 4) -> list[str]:
    out: list[str] = []
    for w in words(text):
        if len(w) >= min_len and w not in stopwords and not w.isdigit() and w not in out and not w.startswith("http"):
            out.append(w)
    return out


def velocity(samples: list[tuple[float, int, int]], created_at: float | None) -> tuple[float | None, float | None]:
    """(likes/min, quotes/min) from the last two metric samples (ts, likes, quotes); with one sample,
    the average since the post was created."""
    if not samples:
        return None, None
    samples = sorted(samples)
    t2, l2, q2 = samples[-1]
    if len(samples) >= 2:
        t1, l1, q1 = samples[-2]
        dt = (t2 - t1) / 60.0
        if dt > 0:
            return max(0.0, (l2 - l1) / dt), max(0.0, (q2 - q1) / dt)
    if created_at is not None and t2 > created_at:
        age = (t2 - created_at) / 60.0
        return l2 / age, q2 / age
    return None, None


def is_fast(likes: int, likes_per_min: float | None, quotes_per_min: float | None, age_s: float, mcfg: dict[str, Any]) -> bool:
    if age_s > float(mcfg.get("refresh_max_age_s", 7200)):
        return False
    if likes_per_min is not None and likes_per_min >= float(mcfg.get("fast_likes_per_min", 150)) and likes >= int(mcfg.get("fast_min_likes", 3000)):
        return True
    return quotes_per_min is not None and quotes_per_min >= float(mcfg.get("fast_quotes_per_min", 3))


def posts_from_response(resp: Any, kind: str, received_at: float, query_key: str | None = None) -> list[dict[str, Any]]:
    """Normalize a search / lookup response into post dicts (author from includes.users when expanded)."""
    if not isinstance(resp, dict):
        return []
    users = {}
    for u in (resp.get("includes") or {}).get("users") or []:
        if isinstance(u, dict) and u.get("id"):
            users[str(u["id"])] = u
    out = []
    for t in resp.get("data") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        pm = t.get("public_metrics") or {}
        u = users.get(str(t.get("author_id")))
        upm = (u or {}).get("public_metrics") or {}
        out.append({
            "id": str(t["id"]), "platform": "x", "author": (u or {}).get("username"), "author_id": str(t.get("author_id")) if t.get("author_id") else None,
            "text": t.get("text"), "created_at": parse_created_at(t.get("created_at")), "received_at": received_at,
            "likes": pm.get("like_count"), "quotes": pm.get("quote_count"), "replies": pm.get("reply_count"), "impressions": pm.get("impression_count"),
            "kind": kind, "followers": upm.get("followers_count"), "query_key": query_key,
        })
    return out


def users_from_response(resp: Any) -> list[dict[str, Any]]:
    """Normalize a `GET /2/users?ids=` response: id, username, followers."""
    if not isinstance(resp, dict):
        return []
    out = []
    for u in resp.get("data") or []:
        if isinstance(u, dict) and u.get("id"):
            out.append({"id": str(u["id"]), "username": u.get("username"), "followers": ((u.get("public_metrics") or {}).get("followers_count"))})
    return out


def pick_authors(posts: list[dict[str, Any]], max_authors: int, already_seen: Callable[[str], bool]) -> list[str]:
    """The distinct authors of a confirmation search worth a lookup: the most engaged posts first (likes + quotes +
    replies), newest first on a tie, skipping authors already expanded today (known and paid for), at most `max_authors`."""
    ranked = sorted(posts, key=lambda p: (-(int(p.get("likes") or 0) + int(p.get("quotes") or 0) + int(p.get("replies") or 0)), -(p.get("created_at") or 0.0)))
    out: list[str] = []
    for p in ranked:
        a = p.get("author_id")
        if not a or a in out or already_seen(a):
            continue
        out.append(a)
        if len(out) >= max(0, int(max_authors)):
            break
    return out


def counts_from_response(resp: Any, now: float) -> tuple[int | None, int | None]:
    """(n_last_hour, n_24h): hourly buckets ending within the last 60 minutes, and the total."""
    if not isinstance(resp, dict):
        return None, None
    buckets = resp.get("data") or []
    last_hour = 0
    seen = False
    for b in buckets:
        if not isinstance(b, dict):
            continue
        end = parse_created_at(b.get("end"))
        n = b.get("tweet_count")
        if end is not None and isinstance(n, int) and end >= now - 3600:
            last_hour += n
            seen = True
    total = (resp.get("meta") or {}).get("total_tweet_count")
    if total is None and buckets:
        total = sum(int(b.get("tweet_count") or 0) for b in buckets if isinstance(b, dict))
    return (last_hour if seen or buckets else None), (int(total) if isinstance(total, int) else None)


def confirmation_candidates(conn: sqlite3.Connection, now: float, cfg: dict[str, Any], stopwords: set[str]) -> list[dict[str, Any]]:
    """The ranked candidate keys of the planner right now (`planner.candidates`): coin observations youngest first,
    then the strongest themes. `cfg` is the whole X config (milestone 6 passed the `confirm` block alone)."""
    if "confirm" not in cfg:
        cfg = {"confirm": cfg}
    return planner.candidates(conn, now, cfg, stopwords)


# ----- storage ------------------------------------------------------------------------------------------
def upsert_post(conn: sqlite3.Connection, p: dict[str, Any]) -> bool:
    """Insert a post or refresh its metrics. received_at keeps the first arrival (as-of guarantee).
    Returns True when the post was new."""
    row = conn.execute("SELECT 1 FROM social_posts WHERE id=?", (p["id"],)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO social_posts(id, platform, author, author_id, text, created_at, received_at, likes, quotes, replies, impressions,"
            " kind, followers, query_key, json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p.get("platform", "x"), p.get("author"), p.get("author_id"), p.get("text"), p.get("created_at"), p["received_at"],
             p.get("likes"), p.get("quotes"), p.get("replies"), p.get("impressions"), p["kind"], p.get("followers"), p.get("query_key"), None),
        )
        new = True
    else:
        conn.execute(
            "UPDATE social_posts SET likes=COALESCE(?, likes), quotes=COALESCE(?, quotes), replies=COALESCE(?, replies),"
            " impressions=COALESCE(?, impressions), followers=COALESCE(?, followers), author=COALESCE(author, ?),"
            " query_key=COALESCE(query_key, ?), refreshed_at=? WHERE id=?",
            (p.get("likes"), p.get("quotes"), p.get("replies"), p.get("impressions"), p.get("followers"), p.get("author"),
             p.get("query_key"), p["received_at"], p["id"]),
        )
        new = False
    if p.get("likes") is not None or p.get("quotes") is not None:
        conn.execute(
            "INSERT OR REPLACE INTO social_post_metrics(id, ts, likes, quotes, replies, impressions) VALUES(?,?,?,?,?,?)",
            (p["id"], p["received_at"], p.get("likes"), p.get("quotes"), p.get("replies"), p.get("impressions")),
        )
    return new


# ----- the client -----------------------------------------------------------------------------------------
class XClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        budget: Budget,
        xbudget: XBudget,
        cfg: dict[str, Any],
        kols: list[str],
        bearer_token: str,
        stopwords: set[str],
        rules_version: str,
        clock: Callable[[], float] = wall_now,
        request: Callable[..., Any] = request_json,
        shadow: bool = False,
    ):
        self.conn = conn
        self.budget = budget
        self.xb = xbudget
        self.cfg = cfg
        self.kols = list(kols)
        self._token = bearer_token
        self.stopwords = stopwords
        self.rules_version = rules_version
        self.clock = clock
        self._request = request
        self.shadow = bool(shadow)
        self.base = cfg.get("api_base", "https://api.x.com/2").rstrip("/")
        self._throttle = Throttle(3600)
        self.stats: dict[str, int] = {}
        self.last_verdict: XVerdict | None = None

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    # ----- HTTP ---------------------------------------------------------------------------------------------
    def _get(self, kind: str, path: str, params: dict[str, Any], ceiling_usd: float, cost_fn: Callable[[Any], float] | None,
             expected_usd: float | None = None) -> Any | None:
        """Planner -> gate -> GET. Returns the parsed body, or None when deferred / refused / failed (counted, logged,
        a refusal ledgered). `ceiling_usd` is what the call can cost at most (the gate's estimate as well), `expected_usd`
        what it is expected to cost (the envelope's measure; the ceiling when omitted)."""
        now = self.clock()
        v = self.xb.admit(kind, ceiling_usd, expected_usd, now)
        self.last_verdict = v
        if not v.allowed:
            if v.deferred:
                self._bump(f"deferred_{kind}")
            else:
                self._bump(f"blocked_{v.reason}")
                if self._throttle.allow(f"blocked:{kind}:{v.reason}", now):
                    log.warning("x.blocked", kind=kind, reason=v.reason, spend_today=round(v.spend_today, 4), cap=self.xb.cap)
            return None
        url = f"{self.base}{path}?{urllib.parse.urlencode(params, safe=':,')}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            return self._request(self.budget, SOURCE, kind, url, headers=headers, timeout_s=10.0, cost=ceiling_usd, cost_from_response=cost_fn)
        except Blocked as e:
            self._bump("blocked_gate")
            if self._throttle.allow(f"gate:{kind}:{e.reason}", now):
                log.warning("x.gate_blocked", kind=kind, reason=e.reason, retry_in_s=round(e.retry_in_s, 1))
        except HttpError as e:
            self._bump("http_error")
            log.warning("x.http_error", kind=kind, status=e.status, retry_after_s=e.retry_after_s, error=str(e)[:160])
        return None

    def _search(self, kind: str, post_kind: str, query: str, max_results: int, since_id: str | None, start_time: float | None,
                query_key: str | None, with_authors: bool = True, n_accounts: int = 0, extra_ceiling_usd: float = 0.0) -> dict[str, Any] | None:
        """search/recent. Returns {new: stored new posts, posts: every post of the response, newest: newest_id, usd} or
        None when nothing was called. `kind` is the budget kind (mega_search, kol_search, ca_search), `post_kind` what
        the rows carry (mega, kol, ca). Cost = new posts today x price + new authors today x price (with_authors).
        Ceiling = every post and every account new (`extra_ceiling_usd` adds the author lookup of a confirmation search);
        an incremental call (since_id) is paced at the observed average cost of the kind."""
        now = self.clock()
        params: dict[str, Any] = {"query": query, "max_results": max(10, min(100, int(max_results))), "tweet.fields": TWEET_FIELDS}
        if with_authors:
            params["expansions"] = "author_id"
            params["user.fields"] = USER_FIELDS
        if since_id:
            params["since_id"] = since_id
        elif start_time is not None:
            params["start_time"] = iso_utc(start_time)
        ceiling = self.xb.search_ceiling(params["max_results"], n_accounts if with_authors else 0) + float(extra_ceiling_usd)
        expected = self.xb.expected_cost(kind, ceiling, now) if since_id else ceiling
        booked: dict[str, Any] = {}

        def cost_fn(resp: Any) -> float:
            posts = posts_from_response(resp, post_kind, now, query_key)
            new_posts = self.xb.dedupe_new("post", [p["id"] for p in posts], now)
            authors = {p["author_id"] for p in posts if p.get("author_id")} if with_authors else set()
            new_authors = self.xb.dedupe_new("author", sorted(authors), now)
            usd_posts = len(new_posts) * self.xb.price("post_usd")
            usd_authors = len(new_authors) * self.xb.price("author_expansion_usd")
            booked.update(posts=posts, usd=usd_posts + usd_authors, usd_posts=usd_posts, usd_authors=usd_authors, n=len(new_posts), n_authors=len(new_authors))
            return usd_posts + usd_authors

        resp = self._get(kind, "/tweets/search/recent", params, ceiling, cost_fn, expected)
        if resp is None:
            return None
        posts = booked.get("posts", posts_from_response(resp, post_kind, now, query_key))

        def _write(c) -> list[dict[str, Any]]:
            self.xb.book(kind, booked.get("n", len(posts)), booked.get("usd_posts", 0.0), now, unit="post")
            if booked.get("n_authors"):
                self.xb.book(kind, booked["n_authors"], booked.get("usd_authors", 0.0), now, unit="author")
            return [p for p in posts if upsert_post(c, p)]

        new = db.write_tx(self.conn, _write, what="x.search")
        newest = (resp.get("meta") or {}).get("newest_id") if isinstance(resp, dict) else None
        self._bump(f"{kind}_posts", len(new))
        return {"new": new, "posts": posts, "newest": (str(newest) if newest else None), "usd": float(booked.get("usd", 0.0))}

    def _lookup_authors(self, author_ids: list[str], now: float, post_kind: str = "ca") -> tuple[int, float]:
        """`GET /2/users?ids=` for the chosen authors of a confirmation search (at most `search_max_authors`, chosen by
        `pick_authors`); their username and follower count land on their posts. Returns (users looked up, USD)."""
        if not author_ids:
            return 0, 0.0
        unit_price = self.xb.price("author_expansion_usd")
        params = {"ids": ",".join(author_ids), "user.fields": USER_FIELDS}
        booked: dict[str, Any] = {}

        def cost_fn(resp: Any) -> float:
            users = users_from_response(resp)
            new = self.xb.dedupe_new("author", [u["id"] for u in users], now)
            booked.update(users=users, n=len(new), usd=len(new) * unit_price)
            return len(new) * unit_price

        resp = self._get("ca_authors", "/users", params, unit_price * len(author_ids), cost_fn)
        if resp is None:
            return 0, 0.0
        users = booked.get("users", users_from_response(resp))

        def _write(c) -> None:
            self.xb.book("ca_authors", booked.get("n", len(users)), booked.get("usd", 0.0), now, unit="author")
            for u in users:
                c.execute("UPDATE social_posts SET author=COALESCE(author, ?), followers=COALESCE(?, followers) WHERE author_id=? AND kind=?",
                          (u.get("username"), u.get("followers"), u["id"], post_kind))

        db.write_tx(self.conn, _write, what="x.authors")
        self._bump("ca_authors", len(users))
        return len(users), float(booked.get("usd", 0.0))

    # ----- jobs ----------------------------------------------------------------------------------------------
    def mega_watch(self, now: float) -> dict[str, Any]:
        mcfg = self.cfg["mega"]
        since = kv_get(self.conn, "x.mega.since_id")
        res = self._search("mega_search", "mega", mcfg["query"], int(mcfg.get("max_results", 20)), since,
                           now - float(mcfg.get("refresh_max_age_s", 7200)), None, n_accounts=accounts_in_query(str(mcfg.get("query", ""))))
        if res and res["newest"]:
            kv_set(self.conn, "x.mega.since_id", res["newest"], now)
        kv_set(self.conn, "x.mega.last", str(now), now)
        return {"new_posts": len(res["new"]) if res else 0}

    def kol_watch(self, now: float) -> dict[str, Any]:
        if not self.kols:
            return {"new_posts": 0}
        kcfg = self.cfg["kol"]
        since = kv_get(self.conn, "x.kol.since_id")
        res = self._search("kol_search", "kol", kol_query(self.kols), int(kcfg.get("max_results", 20)), since, now - 3600.0, None,
                           n_accounts=len(self.kols))
        if res and res["newest"]:
            kv_set(self.conn, "x.kol.since_id", res["newest"], now)
        kv_set(self.conn, "x.kol.last", str(now), now)
        return {"new_posts": len(res["new"]) if res else 0}

    def refresh_recent(self, now: float) -> dict[str, Any]:
        """Re-read today's mega posts younger than refresh_max_age_s to compute likes/min and mark `fast`."""
        mcfg = self.cfg["mega"]
        max_age = float(mcfg.get("refresh_max_age_s", 7200))
        rows = self.conn.execute(
            "SELECT id, created_at, received_at FROM social_posts WHERE kind='mega' AND COALESCE(created_at, received_at) >= ?"
            " ORDER BY COALESCE(created_at, received_at) DESC LIMIT 100", (now - max_age,),
        ).fetchall()
        kv_set(self.conn, "x.refresh.last", str(now), now)
        if not rows:
            return {"refreshed": 0, "fast": 0}
        ids = [r["id"] for r in rows]
        params = {"ids": ",".join(ids), "tweet.fields": TWEET_FIELDS}
        unit_price = self.xb.price("refresh_post_usd")
        resp = self._get("refresh", "/tweets", params, unit_price * len(ids),
                         lambda r: unit_price * len((r or {}).get("data") or []) if isinstance(r, dict) else 0.0)
        if resp is None:
            return {"refreshed": 0, "fast": 0}
        posts = posts_from_response(resp, "mega", now)

        def _write(c) -> int:
            n = 0
            self.xb.book("refresh", len(posts), unit_price * len(posts), now, unit="post")
            for p in posts:
                upsert_post(c, p)
                if self._update_velocity(p["id"], now, mcfg):
                    n += 1
            return n

        newly_fast = db.write_tx(self.conn, _write, what="x.refresh")
        self._bump("refreshed", len(posts))
        return {"refreshed": len(posts), "fast": newly_fast}

    def _update_velocity(self, post_id: str, now: float, mcfg: dict[str, Any]) -> bool:
        row = self.conn.execute("SELECT created_at, received_at, likes, fast FROM social_posts WHERE id=?", (post_id,)).fetchone()
        if row is None:
            return False
        samples = [(float(r["ts"]), int(r["likes"] or 0), int(r["quotes"] or 0)) for r in self.conn.execute(
            "SELECT ts, likes, quotes FROM social_post_metrics WHERE id=? ORDER BY ts DESC LIMIT 2", (post_id,))]
        lpm, qpm = velocity(samples, row["created_at"])
        age = now - float(row["created_at"] if row["created_at"] is not None else row["received_at"])
        fast = is_fast(int(row["likes"] or 0), lpm, qpm, age, mcfg)
        self.conn.execute("UPDATE social_posts SET likes_per_min=?, quotes_per_min=? WHERE id=?", (lpm, qpm, post_id))
        if fast and not row["fast"]:
            self.conn.execute("UPDATE social_posts SET fast=1, fast_at=? WHERE id=?", (now, post_id))
            log.info("x.fast_post", post_id=post_id, likes_per_min=None if lpm is None else round(lpm, 1), quotes_per_min=None if qpm is None else round(qpm, 2))
            return True
        return False

    # ----- the confirmation planner ------------------------------------------------------------------------------
    def sync_plan(self, now: float) -> dict[str, Any]:
        """Admit, stop and displace keys (one short write); the counters of the sync."""
        return db.write_tx(self.conn, lambda c: planner.sync(c, now, self.cfg, self.stopwords, self.rules_version), what="x.plan")

    def confirmation_keys(self, now: float) -> list[dict[str, Any]]:
        """The keys due for a count right now, in plan order (after syncing the plan): youngest coin observation
        first, themes last. A confirmed key (search pending) is not in it; `due_requests` carries the searches."""
        self.sync_plan(now)
        return [r for r in planner.due_requests(self.conn, now) if r["request"] == planner.REQUEST_COUNT]

    def _confirm_count(self, req: dict[str, Any], now: float, threshold: int, stats: dict[str, Any]) -> bool:
        """One counts/recent call for a planned key; records it against the key and decides the key. False when the
        call did not happen (deferred, refused or failed): the key stays due."""
        params = {"query": req["query"], "granularity": "hour", "start_time": iso_utc(now - 86400.0)}
        unit_price = self.xb.price("counts_request_usd")
        resp = self._get("ca_counts", "/tweets/counts/recent", params, unit_price, lambda r: unit_price)
        if resp is None:
            if self.last_verdict is not None and self.last_verdict.deferred:
                stats["deferred"] += 1
            return False
        n1h, n24 = counts_from_response(resp, now)

        def _write(c) -> str:
            self.xb.book("ca_counts", 1, unit_price, now, unit="request")
            if self.shadow:
                # shadow: the count is kept next to the decision it would have confirmed, never in social_counts
                shadow_mod.write_shadow_confirmation(c, now, req, n1h, n24, threshold, self.rules_version)
            else:
                c.execute("INSERT OR REPLACE INTO social_counts(key, kind, ts, n_last_hour, n_24h, query) VALUES(?,?,?,?,?,?)",
                          (req["key"], req["kind"], now, n1h, n24, req["query"]))
            return planner.record_count(c, req, now, n1h, n24, threshold, unit_price, self.cfg)

        outcome = db.write_tx(self.conn, _write, what="x.counts")
        stats["counts"] += 1
        stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1
        if outcome == "confirmed":
            log.info("x.key_confirmed", key=req["key"][:20], kind=req["kind"], n_last_hour=n1h, counts=req["counts_n"] + 1, shadow=self.shadow)
            self._confirm_search(req, now, stats)  # the one search, right away when the planner admits it
        elif outcome in planner.STOP_REASONS:
            log.info("x.key_stopped", key=req["key"][:20], kind=req["kind"], reason=outcome, counts=req["counts_n"] + 1)
        return True

    def _confirm_search(self, req: dict[str, Any], now: float, stats: dict[str, Any]) -> bool:
        """The single search of a confirmed key: `search_max_results` posts without author expansion, then a user lookup
        of at most `search_max_authors` authors; the key stops as `confirmed`. False when the search did not happen
        (it stays pending; the planner offers it first next tick)."""
        ccfg, pcfg = self.cfg["confirm"], self.cfg.get("planner", {})
        max_authors = max(0, int(pcfg.get("search_max_authors", 3)))
        res = self._search("ca_search", "ca", req["query"], int(ccfg.get("search_max_results", 10)), None, now - 3600.0, req["key"],
                           with_authors=False, extra_ceiling_usd=max_authors * self.xb.price("author_expansion_usd"))
        if res is None:
            if self.last_verdict is not None and self.last_verdict.deferred:
                stats["deferred"] += 1
            return False
        authors_n, usd_authors = 0, 0.0
        if max_authors and res["posts"]:
            chosen = pick_authors(res["posts"], max_authors, lambda a: self.xb.seen_today("author", a, now))
            authors_n, usd_authors = self._lookup_authors(chosen, now)
        db.write_tx(self.conn, lambda c: planner.record_search(c, req, now, authors_n, res["usd"] + usd_authors), what="x.key_search")
        stats["searches"] += 1
        stats["posts"] += len(res["new"])
        stats["authors"] += authors_n
        return True

    def confirm(self, now: float) -> dict[str, Any]:
        """One round of the confirmation planner: sync the plan, then serve what is due in plan order (pending
        searches first, then the due counts). A deferred or refused count ends the round (the cheaper requests behind
        it cost the same); a deferred search does not stop the counts behind it."""
        ccfg = self.cfg["confirm"]
        stats: dict[str, Any] = {"counts": 0, "searches": 0, "posts": 0, "authors": 0, "deferred": 0, "outcomes": {}}
        plan = self.sync_plan(now)
        stats.update(admitted=plan.get("admitted", 0), stopped=plan.get("stopped", {}), active=plan.get("active", 0), waiting=plan.get("waiting", 0))
        threshold = int(ccfg.get("search_when_count_1h", 10))
        for req in planner.due_requests(self.conn, now):
            if req["request"] == planner.REQUEST_SEARCH:
                self._confirm_search(req, now, stats)
                continue
            if not self._confirm_count(req, now, threshold, stats):
                break  # budget or transport: stop this round, keep the keys due
        kv_set(self.conn, "x.confirm.last", str(now), now)
        return stats

    # ----- fast-post matching -----------------------------------------------------------------------------------
    def match_fast_post(self, post: sqlite3.Row | dict[str, Any], now: float) -> dict[str, Any] | None:
        fcfg = self.cfg["fast_post"]
        sw = self.stopwords | {normalize(w) for w in fcfg.get("stopwords", [])}
        rw = rare_words(post["text"], sw, int(fcfg.get("min_word_len", 4)))
        if not rw:
            return None
        t = float(post["created_at"] if post["created_at"] is not None else post["received_at"])
        window = float(fcfg.get("window_s", 1800))
        coins = self.conn.execute(
            "SELECT mint, name, symbol, creator FROM coins WHERE received_at <= ? AND COALESCE(block_time, received_at) BETWEEN ? AND ?",
            (now, t - window, t + window),
        ).fetchall()
        if not coins:
            return None
        matched = []
        for w in rw:
            hits = [c for c in coins if w in set(words(c["name"]) + words(c["symbol"]))]
            creators = {c["creator"] for c in hits if c["creator"]}
            if len(hits) >= int(fcfg.get("min_coins", 3)) and len(creators) >= int(fcfg.get("min_creators", 2)):
                matched.append({"word": w, "n_coins": len(hits), "n_creators": len(creators), "mints": [c["mint"] for c in hits][:20]})
        if not matched:
            return None
        matched.sort(key=lambda m: (m["n_coins"], m["n_creators"]), reverse=True)
        return {"post_id": post["id"], "author": post["author"], "created_at": post["created_at"], "likes": post["likes"],
                "likes_per_min": post["likes_per_min"], "quotes_per_min": post["quotes_per_min"], "text": (post["text"] or "")[:280],
                "matched": matched, "mints": sorted({m for x in matched for m in x["mints"]})}

    def fast_post_decisions(self, now: float) -> int:
        """One `fast_post` decision per fast mega post whose rare words match a burst of coin names. In shadow
        mode the row goes to `shadow_decisions` (would_kind `fast_post`) and `decisions` is never touched."""
        fcfg = self.cfg["fast_post"]
        horizon = float(fcfg.get("window_s", 1800)) + float(self.cfg["mega"].get("refresh_max_age_s", 7200))
        seen = ("SELECT 1 FROM shadow_decisions WHERE would_kind='fast_post' AND term = 'x:' || social_posts.id" if self.shadow
                else "SELECT 1 FROM decisions WHERE kind='fast_post' AND term = 'x:' || social_posts.id")
        rows = self.conn.execute(
            f"SELECT * FROM social_posts WHERE fast=1 AND fast_at >= ? AND NOT EXISTS ({seen})", (now - horizon,),
        ).fetchall()
        n = 0
        for post in rows:
            m = self.match_fast_post(post, now)
            if m is None:
                continue
            reasons = [f"fast_post likes_per_min={m['likes_per_min']}", *(f"{x['word']}: {x['n_coins']} coins / {x['n_creators']} creators" for x in m["matched"][:3])]
            if self.shadow:
                db.write_tx(self.conn, lambda c, post=post, m=m, reasons=reasons: shadow_mod.write_shadow_decision(
                    c, now, now, "fast_post", None, f"x:{post['id']}", m, self.rules_version, "observe", reasons), what="x.shadow_fast_post")
            else:
                db.write_tx(self.conn, lambda c, post=post, m=m, reasons=reasons: decide.write_decision(
                    c, now, now, "fast_post", None, f"x:{post['id']}", m, self.rules_version, "observe", reasons), what="x.fast_post")
            n += 1
            log.info("x.fast_post_decision", post_id=post["id"], words=[x["word"] for x in m["matched"]], mints=len(m["mints"]),
                     shadow=self.shadow)
        return n

    # ----- scheduler ------------------------------------------------------------------------------------------
    def due(self, key: str, interval_s: float, now: float) -> bool:
        last = kv_get(self.conn, key)
        return last is None or now - float(last) >= interval_s

    def tick(self, now: float) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        mcfg, kcfg = self.cfg["mega"], self.cfg["kol"]
        peak = self.xb.is_peak(now)
        if self.due("x.mega.last", interval_for(mcfg, peak, 60.0, 120.0), now):
            summary["mega"] = self.mega_watch(now)
        if self.due("x.refresh.last", float(mcfg.get("refresh_interval_s", 120)), now):
            summary["refresh"] = self.refresh_recent(now)
        if self.due("x.kol.last", interval_for(kcfg, peak, 180.0, 300.0), now):
            summary["kol"] = self.kol_watch(now)
        if self.due("x.confirm.last", 15.0, now):
            summary["confirm"] = self.confirm(now)
        summary["fast_post_decisions"] = self.fast_post_decisions(now)
        summary["spend_today_usd"] = round(self.xb.spend_today(now), 4)
        summary["x_mode"] = "shadow" if self.shadow else "live"
        return summary
