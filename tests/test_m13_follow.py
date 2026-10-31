"""Milestone 13, the measurement side: schema v10, the BondingCurve decoder on a real account next to the last TradeEvent of the
same mint (tests/fixtures/bonding_curve_accounts.json, read from the public RPC in the sandbox on 31 Oct 2026), the RPC helper
(getMultipleAccounts <= 100, booked per call), the follow step with a fake RPC (triggers, rows, batching, intervals, the daily
share, the degradation, the bound on followed coins, missing and undecodable accounts, errors counted), the harness on a synthetic
world in which the stream lets a coin go after three minutes while the polls go on (outcome known, within 60 s of the world with
full trades; the trade-only figures unchanged), the tracker inside its gate and time budget, retention, and the status lines."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from fart import db
from fart.chain import bonding_curve as bc
from fart.chain.base58 import b58encode
from fart.chain.curve import CurveParams, CurveState
from fart.chain.pda import bonding_curve_address
from fart.config import load_settings
from fart.measure import outcomes
from fart.measure.paper import PricePoint, Position
from fart.net.budget import Blocked, Budget
from fart.plays import follow as fm, service as plays_service
from fart.research import harness as rh, hypotheses as hyps, report as rr
from fart.util import day_bounds

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
K = 30_000_000_000 * 1_073_000_000_000_000
NOW = 1_793_500_000.0           # 2026-11-01T02:26:40Z
DAYS = ["2026-10-30", "2026-10-31"]
COIN_SQL = "INSERT INTO coins(mint, name, symbol, creator, slot, block_time, received_at, dev_buy_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,'pumpportal','[]')"
TRADE_SQL = ("INSERT INTO trades(mint, slot, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
             " VALUES(?,?,?,?,?,?,?,?,?,?,?,'rpc_trades')")


def mint_of(tag: str) -> str:
    """A valid 32-byte pubkey (base58) for a test coin: the PDA derivation needs one."""
    return b58encode(hashlib.sha256(tag.encode()).digest())


def fake_account(vsol: int, complete: bool = False, quote_mint: bytes = bytes(32), length: int | None = None) -> dict:
    """A BondingCurve account in the IDL layout (166 bytes, like the real ones) as `getMultipleAccounts` returns it."""
    vt = K // vsol if vsol else 0
    rsol = max(0, vsol - 30_000_000_000)
    data = (bc.ACCOUNT_DISCRIMINATOR + struct.pack("<QQQQQ", vt, vsol, 700_000_000_000_000, rsol, 1_000_000_000_000_000) + bytes([1 if complete else 0])
            + bytes(32) + bytes([0, 0]) + quote_mint + struct.pack("<Q", 0) + bytes([0, 0]) + struct.pack("<QQ", 0, 0) + bytes([0]) + struct.pack("<QQQ", 30_000_000_000, 0, 0))
    if length is not None:
        data = data[:length]
    return {"data": [base64.b64encode(data).decode(), "base64"], "owner": PROGRAM, "lamports": 1_729_054_994, "space": len(data)}


def keyed_settings(root, extra: dict | None = None):
    env = {"FART_RULES_VERSION": "test-rules-v1", "RPC_HTTP_URL": "https://keyed.example/v2/secret", "RPC_WSS_URL": "wss://keyed.example/v2/secret", **(extra or {})}
    s = load_settings(root=root, env=env)
    s.ensure_dirs()
    return s


class FakeRpc:
    """`get_multiple_accounts_base64` through the real budget gate (one ledger row per call) with programmable answers."""

    def __init__(self, budget: Budget, source: str = "rpc"):
        self.budget = budget
        self.source = source
        self.calls: list[list[str]] = []
        self.accounts: dict[str, dict | None] = {}
        self.default_vsol = 31_000_000_000
        self.fail: Exception | None = None

    def get_multiple_accounts_base64(self, addresses):
        if len(addresses) > 100:
            raise ValueError("getMultipleAccounts takes at most 100 addresses per call")
        self.budget.acquire_or_raise(self.source, fm.METHOD)
        if self.fail is not None:
            self.budget.record(self.source, fm.METHOD, 20.0, False, 500, 10.0, err="boom")
            raise self.fail
        self.budget.record(self.source, fm.METHOD, 20.0, True, 200, 40.0)
        self.calls.append(list(addresses))
        return 463_659_000 + len(self.calls), [self.accounts.get(a, fake_account(self.default_vsol)) for a in addresses]


# ----- schema -------------------------------------------------------------------------------------------------------------------
def test_schema_v10_adds_followed_and_path_points_idempotently(settings, conn):
    assert db.SCHEMA_VERSION == 10 and conn.execute("PRAGMA user_version").fetchone()[0] == 10
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"followed", "path_points", "plays", "creator_stats"} <= tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(path_points)")}
    assert cols == {"mint", "t", "source", "virtual_sol", "virtual_token", "real_sol", "real_token", "complete", "slot"}
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(followed)")}
    assert {"mint", "since", "reason", "until", "state", "curve", "next_poll_at", "interval_s", "polls", "errors", "misses", "changed_at", "last_source"} <= fcols
    again = db.connect(settings.db_path)
    assert again.execute("PRAGMA user_version").fetchone()[0] == 10
    again.close()
    conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES('M', 1.0, 'poll', 31, 7, 1, 7, 0, 5)")
    conn.execute("INSERT OR REPLACE INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES('M', 1.0, 'poll', 32, 7, 2, 7, 0, 6)")
    assert conn.execute("SELECT COUNT(*), MAX(virtual_sol) FROM path_points").fetchone()[:2] == (1, 32)   # one point per mint, time and source
    from fart import version

    assert "fart/plays" not in " ".join(version.RULE_FILES) and "fart/chain/bonding_curve.py" not in " ".join(version.RULE_FILES)


# ----- the decoder on a real account -------------------------------------------------------------------------------------------------
def test_bonding_curve_decoder_matches_the_last_trade_event_and_the_idl():
    fx = json.loads((FIXTURES / "bonding_curve_accounts.json").read_text(encoding="utf-8"))
    assert bc.ACCOUNT_DISCRIMINATOR == bytes([23, 183, 248, 55, 96, 216, 172, 96])      # the IDL's own discriminator list for `BondingCurve`
    assert [name for name, _ in bc.LAYOUT[:6]] == ["virtual_token_reserves", "virtual_quote_reserves", "real_token_reserves", "real_quote_reserves", "token_total_supply", "complete"]
    live = next(c for c in fx["cases"] if c["kind"] == "live")
    raw = base64.b64decode(live["account"]["data_b64"])
    a = bc.decode_bonding_curve(raw)
    t = live["trade"]["fields"]
    assert a.ok and a.discriminator_ok and a.error is None and a.suspect == [] and a.consumed == len(raw) == 166 and a.extra_bytes == 0
    assert a.virtual_sol == t["virtual_sol_reserves"] and a.virtual_token == t["virtual_token_reserves"]
    assert a.real_sol == t["real_sol_reserves"] and a.real_token == t["real_token_reserves"]
    assert a.complete is False and a.quote_is_sol and a.fields["token_total_supply"] == 1_000_000_000_000_000 and a.fields["creator"] == t["creator"]
    assert a.fields["quote_mint"] == bc.ZERO_PUBKEY and live["account"]["owner"] == PROGRAM and live["account"]["slot"] >= live["trade"]["slot"]
    assert bonding_curve_address(live["mint"], PROGRAM) == live["bonding_curve"]
    params = CurveParams.from_config(json.loads((Path(__file__).resolve().parent.parent / "config" / "pumpfun.json").read_text(encoding="utf-8")))
    st = a.state()
    assert st is not None and st.price_sol(params) == pytest.approx(CurveState(t["virtual_sol_reserves"], t["virtual_token_reserves"]).price_sol(params))
    assert st.real_sol == t["real_sol_reserves"] and 0.0 < st.real_sol_estimate(params) / 1e9 < 85.0
    done = next(c for c in fx["cases"] if c["kind"] == "completed")
    g = bc.decode_bonding_curve(base64.b64decode(done["account"]["data_b64"]))
    assert g.ok and g.complete is True and g.virtual_sol == 0 and g.virtual_token == 0 and g.real_sol == 0 and g.real_token == 0 and g.state() is None
    assert bonding_curve_address(done["mint"], PROGRAM) == done["bonding_curve"] == done["migration"]["fields"]["bonding_curve"]
    # tolerant decoding: short, wrong discriminator, truncated at the required fields, another quote mint, the other wire shapes
    assert bc.decode_bonding_curve(b"\x00" * 5).error == "too_short"
    bad = bc.decode_bonding_curve(b"\x01" * 60)
    assert bad.error.startswith("wrong discriminator") and not bad.ok
    short = bc.decode_bonding_curve(raw[:40])
    assert short.error.startswith("truncated") and not short.ok
    old_layout = bc.decode_bonding_curve(raw[:49])
    assert old_layout.ok and old_layout.quote_is_sol and old_layout.fields.get("quote_mint") is None and old_layout.virtual_sol == a.virtual_sol
    other = bc.decode_account_value(fake_account(31_000_000_000, quote_mint=hashlib.sha256(b"usdc").digest()))[0]
    assert other.ok and other.quote_is_sol is False and other.virtual_sol is None and other.state() is None
    assert bc.decode_account_value(None) == (None, {"error": "no account"})
    acc, info = bc.decode_account_value({"data": ["not base64!!", "base64"], "owner": PROGRAM, "lamports": 1})
    assert acc is None and "bad base64" in info["error"]
    acc, info = bc.decode_account_value({"data": [live["account"]["data_b64"], "base64"], "owner": PROGRAM, "lamports": 5})
    assert acc.ok and info["owner"] == PROGRAM and acc.to_json()["fields"]["virtual_quote_reserves"] == t["virtual_sol_reserves"]
    with_sol = bc.decode_account_value(fake_account(40_000_000_000, quote_mint=__import__("fart.chain.base58", fromlist=["b58decode"]).b58decode(bc.WSOL_MINT)))[0]
    assert with_sol.quote_is_sol and with_sol.virtual_sol == 40_000_000_000


def test_rpc_get_multiple_accounts_books_one_call_at_the_table_cost(tmp_path, monkeypatch):
    from fart.chain import rpc as rpc_mod

    s = keyed_settings(tmp_path)
    conn = db.connect(s.db_path)
    budget = Budget.from_settings(conn, s, clock=lambda: NOW)
    seen = {}

    def fake_request(budget_, source, method, url, body=None, timeout_s=10.0, **kw):
        seen.update({"source": source, "method": method, "url": url, "body": body})
        v = budget_.acquire_or_raise(source, method)
        budget_.record(source, method, v.cost, True, 200, 12.0)
        return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 463_659_389}, "value": [fake_account(31_000_000_000), None]}}

    monkeypatch.setattr(rpc_mod, "request_json", fake_request)
    client = rpc_mod.RpcClient(budget, s.rpc_http_url, s.rpc_is_public)
    slot, values = client.get_multiple_accounts_base64(["A" * 44, "B" * 44])
    assert slot == 463_659_389 and len(values) == 2 and values[1] is None and values[0]["owner"] == PROGRAM
    assert seen["source"] == "rpc" and seen["method"] == "getMultipleAccounts" and seen["body"]["params"] == [["A" * 44, "B" * 44], {"encoding": "base64", "commitment": "confirmed"}]
    row = conn.execute("SELECT method, cost, ok FROM api_ledger").fetchone()
    assert (row["method"], row["cost"], row["ok"]) == ("getMultipleAccounts", 20.0, 1)       # the Alchemy page of 31 Oct 2026: 20 CU per call, flat
    assert s.config("budget")["sources"]["rpc"]["cost_table"]["getMultipleAccounts"] == 20
    with pytest.raises(ValueError):
        client.get_multiple_accounts_base64([f"X{i}" for i in range(101)])
    assert client.get_multiple_accounts_base64([]) == (None, [])
    assert s.config("budget")["sources"]["dexscreener"]["rate_per_s"] == pytest.approx(3.3333) and s.config("budget")["sources"]["dexscreener"]["burst"] == 10   # 200 per minute
    conn.close()


# ----- the follow step -------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path):
    s = keyed_settings(tmp_path)
    conn = db.connect(s.db_path)
    clock = [NOW]
    budget = Budget.from_settings(conn, s, clock=lambda: clock[0])
    rpc = FakeRpc(budget)
    f = fm.Follower(conn, s, budget=budget, rpc=rpc, clock=lambda: clock[0])
    yield {"settings": s, "conn": conn, "clock": clock, "budget": budget, "rpc": rpc, "follower": f}
    conn.close()


def _coin(conn, mint, t, creator="Cr", dev=0.3):
    conn.execute(COIN_SQL, (mint, f"Coin {mint[:4]}", mint[:3], creator, 7, t, t + 0.5, dev))


def test_follow_rows_triggers_reasons_and_lifecycle(world):
    conn, f, clock = world["conn"], world["follower"], world["clock"]
    assert f.polls_enabled and f.disabled_reason is None and f.cfg.max_followed == 1500 and f.cfg.daily_cu_share == 0.10 and f.cfg.degraded_intervals_s == (60.0, 120.0, 300.0)
    assert f.share_cu == 300_000.0                                   # 0.10 of the 3,000,000 CU cap
    mints = [mint_of(f"coin{i}") for i in range(4)]
    for m in mints:
        _coin(conn, m, NOW - 100)
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (mints[2], NOW - 50, NOW - 49, "rpc_logs", "[]", "Pool"))
    conn.commit()
    res = f.follow_many([(mints[0], NOW - 60, "early_buyers"), (mints[1], NOW - 30, "curve60"), (mints[2], NOW - 70, "observe"), ("not-a-pubkey!", NOW, "early_buyers"),
                         (mints[3], NOW - 90_000, "early_buyers")], NOW)
    assert res == {"new": 5, "updated": 0}
    rows = {r["mint"]: dict(r) for r in conn.execute("SELECT * FROM followed")}
    assert rows[mints[0]]["state"] == "active" and rows[mints[0]]["since"] == NOW - 60 and rows[mints[0]]["until"] == NOW - 60 + 86400 and rows[mints[0]]["curve"] == bonding_curve_address(mints[0], PROGRAM)
    assert rows[mints[0]]["next_poll_at"] == NOW and rows[mints[0]]["interval_s"] == 60.0 and rows[mints[0]]["reason"] == "early_buyers"
    assert rows[mints[2]]["state"] == "graduated" and rows[mints[2]]["graduated_at"] == NOW - 50          # the migration is known: no poll
    assert rows["not-a-pubkey!"]["state"] == "closed" and rows["not-a-pubkey!"]["closed_reason"] == "bad_mint"
    assert rows[mints[3]]["state"] == "done"                                                                 # the 24 hours were over at the signal
    assert float(db.kv_get(conn, fm.KV_STARTED)) == NOW
    # a second trigger on a followed coin appends its reason once and changes nothing else
    res = f.follow_many([(mints[0], NOW + 5, "curve60"), (mints[0], NOW + 6, "curve60"), (mints[0], NOW + 7, "play:H2-v1")], NOW + 10)
    assert res == {"new": 0, "updated": 2}
    r0 = conn.execute("SELECT reason, since, until FROM followed WHERE mint=?", (mints[0],)).fetchone()
    assert r0["reason"] == "early_buyers,curve60,play:H2-v1" and r0["since"] == NOW - 60
    # observe decisions: the cursor starts at the first call (nothing with hindsight), then every new decision follows
    assert f.add_observe_decisions(NOW) == 0 and float(db.kv_get(conn, fm.KV_DECISIONS_CURSOR)) == NOW
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (NOW + 20, NOW + 21, "observe", mints[1], None, "{}", "test-rules-v1", "observe", "[]"))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (NOW + 22, NOW + 23, "observe", mint_of("fresh"), None, "{}", "test-rules-v1", "observe", "[]"))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (NOW + 24, NOW + 25, "observe_meta", None, "pepe", "{}", "test-rules-v1", "observe", "[]"))
    conn.commit()
    assert f.add_observe_decisions(NOW + 30) == 1 and float(db.kv_get(conn, fm.KV_DECISIONS_CURSOR)) == NOW + 23
    assert conn.execute("SELECT reason FROM followed WHERE mint=?", (mints[1],)).fetchone()[0] == "curve60,observe"
    assert conn.execute("SELECT state, reason FROM followed WHERE mint=?", (mint_of("fresh"),)).fetchone()[:2] == ("active", "observe")   # a coin without a coins row: the PDA is derived
    # open plays are followed for their 24 hours
    cols = "rule, version, hypothesis, variant, mint, as_of, created_at, features_json, entry_json, size_sol, status, rules_version, json"
    conn.execute(f"INSERT INTO plays({cols}) VALUES('H2-v1', 1, 'H2', 'm5_ts5', ?, ?, ?, '{{}}', '{{}}', 0.5, 'open', 'r', '{{}}')", (mint_of("played"), NOW + 40, NOW + 40))
    conn.commit()
    assert f.add_plays(NOW + 45) == 1 and conn.execute("SELECT reason FROM followed WHERE mint=?", (mint_of("played"),)).fetchone()[0] == "play:H2-v1"
    # the lifecycle: done after 24 h, graduated once a migration lands
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (mints[1], NOW + 100, NOW + 101, "rpc_logs", "[]", "Pool2"))
    conn.commit()
    clock[0] = NOW - 60 + 86400 + 1
    assert f.refresh(clock[0]) == {"done": 1, "graduated": 1}
    st = {r["mint"]: r["state"] for r in conn.execute("SELECT mint, state FROM followed")}
    assert st[mints[0]] == "done" and st[mints[1]] == "graduated" and conn.execute("SELECT graduated_at FROM followed WHERE mint=?", (mints[1],)).fetchone()[0] == NOW + 100
    labels = hyps.FOLLOW_REASONS_NL
    assert set(labels) == {"early_buyers", "theme", "curve60", "play", "observe"} and hyps.follow_trigger("curve60").hypotheses == ("H2",) and hyps.follow_trigger("nope") is None


def test_follow_polls_batch_decode_schedule_and_degrade(world):
    conn, f, clock, rpc, budget = world["conn"], world["follower"], world["clock"], world["rpc"], world["budget"]
    mints = [mint_of(f"poll{i}") for i in range(230)]
    for i, m in enumerate(mints):
        _coin(conn, m, NOW - 3600 - i)
    conn.commit()
    f.follow_many([(m, NOW - 30 - i, "early_buyers") for i, m in enumerate(mints)], NOW)
    curves = {m: bonding_curve_address(m, PROGRAM) for m in mints}
    rpc.accounts[curves[mints[0]]] = fake_account(45_000_000_000)                    # 15 SOL in the curve
    rpc.accounts[curves[mints[1]]] = fake_account(0, complete=True)                  # graduated: every reserve zero
    rpc.accounts[curves[mints[2]]] = None                                            # no account
    rpc.accounts[curves[mints[3]]] = fake_account(31_000_000_000, length=40)         # truncated: undecodable
    rpc.accounts[curves[mints[4]]] = fake_account(31_000_000_000, quote_mint=hashlib.sha256(b"usdc").digest())   # not priced in SOL
    st = f.poll(NOW)
    assert st["enabled"] and st["active"] == 230 and st["due"] == 230 and st["calls"] == 3 and [len(c) for c in rpc.calls] == [100, 100, 30]    # batches of at most 100
    assert st["polled"] == 230 and st["points"] == 230 - 3 and st["graduated"] == 1 and st["misses"] == 1 and st["errors"] == 2 and st["blocked"] == 0 and st["level"] == 0
    assert st["cu_today"] == 60.0 and st["share_cu"] == 300_000.0 and st["paused"] is False
    pts = {r["mint"]: dict(r) for r in conn.execute("SELECT * FROM path_points")}
    p0 = pts[mints[0]]
    assert p0["t"] == NOW and p0["source"] == "poll" and p0["virtual_sol"] == 45_000_000_000 and p0["virtual_token"] == K // 45_000_000_000 and p0["real_sol"] == 15_000_000_000 and p0["complete"] == 0 and p0["slot"] in (463_659_001, 463_659_002, 463_659_003)
    assert pts[mints[1]]["complete"] == 1 and pts[mints[1]]["virtual_sol"] == 0 and mints[2] not in pts and mints[3] not in pts and mints[4] not in pts
    rows = {r["mint"]: dict(r) for r in conn.execute("SELECT * FROM followed")}
    assert rows[mints[0]]["state"] == "active" and rows[mints[0]]["next_poll_at"] == NOW + 60 and rows[mints[0]]["interval_s"] == 60.0 and rows[mints[0]]["polls"] == 1
    assert rows[mints[0]]["changed_at"] == NOW and rows[mints[0]]["last_source"] == "poll" and rows[mints[0]]["last_point_t"] == NOW
    assert rows[mints[1]]["state"] == "graduated" and rows[mints[1]]["graduated_at"] == NOW and rows[mints[1]]["next_poll_at"] is None
    assert rows[mints[2]]["misses"] == 1 and rows[mints[2]]["state"] == "active" and rows[mints[3]]["errors"] == 1 and rows[mints[4]]["errors"] == 1
    ledger = conn.execute("SELECT method, COUNT(*), SUM(cost) FROM api_ledger WHERE blocked_reason IS NULL GROUP BY method").fetchone()
    assert tuple(ledger) == ("getMultipleAccounts", 3, 60.0)                   # one ledger row per call, 20 CU each
    assert int(db.kv_get(conn, f"{fm.KV_POLLS}.2026-11-01")) == 230 and int(db.kv_get(conn, f"{fm.KV_CALLS}.2026-11-01")) == 3 and int(db.kv_get(conn, f"{fm.KV_ERRORS}.2026-11-01")) == 2
    # nothing is due before the interval; after it the same coins are polled again, the unchanged reserves keep changed_at
    clock[0] = NOW + 30
    assert f.poll(clock[0])["calls"] == 0
    clock[0] = NOW + 61
    st = f.poll(clock[0])
    assert st["due"] == 229 and st["calls"] == 3 and st["points"] == 229 - 3        # the graduated coin is not polled any more; the three unusable answers return
    r0 = conn.execute("SELECT changed_at, polls FROM followed WHERE mint=?", (mints[0],)).fetchone()
    assert r0["changed_at"] == NOW and r0["polls"] == 2
    # a missing account closes the row after three polls; an undecodable one too; both are counted and never silently dropped
    clock[0] = NOW + 122
    f.poll(clock[0])
    rows = {r["mint"]: dict(r) for r in conn.execute("SELECT * FROM followed")}
    assert rows[mints[2]]["state"] == "closed" and rows[mints[2]]["closed_reason"] == "account_missing" and rows[mints[2]]["misses"] == 3
    assert rows[mints[3]]["state"] == "closed" and rows[mints[3]]["closed_reason"].startswith("undecodable") and rows[mints[4]]["state"] == "closed"
    # intervals: two hours after the signal the slow interval; unchanged reserves for two hours too; an open stream subscription means a watch poll only
    row = dict(rows[mints[0]])
    assert f.interval_for(row, NOW + 1000, False) == 60.0 and f.interval_for(row, NOW + 7300, False) == 300.0
    assert f.interval_for({**row, "changed_at": NOW - 7200}, NOW + 1000, False) == 300.0 and f.interval_for(row, NOW + 1000, True) == 300.0
    # degradation: the last hour above the hourly envelope (12,500 CU) lengthens the fast interval 60 -> 120 -> 300 and recovers below half of it
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, status) VALUES(?,?,?,?,1,200)", (clock[0] - 100, "rpc", fm.METHOD, 13_000.0))
    conn.commit()
    clock[0] = NOW + 400
    assert f.update_level(clock[0]) == 1 and f.interval_for(row, clock[0], False) == 120.0 and int(db.kv_get(conn, fm.KV_LEVEL)) == 1
    assert f.update_level(clock[0] + 10) == 1                                  # at most one step per five minutes
    assert f.update_level(clock[0] + 301) == 2 and f.interval_for(row, clock[0] + 301, False) == 300.0 and f.update_level(clock[0] + 700) == 2    # the longest interval is the floor
    conn.execute("DELETE FROM api_ledger")
    conn.commit()
    assert f.update_level(clock[0] + 800) == 2                                  # recovery waits fifteen minutes after the last change
    assert f.update_level(clock[0] + 301 + 901) == 1 and f.update_level(clock[0] + 301 + 2 * 901) == 0
    f.level, f.level_changed_at = 0, 0.0
    # the gate refuses (a rate limit): counted as blocked, a level up, nothing silently skipped
    budget.sources["rpc"] = budget.sources["rpc"].__class__(**{**budget.sources["rpc"].__dict__, "rate_per_s": 0.0001, "burst": 1.0})
    conn.execute("UPDATE followed SET next_poll_at=? WHERE state='active'", (NOW + 2000,))
    conn.commit()
    clock[0] = NOW + 2001
    st = f.poll(clock[0])
    assert st["calls"] == 1 and st["blocked"] == 1 and f.level == 1 and conn.execute("SELECT COUNT(*) FROM api_ledger WHERE blocked_reason='rate_limit'").fetchone()[0] == 1
    budget.sources["rpc"] = budget.sources["rpc"].__class__(**{**budget.sources["rpc"].__dict__, "rate_per_s": 8.0, "burst": 16.0})
    # the daily share: spent means paused until the next UTC day, booked as blocked with its own reason, the interval already at its longest
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, status) VALUES(?,?,?,?,1,200)", (clock[0], "rpc", fm.METHOD, 299_990.0))
    conn.execute("UPDATE followed SET next_poll_at=? WHERE state='active'", (NOW + 3000,))
    conn.commit()
    clock[0] = NOW + 3001
    st = f.poll(clock[0])
    assert st["paused"] is True and st["calls"] == 0 and st["blocked"] == 1 and st["cu_today"] >= 300_000.0
    assert conn.execute("SELECT COUNT(*) FROM api_ledger WHERE blocked_reason=?", (fm.BLOCK_REASON_SHARE,)).fetchone()[0] == 1
    # a transport error: counted on the rows and in the day's counter, the tick ends, the next tick tries again
    conn.execute("DELETE FROM api_ledger")
    conn.execute("UPDATE followed SET next_poll_at=? WHERE state='active'", (NOW + 4000,))
    conn.commit()
    from fart.net.http import HttpError

    rpc.fail = HttpError(502, "bad gateway")
    clock[0] = NOW + 4001
    st = f.poll(clock[0])
    assert st["errors"] == 1 and st["calls"] == 0 and conn.execute("SELECT COUNT(*) FROM followed WHERE state='active' AND errors >= 1").fetchone()[0] == 100   # the batch of the failed call
    rpc.fail = None
    # the bound on followed coins: the newest signals are polled, the rest waits and is counted
    f2 = fm.Follower(conn, world["settings"], budget=budget, rpc=rpc, clock=lambda: clock[0], cfg=fm.FollowConfig(max_followed=50, daily_cu_share=0.10))
    conn.execute("UPDATE followed SET next_poll_at=? WHERE state='active'", (NOW + 5000,))
    conn.commit()
    clock[0] = NOW + 5001
    n_before = len(rpc.calls)
    st = f2.poll(clock[0])
    assert st["due"] == 50 and st["over_cap"] == st["active"] - 50 and st["calls"] == 1 and len(rpc.calls[n_before]) == 50
    polled = {conn.execute("SELECT mint FROM followed WHERE curve=?", (c,)).fetchone()[0] for c in rpc.calls[n_before]}
    newest = {r[0] for r in conn.execute("SELECT mint FROM followed WHERE state='active' ORDER BY since DESC LIMIT 50")}
    assert polled == newest                                                   # the newest fifty signals; the rest waits
    # status: the counts, the share, the level, the source of the last point, the Dutch-free English status line of `fart status`
    s = fm.status(conn, world["settings"], clock[0])
    assert s["exists"] and s["active"] == 230 - 4 and s["by_state"]["graduated"] == 1 and s["by_state"]["closed"] == 3 and s["share_cu"] == 300_000.0 and s["level"] in (0, 1)
    assert s["last_point"]["source"] == "poll" and s["points_total"] == conn.execute("SELECT COUNT(*) FROM path_points").fetchone()[0] and s["reasons"] == {"early_buyers": 227}
    line = fm.status_line(s)
    assert line.startswith("follow: 230 followed (active 226, graduated 1, done 0, closed 3; early_buyers 227)") and "polls today" in line and "of the 300,000 share" in line and "last point via poll" in line
    assert fm.status_line({"exists": False}).startswith("follow: no followed table yet")
    assert fm.status_line({**s, "disabled_reason": "the polls need the keyed RPC"}).endswith("polls DISABLED: the polls need the keyed RPC; last point via poll 0 s ago")


def test_follow_needs_the_keyed_rpc_and_the_plays_daemon_follows_its_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("FART_ROOT", str(tmp_path))
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("RPC_HTTP_URL", "RPC_WSS_URL"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(root=tmp_path, env={"FART_RULES_VERSION": "test-rules-v1"})
    s.ensure_dirs()
    assert s.rpc_is_public
    conn = db.connect(s.db_path)
    clock = [NOW]
    # with the public fallback the rows are kept but no poll goes out: the reason is in the status
    public_eng = plays_service.PlaysEngine(conn, s, clock=lambda: clock[0])
    assert public_eng.follow_enabled and not public_eng.follower.polls_enabled and "keyed RPC" in public_eng.follower.disabled_reason
    t0 = public_eng.tick()
    assert t0["rules"] == 0 and t0["candidates"] == 0 and t0["follow_new"] == 0 and t0["polls"]["enabled"] is False and "keyed RPC" in t0["polls"]["reason"]
    # a keyed world with a fake RPC: the daemon follows what the triggers say, with or without an active rule
    s2 = keyed_settings(tmp_path / "keyed")
    conn2 = db.connect(s2.db_path)
    budget = Budget.from_settings(conn2, s2, clock=lambda: clock[0])
    rpc = FakeRpc(budget)
    eng = plays_service.PlaysEngine(conn2, s2, clock=lambda: clock[0], rpc=rpc)
    assert eng.rules == [] and eng.follower.polls_enabled and eng.families() == {"H1", "H4", "H5", "H7", "H3", "H2"}
    early, curve, quiet = mint_of("early"), mint_of("curve60"), mint_of("quiet")
    for m, age, reason in ((early, 90, "probation"), (curve, 1200, "early_buyers"), (quiet, 100, "probation")):
        _coin(conn2, m, NOW - age)
        conn2.execute("INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)", (m, NOW - age, reason, NOW + 86400, NOW - age))
    vs = 30_000_000_000
    for i in range(12):                                   # twelve distinct buyers in the first minute: the early-buyers trigger
        vs += int(0.4e9)
        conn2.execute(TRADE_SQL, (early, 1, NOW - 80 + i * 5, NOW - 80 + i * 5 + 0.2, f"e{i}", f"B{i}", 1, 0.4, 4e5, vs, K // vs))
    vc = 30_000_000_000 + int(62e9)                       # 62 SOL in the curve: the H2 follow trigger, not yet the crossing
    for i in range(3):
        vc += int(0.5e9)
        conn2.execute(TRADE_SQL, (curve, 1, NOW - 60 + i * 10, NOW - 60 + i * 10 + 0.2, f"c{i}", f"W{i}", 1, 0.5, 4e5, vc, K // vc))
    conn2.execute(TRADE_SQL, (quiet, 1, NOW - 90, NOW - 89.8, "q0", "Q0", 1, 0.1, 4e5, 30_100_000_000, K // 30_100_000_000))
    conn2.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                  (NOW - 10, NOW - 9, "observe", mint_of("observed"), None, "{}", "test-rules-v1", "observe", "[]"))
    conn2.commit()
    first = eng.tick()
    assert first["candidates"] == 3 and first["follow_triggers"] == 2 and first["follow_new"] == 2 and first["polls"]["enabled"] is True and first["polls"]["calls"] == 1 and first["polls"]["polled"] == 2
    rows = {r["mint"]: dict(r) for r in conn2.execute("SELECT * FROM followed")}
    assert set(rows) == {early, curve} and rows[early]["reason"] == "early_buyers" and rows[curve]["reason"] == "curve60" and rows[early]["since"] == NOW
    assert quiet not in rows                              # one buyer, no trigger
    assert conn2.execute("SELECT COUNT(*) FROM path_points").fetchone()[0] == 2
    # the observe decision of the engine is followed from the next tick on (the cursor started at the first tick)
    conn2.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                  (NOW + 5, NOW + 6, "observe", mint_of("observed2"), None, "{}", "test-rules-v1", "observe", "[]"))
    conn2.commit()
    clock[0] = NOW + 15
    second = eng.tick()
    assert second["follow_new"] == 1 and conn2.execute("SELECT reason FROM followed WHERE mint=?", (mint_of("observed2"),)).fetchone()[0] == "observe"
    st = plays_service.status(conn2, s2, clock[0])
    assert st["last_tick"]["polls"]["polled"] == 1 and st["last_tick"]["candidates"] == 3     # only the fresh row was due 15 s later: a new followed coin is polled at once
    conn.close()
    conn2.close()


# ----- the harness on a world in which the stream lets go -----------------------------------------------------------------------
def _ramp_vsol(t0: float, t: float) -> int:
    """The curve of the synthetic world: 0.5 SOL per buy in the first three minutes (one buy every five seconds), then a slow rise of
    0.2 SOL per minute: a path every world shares, sampled by trades, polls or both."""
    if t <= t0 + 180:
        return 30_000_000_000 + int(0.5e9) * int((t - t0) // 5)
    return _ramp_vsol(t0, t0 + 180) + int(0.2e9 * (t - t0 - 180) / 60.0)


def build_drop_world(root: Path, stream_until_s: float | None, polls: bool, trades_every_s: float = 5.0) -> dict:
    """One coin per day of DAYS: trades from the create; the stream lets go `stream_until_s` after the create (None: never); polls of
    the curve every 60 s from 60 s after the create until 30 minutes after it when `polls`."""
    s = keyed_settings(root)
    conn = db.connect(s.db_path)
    mints = []
    conn.execute("BEGIN")
    for di, day in enumerate(DAYS):
        t0 = day_bounds(day)[0] + 14 * 3600
        mint = mint_of(f"drop{di}")
        mints.append(mint)
        conn.execute(COIN_SQL, (mint, f"Drop {di}", f"D{di}", "Maker", 10 + di, t0, t0 + 0.4, 0.3))
        t = t0 + 5.0
        k = 0
        while t <= t0 + 1800 and (stream_until_s is None or t <= t0 + stream_until_s):
            vs = _ramp_vsol(t0, t)
            conn.execute(TRADE_SQL, (mint, 100 + k, t, t + 0.3, f"{mint[:6]}-{k}", f"W{k % 40}", 1, 0.5 if t <= t0 + 180 else 0.02, 4e5, vs, K // vs))
            k += 1
            t += trades_every_s
        if polls:
            tp = t0 + 60.0
            while tp <= t0 + 1800:
                vs = _ramp_vsol(t0, tp)
                conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES(?,?,?,?,?,?,?,0,?)",
                             (mint, tp, "poll", vs, K // vs, vs - 30_000_000_000, 700_000_000_000_000, 500))
                tp += 60.0
    db.kv_set(conn, fm.KV_STARTED, str(day_bounds(DAYS[0])[0]), day_bounds(DAYS[0])[0])
    conn.execute("COMMIT")
    return {"settings": s, "conn": conn, "mints": mints}


def test_harness_outcome_known_through_polls_when_the_stream_lets_go(tmp_path):
    kw = dict(min_cases=1, min_select_cases=1, variants=["none_ts15"])
    dropped = build_drop_world(tmp_path / "dropped", stream_until_s=180.0, polls=False)       # the M12 world: the stream let go after 3 minutes, nothing else
    followed = build_drop_world(tmp_path / "followed", stream_until_s=180.0, polls=True)      # milestone 13: the polls go on
    full = build_drop_world(tmp_path / "full", stream_until_s=None, polls=False)              # the world with every trade
    try:
        r_drop = rh.run_hypothesis(dropped["conn"], dropped["settings"], "H5", DAYS, **kw)["variants"][0]["full"]
        res_follow = rh.run_hypothesis(followed["conn"], followed["settings"], "H5", DAYS, **kw)
        r_follow = res_follow["variants"][0]["full"]
        res_full = rh.run_hypothesis(full["conn"], full["settings"], "H5", DAYS, **kw)
        r_full = res_full["variants"][0]["full"]
        assert r_drop["signals"] == 2 and r_drop["unknown"] == 2 and r_drop["cases"] == 0 and r_drop["unknown_share"] == 1.0           # the path ended: unknown, honestly
        assert r_follow["signals"] == 2 and r_follow["unknown"] == 0 and r_follow["cases"] == 2 and r_follow["exit_reasons"] == {"time_stop": 2}
        assert r_full["signals"] == 2 and r_full["unknown"] == 0 and r_full["cases"] == 2
        assert r_follow["fill_sources"] == {"entry": {"curve": 2}, "exit": {"poll": 2}} and r_full["fill_sources"] == {"entry": {"curve": 2}, "exit": {"curve": 2}}
        cf = {c["mint"]: c for c in res_follow["cases"]}
        cu = {c["mint"]: c for c in res_full["cases"]}
        for mint in cf:
            a, b = cf[mint], cu[mint]
            assert a["signal_t"] == b["signal_t"] and a["entry_kind"] == "curve" and a["exit_kind"] == "poll" and b["exit_kind"] == "curve"
            # the exit is seen at the next poll and the human delay then waits for the poll after that: at most two poll intervals (2 x 60 s)
            # later than the world with every trade; the M12 execution model is unchanged (a trigger counts from the point that shows it)
            assert abs(a["exit_t"] - b["exit_t"]) <= 120.0 and a["exit_reason"] == b["exit_reason"] == "time_stop" and a["post_follow"] is True
            assert abs(a["pnl_sol"] - b["pnl_sol"]) < 0.01 and a["pnl_sol"] is not None
        # the follow split and the points line of the report
        split = res_follow["variants"][0]["follow_split"]
        assert split["before"]["signals"] == 0 and split["after"]["signals"] == 2 and split["after"]["unknown_share"] == 0.0
        assert res_follow["config"]["follow_started_at"] == day_bounds(DAYS[0])[0] and res_follow["config"]["poll_points_in_window"] == 2 * 30
        md, js = rr.write_report(followed["settings"], res_follow, day_bounds(DAYS[-1])[1] + 60)
        text = md.read_text(encoding="utf-8")
        for needle in ("poll-punten in het venster", "Signalen vóór en ná de start van het volgen", "alleen trade-punten (zonder polls)", "uitstap poll 100%", "dev-verkoop is alleen op een trade-punt zichtbaar"):
            assert needle in text, needle
        # the sensitivity's source rows: with the polls the outcome is known, without them it is unknown again
        src = {d["include_polls"]: d for d in res_follow["sensitivity"]["sources"]}
        assert src[True]["cases"] == 2 and src[True]["unknown"] == 0 and src[False]["cases"] == 0 and src[False]["unknown"] == 2
        # with and without polls the trade-point figures are the same: the trade-only row of the followed world equals the dropped world
        assert src[False]["signals"] == r_drop["signals"] and src[False]["unknown"] == r_drop["unknown"] and src[False]["ev_sol"] == r_drop["ev_sol"] is None
        # and on the full world the polls change nothing that the trades did not already know (its sources rows agree)
        src_full = {d["include_polls"]: d for d in res_full["sensitivity"]["sources"]}
        assert src_full[True]["ev_sol"] == src_full[False]["ev_sol"] == r_full["ev_sol"] and src_full[True]["cases"] == src_full[False]["cases"] == 2
    finally:
        for w in (dropped, followed, full):
            w["conn"].close()


def test_price_path_polls_graduation_and_the_live_position_on_the_combined_path(settings, conn):
    params = CurveParams.from_config(settings.config("pumpfun"))
    mint = mint_of("path")
    t0 = NOW - 3600
    conn.execute(COIN_SQL, (mint, "Path", "P", "Maker", 3, t0, t0 + 0.4, 0.3))
    for k, (dt, vs) in enumerate(((5, 31_000_000_000), (65, 33_000_000_000), (125, 34_000_000_000))):
        conn.execute(TRADE_SQL, (mint, 1, t0 + dt, t0 + dt + 0.2, f"p{k}", "Maker" if k == 2 else f"W{k}", 1 if k < 2 else 0, 0.5, 4e5, vs, K // vs))
    for dt, vs, complete in ((60, 32_000_000_000, 0), (180, 36_000_000_000, 0), (240, 40_000_000_000, 0), (300, 0, 1), (360, 0, 1)):
        conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES(?,?,?,?,?,?,?,?,?)",
                     (mint, t0 + dt, "poll", vs, (K // vs) if vs else 0, max(0, vs - 30_000_000_000), 7, complete, 9))
    conn.commit()
    coin = rh.load_coin(conn, mint)
    assert len(coin.points) == 5 and coin.points[0].source == "poll" and coin.points[3].complete and not coin.points[3].has_state and coin.grad_t is None
    assert rh.graduation_time(coin) == t0 + 300                                   # the first poll that found the curve complete: the stream missed the migration
    path = rh.price_path(coin, t0)
    kinds = [(round(p.t - t0), p.kind, p.graduation, p.dev_sell) for p in path]
    assert kinds == [(5, "curve", False, False), (60, "poll", False, False), (65, "curve", False, False), (125, "curve", False, True), (180, "poll", False, False), (240, "poll", False, False),
                     (300, "curve", True, False)]
    assert path[-1].virtual_sol == 40_000_000_000 and path[-1].spot(params) == path[-2].spot(params)      # the graduation point carries the last known state, never the zeros
    assert [p.kind for p in rh.price_path(coin, t0, include_polls=False)] == ["curve", "curve", "curve", "curve"]
    assert PricePoint(1.0, "poll", 31_000_000_000, K // 31_000_000_000).state() is not None and PricePoint(1.0, "sample", price_sol=1e-7).state() is None
    # a position fills on a poll point against the curve's quote, records the point kind, and survives the JSON round trip
    pos = Position(t0 + 100, 0.5, params, hyps.EXITS["tr30"], entry_delay_s=20.0)
    pos.feed_all(path)
    assert pos.entry is not None and pos.entry.point_kind == "curve" and pos.entry.t == t0 + 125
    pos2 = Position(t0 + 150, 0.5, params, hyps.EXITS["ts5"], entry_delay_s=20.0)
    pos2.feed_all(path)
    assert pos2.entry.point_kind == "poll" and pos2.entry.venue == "curve" and pos2.entry.t == t0 + 180 and pos2.status == "open"
    restored = Position.from_json(json.loads(json.dumps(pos2.to_json())), params)
    assert restored.entry.point_kind == "poll" and restored.held == pos2.held
    legacy = Position.from_json({**pos2.to_json(), "fills": [{k: v for k, v in f.items() if k != "point_kind"} for f in pos2.to_json()["fills"]]}, params)
    assert legacy.entry.point_kind is None and legacy.held == pos2.held        # a plays row written before milestone 13 still loads
    # the live feature state: a newer poll carries the curve state, the window features stay the trades'
    from fart.research import features as rf

    kn = rf.knowable(coin.trades, t0 + 250)
    f = rf.replay_features(kn, t0 + 250, t0, params)
    assert f["curve_sol"] == 4.0 and f["curve_source" if "curve_source" in f else "virtual_sol"]
    rf.apply_poll_state(f, coin.points, t0 + 250, params)
    assert f["curve_source"] == "poll" and f["curve_sol"] == 10.0 and f["virtual_sol"] == 40_000_000_000 and f["buys_5m"] == 2 and f["sells_5m"] == 1
    f2 = rf.replay_features(kn, t0 + 130, t0, params)
    rf.apply_poll_state(f2, coin.points, t0 + 130, params)
    assert f2["curve_source"] == "trade" and f2["curve_sol"] == 4.0               # the last trade is newer than the last poll


def test_h2_sees_the_crossing_on_a_poll_in_the_harness_and_live(tmp_path):
    s = keyed_settings(tmp_path)
    conn = db.connect(s.db_path)
    params = CurveParams.from_config(s.config("pumpfun"))
    mint = mint_of("cross")
    t0 = day_bounds(DAYS[1])[0] + 12 * 3600
    conn.execute(COIN_SQL, (mint, "Cross", "X", "Maker", 3, t0, t0 + 0.4, 0.3))
    vs = 30_000_000_000 + int(60e9)
    for k in range(30):                                   # strong inflow up to 72 SOL in the curve, then the stream lets go
        vs += int(0.4e9)
        conn.execute(TRADE_SQL, (mint, 1, t0 + 10 + k * 5, t0 + 10 + k * 5 + 0.2, f"x{k}", f"W{k}", 1, 0.4, 4e5, vs, K // vs))
    for dt, add in ((200, 73.0), (260, 74.5), (320, 76.0), (380, 77.0), (440, 78.0), (500, 79.0), (560, 80.0), (620, 81.0), (680, 82.0), (740, 83.0), (800, 84.0)):
        v = 30_000_000_000 + int(add * 1e9)
        conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES(?,?,?,?,?,?,?,0,1)", (mint, t0 + dt, "poll", v, K // v, v - 30_000_000_000, 7))
    conn.commit()
    res = rh.run_hypothesis(conn, s, "H2", DAYS, min_cases=1, min_select_cases=1, variants=["m5_ts5"])
    full = res["variants"][0]["full"]
    assert full["signals"] == 1 and full["cases"] == 1 and full["exit_reasons"] == {"time_stop": 1} and full["fill_sources"] == {"entry": {"poll": 1}, "exit": {"poll": 1}}
    case = res["cases"][0]
    assert case["curve_source"] == "poll" and case["features"]["curve_source"] == "poll" and t0 + 320 <= case["signal_t"] <= t0 + 335   # the crossing was seen on the poll of 76 SOL
    # live: the plays daemon computes the same crossing on the same points with the same predicate
    budget = Budget.from_settings(conn, s, clock=lambda: t0 + 335)
    eng = plays_service.PlaysEngine(conn, s, clock=lambda: t0 + 335, rpc=FakeRpc(budget))
    coin = rh.load_coin(conn, mint)
    ctx_prev = eng.context(coin, t0 + 320, False, False)
    assert ctx_prev.f["curve_source"] == "poll" and ctx_prev.f["curve_sol"] == 76.0
    eng.prev[mint] = (t0 + 305, {**ctx_prev.f, "curve_sol": 74.5})
    ctx = eng.context(coin, t0 + 320, False, False)
    assert hyps.get("H2").fires(hyps.get("H2").variant("m5_ts5"), ctx) is True
    conn.close()


# ----- the tracker inside its gate and its time budget ---------------------------------------------------------------------------
def test_tracker_samples_every_subject_within_the_gate_and_the_time_budget(settings, conn, monkeypatch):
    from fart.measure import prices

    cfg = settings.config("measure")
    assert cfg["sampling"]["max_mints_per_run"] == 2000 and cfg["sampling"]["time_budget_s"] == 150 and cfg["tracker"]["run_budget_s"] == 220
    now = NOW
    mints = []
    for i in range(70):
        m = mint_of(f"grad{i}")
        mints.append(m)
        conn.execute(COIN_SQL, (m, f"G{i}", "G", "Cr", 7, now - 7200 - i, now - 7199 - i, 0.3))
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (m, now - 3600 - i * 10, now - 3599 - i * 10, "rpc_logs", "[]", f"Pool{i}"))
    conn.commit()
    calls: list[list[str]] = []

    def fake_request(budget, source, method, url, **kw):
        assert source == "dexscreener"
        if method == "tokens" and url.endswith("So11111111111111111111111111111111111111112"):
            return [{"baseToken": {"address": "So11111111111111111111111111111111111111112"}, "quoteToken": {"symbol": "USDC"}, "priceUsd": "180.0", "liquidity": {"usd": 1e6}}]
        chunk = url.rsplit("/", 1)[1].split(",")
        calls.append(chunk)
        return [{"baseToken": {"address": m}, "priceUsd": "0.00002", "marketCap": 20000, "fdv": 20000, "liquidity": {"usd": 5000}, "volume": {"h24": 1000, "h1": 100}, "dexId": "pumpswap"} for m in chunk]

    monkeypatch.setattr(prices, "request_json", fake_request)
    budget = Budget.from_settings(conn, settings, clock=lambda: now)
    tracker = outcomes.Tracker(conn, settings, budget, clock=lambda: now)
    stats = tracker.run()
    assert stats["subjects"] == 70 and stats["sampled"]["requested"] == 70 and stats["sampled"]["calls"] == 3 and [len(c) for c in calls] == [30, 30, 10]
    assert stats["sampled"]["stored"] == 70 and stats["sampled"]["blocked"] == 0 and stats["sampled"]["time_budget"] == 0 and stats["skipped"] == {"over_cap": 0, "dexscreener": 0, "time_budget": 0}
    assert set(stats["seconds"]) == {"sample", "outcomes"} and conn.execute("SELECT COUNT(*) FROM mcap_samples").fetchone()[0] == 70
    assert calls[0][0] == mints[0]                                        # newest anchor first
    assert conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source='dexscreener' AND blocked_reason IS NOT NULL").fetchone()[0] == 0   # inside the gate of 200 per minute
    # the time budget: a deadline that has passed stops the sampling and counts the rest; nothing is sampled twice
    calls.clear()
    tracker2 = outcomes.Tracker(conn, settings, budget, clock=lambda: now + 300)
    import time as _time

    stats2 = tracker2.run(deadline=_time.perf_counter() - 1.0)
    assert stats2["sampled"]["calls"] == 0 and stats2["sampled"]["time_budget"] == 70 and stats2["skipped"]["time_budget"] == 70 and calls == []
    assert int(db.kv_get(conn, "measure.track.skipped")) == 70 and json.loads(db.kv_get(conn, "measure.track.skipped.last"))["time_budget"] == 70
    # the run record and the status line; the live coverage of the last 24 hours (the rule of the reports: a sample at least every 10 minutes
    # from the graduation on): with one sample per graduation at `now` nobody is covered (the first hour had none)
    rec = outcomes.record_run(conn, now + 300, stats2, {"creator_stats": {"creators_updated": 3, "stopped_early": False, "backlog_sources": 0, "seconds": 1.5}, "candles": {"due": 0, "fetched": 0, "candles": 0, "errors": 0, "blocked": 0, "time_budget": 0, "seconds": 0.2}}, 12.5, 220.0)
    assert rec["seconds"]["total"] == 12.5 and rec["seconds"]["creator_stats"] == 1.5 and rec["over_budget"] is False and outcomes.last_run(conn)["subjects"] == 70
    cov0 = outcomes.graduation_coverage_24h(conn, now + 650, 600.0)
    assert cov0["graduations"] == 70 and cov0["covered"] == 0 and cov0["share"] == 0.0
    for m in mints:
        for k in range(1, 14):
            conn.execute("INSERT OR IGNORE INTO mcap_samples(mint, ts, src, mcap_usd, sol_usd) VALUES(?,?,?,?,?)", (m, now - 3600 - 700 + k * 300, "dexscreener", 20000.0, 180.0))
    conn.commit()
    cov = outcomes.graduation_coverage_24h(conn, now + 650, 600.0)
    assert cov["graduations"] == 70 and cov["covered"] == 70 and cov["share"] == 1.0           # a sample every 300 s from the graduation to now: covered up to now + 650 - 600
    cov2 = outcomes.graduation_coverage_24h(conn, now + 3000, 600.0)
    assert cov2["covered"] == 0 and cov2["share"] == 0.0                                        # 40 minutes without a sample: no graduation is covered any more
    assert outcomes.graduation_coverage_24h(conn, now + 650, 600.0, min_age_s=90_000)["graduations"] == 0
    from fart.cli import tracker_run_line

    line = tracker_run_line({"last_run": outcomes.last_run(conn), "coverage_24h": cov}, now + 310)
    assert line.startswith("tracker run: last 10 s ago, 12 s (sample ") and "time budget cut 70" in line and "creator_stats 1.5 s: 3 updated" in line and "budget 220 s" in line
    assert line.endswith("graduations of the last 24 h with a sample every 10 min: 100% (70 of 70)")
    assert tracker_run_line({"coverage_24h": {"graduations": 0}}, now).startswith("tracker run: no step durations recorded yet")


def test_run_extra_steps_hands_the_remaining_budget_to_the_steps(settings, conn, monkeypatch):
    from fart.measure import candles, creators
    import time as _time

    seen = {}

    def fake_creators(c, s, now, time_budget_s=60.0):
        seen["creators_budget"] = time_budget_s
        return {"creators_updated": 0}

    def fake_candles(c, b, s, now, request=None, deadline=None):
        seen["candles_deadline"] = deadline
        return {"fetched": 0}

    monkeypatch.setattr(creators, "update", fake_creators)
    monkeypatch.setattr(candles, "run", fake_candles)
    budget = Budget.from_settings(conn, settings, clock=lambda: NOW)
    t0 = _time.perf_counter()
    out = outcomes.run_extra_steps(conn, budget, settings, NOW, deadline=t0 + 100.0)
    assert 5.0 <= seen["creators_budget"] <= 60.0 and seen["creators_budget"] == pytest.approx(min(60.0, 100.0 - 20.0), abs=1.0)
    assert seen["candles_deadline"] == pytest.approx(t0 + 95.0, abs=0.5) and out["creator_stats"]["seconds"] >= 0 and out["candles"]["seconds"] >= 0
    out2 = outcomes.run_extra_steps(conn, budget, settings, NOW, deadline=t0 - 1.0)      # nothing left: the steps get the minimum, never a negative budget
    assert seen["creators_budget"] >= 5.0 and "error" not in out2["creator_stats"]


def test_retention_prunes_poll_points_and_followed_rows_with_the_trades(settings, conn):
    from fart.ingest.retention import run_retention

    now = NOW
    conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES('M', ?, 'poll', 31, 7, 1, 7, 0, 5)", (now - 40 * 86400,))
    conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES('M', ?, 'poll', 31, 7, 1, 7, 0, 5)", (now - 86400,))
    conn.execute("INSERT INTO followed(mint, since, reason, until, state, created_at, updated_at) VALUES('Old', ?, 'observe', ?, 'done', ?, ?)", (now - 41 * 86400, now - 40 * 86400, now, now))
    conn.execute("INSERT INTO followed(mint, since, reason, until, state, created_at, updated_at) VALUES('New', ?, 'observe', ?, 'active', ?, ?)", (now - 100, now + 86300, now, now))
    conn.commit()
    out = run_retention(conn, settings.raw_dir, settings.archive_dir, settings.config("ingest")["retention"], now)
    assert out["path_points_archived"] == 1 and out["followed_archived"] == 1
    assert conn.execute("SELECT COUNT(*) FROM path_points").fetchone()[0] == 1 and [r[0] for r in conn.execute("SELECT mint FROM followed")] == ["New"]
    assert list(settings.archive_dir.glob("path_points-*.jsonl.gz")) and list(settings.archive_dir.glob("followed-*.jsonl.gz"))


def test_status_shows_the_follow_and_tracker_lines(settings, conn, capsys):
    from fart.cli import m12_lines, main

    conn.close()
    assert main(["--json", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["follow"]["exists"] is True and out["follow"]["total"] == 0 and out["follow"]["share_cu"] == 300_000.0 and out["follow"]["config"]["max_followed"] == 1500
    assert out["tracker"]["max_mints_per_run"] == 2000 and out["tracker"]["last_run"] is None and out["tracker"]["coverage_24h"]["graduations"] == 0
    lines = m12_lines(out)
    assert lines[-1].startswith("follow: 0 followed (active 0, graduated 0, done 0, closed 0)") and "never ticked" in lines[-1]
    assert main(["status"]) == 0
    text = capsys.readouterr().out
    assert "follow: 0 followed" in text and "tracker run: no step durations recorded yet" in text
