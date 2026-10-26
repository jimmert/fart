import json
import threading

import pytest

from fart import db
from fart.net.budget import Blocked, Budget, load_source_configs


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_budget(settings, conn, clock, overrides=None):
    cfg = json.loads(json.dumps(settings.config("budget")))
    if overrides:
        for src, vals in overrides.items():
            cfg["sources"][src].update(vals)
    srcs = load_source_configs(cfg, {"RPC_DAILY_BUDGET_CU": "1000"})
    return Budget(conn, srcs, settings.lock_path, clock=clock)


def test_rate_gate_blocks_with_reason_and_refills(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"dexscreener": {"rate_per_s": 1.0, "burst": 2}})
    assert b.acquire("dexscreener", "tokens").allowed
    assert b.acquire("dexscreener", "tokens").allowed
    v = b.acquire("dexscreener", "tokens")
    assert not v.allowed and v.reason == "rate_limit"
    assert 0 < v.retry_in_s <= 1.0
    # the blocked call is ledgered, never silent
    row = conn.execute("SELECT blocked_reason FROM api_ledger WHERE blocked_reason IS NOT NULL").fetchone()
    assert row["blocked_reason"] == "rate_limit"
    assert conn.execute("SELECT blocked_count FROM health WHERE source='dexscreener'").fetchone()[0] == 1
    clock.advance(1.0)
    assert b.acquire("dexscreener", "tokens").allowed


def test_daily_cost_cap_blocks_with_reason(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"rpc": {"rate_per_s": 1000, "burst": 1000}})
    # RPC_DAILY_BUDGET_CU=1000; getTransaction costs 40 (Alchemy, milestone 4) -> 25 calls fit, the 26th exceeds 1000
    n = 0
    while True:
        v = b.acquire("rpc", "getTransaction")
        if not v.allowed:
            break
        b.record("rpc", "getTransaction", v.cost, True, 200, 50.0)
        n += 1
        assert n < 100
    assert n == 25
    assert v.reason == "daily_cost_cap"
    usage = b.daily_usage("rpc")
    assert usage["cost"] == 25 * 40
    assert usage["blocked"] == 1
    with pytest.raises(Blocked) as ei:
        b.acquire_or_raise("rpc", "getTransaction")
    assert ei.value.reason == "daily_cost_cap"
    # next UTC day: allowed again
    clock.advance(86400.0)
    assert b.acquire("rpc", "getTransaction").allowed


def test_daily_requests_cap(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"geckoterminal": {"rate_per_s": 1000, "burst": 1000, "daily_requests": 2}})
    for _ in range(2):
        v = b.acquire("geckoterminal", "tokens")
        assert v.allowed
        b.record("geckoterminal", "tokens", v.cost, True, 200, 10.0)
    v = b.acquire("geckoterminal", "tokens")
    assert not v.allowed and v.reason == "daily_requests_cap"


def test_bucket_persists_across_restarts(settings, conn):
    clock = Clock()
    b1 = make_budget(settings, conn, clock, {"pumpportal": {"rate_per_s": 0.0, "burst": 3}})
    assert b1.acquire("pumpportal", "connect").allowed
    assert b1.acquire("pumpportal", "connect").allowed
    # "restart": a brand-new Budget on a fresh connection to the same database
    conn2 = db.connect(settings.db_path)
    b2 = make_budget(settings, conn2, clock, {"pumpportal": {"rate_per_s": 0.0, "burst": 3}})
    assert b2.acquire("pumpportal", "connect").allowed
    v = b2.acquire("pumpportal", "connect")
    assert not v.allowed and v.reason == "rate_limit"
    conn2.close()


def test_backoff_on_429_honors_retry_after(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"dexscreener": {"rate_per_s": 1000, "burst": 1000, "backoff_base_s": 10}})
    v = b.acquire("dexscreener", "tokens")
    b.record("dexscreener", "tokens", v.cost, False, 429, 5.0, err="Too Many Requests", retry_after_s=45)
    v2 = b.acquire("dexscreener", "tokens")
    assert not v2.allowed and v2.reason == "backoff"
    assert 44 <= v2.retry_in_s <= 45
    clock.advance(46)
    v3 = b.acquire("dexscreener", "tokens")
    assert v3.allowed
    # a success clears the backoff state
    b.record("dexscreener", "tokens", v3.cost, True, 200, 5.0)
    h = conn.execute("SELECT consecutive_err, backoff_until, ok_count, err_count FROM health WHERE source='dexscreener'").fetchone()
    assert h["consecutive_err"] == 0 and h["backoff_until"] is None
    assert h["ok_count"] == 1 and h["err_count"] == 1


def test_backoff_grows_exponentially_on_5xx(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"rpc": {"rate_per_s": 1000, "burst": 1000, "backoff_base_s": 2, "backoff_max_s": 300}})
    waits = []
    for _ in range(4):
        v = b.acquire("rpc", "getSlot")
        assert v.allowed
        b.record("rpc", "getSlot", v.cost, False, 503, 5.0, err="unavailable")
        blocked = b.acquire("rpc", "getSlot")
        assert blocked.reason == "backoff"
        waits.append(round(blocked.retry_in_s))
        clock.advance(blocked.retry_in_s + 0.01)
    assert waits == [2, 4, 8, 16]


def test_4xx_other_than_429_does_not_backoff(settings, conn):
    clock = Clock()
    b = make_budget(settings, conn, clock, {"dexscreener": {"rate_per_s": 1000, "burst": 1000}})
    v = b.acquire("dexscreener", "tokens")
    b.record("dexscreener", "tokens", v.cost, False, 404, 5.0, err="not found")
    assert b.acquire("dexscreener", "tokens").allowed


def test_unknown_source_is_an_error(settings, conn):
    b = make_budget(settings, conn, Clock())
    with pytest.raises(KeyError):
        b.acquire("nope", "x")


def test_snapshot_lists_every_source(settings, conn):
    b = make_budget(settings, conn, Clock())
    snap = b.snapshot()
    assert set(snap) == set(settings.config("budget")["sources"])
    assert snap["rpc"]["usage_today"]["daily_cost_cap"] == 1000.0


def test_concurrent_acquires_never_exceed_burst(settings):
    """Two connections (as two processes would) racing on the same bucket."""
    clock = Clock()
    results = []

    def worker():
        c = db.connect(settings.db_path)
        b = make_budget(settings, c, clock, {"geckoterminal": {"rate_per_s": 0.0, "burst": 5}})
        for _ in range(10):
            results.append(b.acquire("geckoterminal", "tokens").allowed)
        c.close()

    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(results) == 5
