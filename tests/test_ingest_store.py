import gzip
import json

from fart.chain.events import decode_logs
from fart.ingest import coverage, store, watch
from fart.ingest.raw import RawWriter, iter_raw, prune_raw


def load(fixtures_dir, name):
    return json.loads((fixtures_dir / name).read_text())


def test_raw_writer_hourly_gzip_members(settings):
    w = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0, flush_lines=1000)
    t0 = 1_761_480_000.0  # 2025-10-26T12:00:00Z
    ref = w.write({"a": 1}, t0)
    w.write({"a": 2}, t0 + 10)
    w.flush()
    w.write({"a": 3}, t0 + 20)  # second member appended to the same hour file
    w.write({"a": 4}, t0 + 3600)  # next hour file
    w.close()
    assert ref == "pumpportal/20251026/12.jsonl.gz"
    p = settings.raw_dir / "pumpportal" / "20251026" / "12.jsonl.gz"
    rows = list(iter_raw(p))
    assert [r["msg"]["a"] for r in rows] == [1, 2, 3]
    assert rows[0]["received_at"] == t0
    assert (settings.raw_dir / "pumpportal" / "20251026" / "13.jsonl.gz").exists()
    # truncated final member does not break reading of the earlier ones
    data = p.read_bytes()
    p.write_bytes(data[:-7])
    assert [r["msg"]["a"] for r in iter_raw(p)][:2] == [1, 2]


def test_prune_raw_by_age(settings):
    w = RawWriter(settings.raw_dir, "pumpportal")
    old = 1_700_000_000.0
    w.write({"x": 1}, old)
    w.write({"x": 2}, old + 40 * 86400)
    w.close()
    removed = prune_raw(settings.raw_dir, keep_days=30, now_ts=old + 40 * 86400)
    assert removed == 1
    assert not (settings.raw_dir / "pumpportal" / "20231114").exists()
    assert (settings.raw_dir / "pumpportal" / "20231224").exists()


def test_pumpportal_messages_normalize(conn, fixtures_dir):
    msgs = load(fixtures_dir, "pumpportal_messages.json")
    assert store.classify_pumpportal(msgs["new_token"]) == "create"
    assert store.classify_pumpportal(msgs["trade"]) == "trade"
    assert store.classify_pumpportal(msgs["sell"]) == "trade"
    assert store.classify_pumpportal(msgs["migration"]) == "migrate"
    assert store.classify_pumpportal(msgs["junk"]) == "info"
    assert store.classify_pumpportal("garbage") == "unknown"
    assert store.classify_pumpportal({"foo": 1}) == "unknown"

    rec = store.pumpportal_create_record(msgs["new_token"])
    assert store.upsert_coin(conn, rec, "pumpportal", received_at=100.0, raw_ref="pumpportal/x.jsonl.gz") is True
    row = conn.execute("SELECT * FROM coins").fetchone()
    assert row["creator"] == msgs["new_token"]["traderPublicKey"]
    assert row["dev_buy_sol"] == 1.5 and row["block_time"] is None and row["received_at"] == 100.0
    assert json.loads(row["sources"]) == ["pumpportal"]

    watch.add(conn, rec["mint"], "probation", now=100.0, ttl_s=300, cap=200)
    t = store.pumpportal_trade_record(msgs["trade"])
    assert store.insert_trade(conn, t, "pumpportal", 101.0) is True
    assert store.insert_trade(conn, t, "pumpportal", 102.0) is False  # duplicate signature ignored
    s = store.pumpportal_trade_record(msgs["sell"])
    assert s["is_buy"] is False
    assert store.insert_trade(conn, s, "pumpportal", 103.0) is True
    tr = conn.execute("SELECT * FROM trades ORDER BY received_at").fetchall()
    assert tr[0]["virtual_sol"] == 32.15e9 and tr[0]["virtual_token"] == 1_002_000_000e6
    assert conn.execute("SELECT last_trade_at FROM watchlist").fetchone()[0] == 103.0

    m = store.pumpportal_migration_record(msgs["migration"])
    assert store.upsert_migration(conn, m, "pumpportal", 200.0) is True
    assert conn.execute("SELECT dex FROM migrations").fetchone()[0] == "pump-amm"


def test_rpc_events_normalize_and_merge_with_pumpportal(conn, fixtures_dir):
    msgs = load(fixtures_dir, "pumpportal_messages.json")
    note = load(fixtures_dir, "logs_notification_create.json")
    v = note["params"]["result"]["value"]
    events, undecoded = decode_logs(v["logs"])
    recs = store.records_from_events(events, v["signature"], note["params"]["result"]["context"]["slot"])
    c = recs["create"]
    assert c["mint"] == msgs["new_token"]["mint"]
    assert c["slot"] == 370_000_001 and c["block_time"] == 1_761_480_000
    assert c["dev_buy_sol"] == 1.5
    assert len(recs["trades"]) == 1 and recs["trades"][0]["is_buy"] is True

    # PumpPortal first (no chain time), then RPC fills slot/block_time and unions sources
    store.upsert_coin(conn, store.pumpportal_create_record(msgs["new_token"]), "pumpportal", 100.0)
    assert store.upsert_coin(conn, c, "rpc_logs", 100.4) is False
    row = conn.execute("SELECT * FROM coins").fetchone()
    assert row["slot"] == 370_000_001 and row["block_time"] == 1_761_480_000
    assert row["received_at"] == 100.0  # earliest arrival kept
    assert row["initial_mcap_sol"] == msgs["new_token"]["marketCapSol"]  # PP-only detail kept
    assert json.loads(row["sources"]) == ["pumpportal", "rpc_logs"]
    assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 1

    mig = load(fixtures_dir, "logs_notification_migration.json")["params"]["result"]
    ev2, _ = decode_logs(mig["value"]["logs"])
    recs2 = store.records_from_events(ev2, mig["value"]["signature"], mig["context"]["slot"])
    assert recs2["migration"]["pool_address"] is not None
    store.upsert_migration(conn, store.pumpportal_migration_record(msgs["migration"]), "pumpportal", 300.0)
    store.upsert_migration(conn, recs2["migration"], "rpc_logs", 300.5)
    mrow = conn.execute("SELECT * FROM migrations").fetchone()
    assert mrow["pool_address"] == recs2["migration"]["pool_address"] and mrow["block_time"] == 1_761_480_090
    assert json.loads(mrow["sources"]) == ["pumpportal", "rpc_logs"]


def test_undecoded_payloads_are_stored_and_counted(conn, fixtures_dir):
    logs = load(fixtures_dir, "logs_mixed_bad.json")["logs"]
    events, undecoded = decode_logs(logs)
    n = store.store_undecoded(conn, undecoded, "rpc_logs", "sig", 1, received_at=50.0)
    assert n == 4  # ignored_event is not stored
    reasons = [r[0] for r in conn.execute("SELECT reason FROM raw_undecoded ORDER BY id")]
    assert reasons[0] == "unknown_discriminator"
    store.counter_bump(conn, "decode.unknown_discriminator", 1, ts=50.0)
    store.counter_bump(conn, "decode.unknown_discriminator", 2, ts=51.0)
    assert conn.execute("SELECT value FROM kv WHERE key='decode.unknown_discriminator'").fetchone()[0] == "3"


def test_watchlist_cap_eviction_and_expiry(conn):
    for i in range(3):
        assert watch.add(conn, f"P{i}", "probation", now=100.0 + i, ttl_s=300, cap=3)
    assert watch.active_mints(conn) == ["P0", "P1", "P2"]
    # a graduation evicts the oldest probation entry
    assert watch.add(conn, "G1", "graduation", now=110.0, ttl_s=86400, cap=3)
    assert watch.active_mints(conn) == ["P1", "P2", "G1"]
    # upgrading an existing entry does not consume a slot
    assert watch.add(conn, "P1", "observe", now=111.0, ttl_s=86400, cap=3)
    assert conn.execute("SELECT reason FROM watchlist WHERE mint='P1'").fetchone()[0] == "observe"
    # when everything left is higher priority than the request, nothing is evicted
    assert watch.add(conn, "G2", "graduation", now=112.0, ttl_s=86400, cap=3)  # evicts P2 (probation)
    assert watch.add(conn, "P9", "probation", now=113.0, ttl_s=300, cap=3) is False
    assert sorted(watch.active_mints(conn)) == ["G1", "G2", "P1"]
    # expiry by until and by idleness
    assert watch.expire(conn, now=100.0 + 86400 + 200) == 3
    assert watch.active_mints(conn) == []
    watch.add(conn, "D1", "graduation", now=200.0, ttl_s=86400, cap=3)
    conn.execute("UPDATE watchlist SET last_trade_at=250 WHERE mint='D1'")
    assert watch.expire(conn, now=250 + 7200 + 1, dead_after_idle_s=7200) == 1
    assert not watch.is_watched(conn, "D1")


def _coin(conn, mint, t, sources, block_time=None):
    conn.execute(
        "INSERT INTO coins(mint, received_at, block_time, source, sources) VALUES(?,?,?,?,?)",
        (mint, t, block_time, sources[0], json.dumps(sources)),
    )


def test_coverage_with_synthetic_gap(conn):
    from fart.util import hour_bounds

    hour = "2025-10-26T12"
    start, end = hour_bounds(hour)
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.pumpportal.started','1',0)")
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.rpc_logs.started','1',0)")
    # 4 creates: 2 seen by both, 1 PumpPortal-only (during the RPC gap), 1 RPC-only (during the PP gap)
    _coin(conn, "A", start + 60, ["pumpportal", "rpc_logs"], block_time=start + 59)
    _coin(conn, "B", start + 600, ["rpc_logs", "pumpportal"], block_time=start + 599)
    _coin(conn, "C", start + 1300, ["pumpportal"])
    _coin(conn, "D", start + 2500, ["rpc_logs"], block_time=start + 2499)
    conn.execute("INSERT INTO migrations(mint, received_at, source, sources) VALUES('A', ?, 'pumpportal', '[\"pumpportal\"]')", (start + 3000,))
    conn.execute("INSERT INTO migrations(mint, received_at, source, sources) VALUES('Z', ?, 'pumpportal', '[\"pumpportal\"]')", (start + 3100,))
    g1 = coverage.gap_open(conn, "rpc_logs", start + 1200, "disconnect")
    coverage.gap_close(conn, g1, start + 1500)
    g2 = coverage.gap_open(conn, "pumpportal", start + 2400, "watchdog")
    coverage.gap_close(conn, g2, start + 2700)

    row = coverage.write_hour(conn, hour, now=end + 10)
    assert row["creates_pp"] == 3 and row["creates_rpc"] == 3 and row["creates_union"] == 4
    assert row["grads"] == 2 and row["grads_without_create"] == 1
    assert [g["source"] for g in row["gaps"]] == ["rpc_logs", "pumpportal"]
    assert row["gaps"][0]["seconds"] == 300.0
    # both create sources had a gap in this hour -> not complete
    assert row["complete"] is False
    stored = conn.execute("SELECT * FROM coverage WHERE hour=?", (hour,)).fetchone()
    assert stored["creates_union"] == 4 and stored["complete"] == 0
    assert len(json.loads(stored["gaps_json"])) == 2

    # the next hour: no gaps, hour over -> complete; while running -> not complete yet
    nxt = "2025-10-26T13"
    s2, e2 = hour_bounds(nxt)
    _coin(conn, "E", s2 + 5, ["pumpportal", "rpc_logs"])
    assert coverage.compute_hour(conn, nxt, now=s2 + 100)["complete"] is False
    assert coverage.compute_hour(conn, nxt, now=e2 + 1)["complete"] is True

    # a gap still open (crash) reaches into the hour until closed
    g3 = coverage.gap_open(conn, "pumpportal", e2 - 100, "crash")
    live = coverage.compute_hour(conn, nxt, now=e2 + 50)
    assert live["gaps"][0]["seconds"] == 100.0
    assert coverage.close_dangling_gaps(conn, end=e2 + 50) == 1
    rep = coverage.report(conn, hours=2, now=e2 + 50)
    assert [r["hour"] for r in rep] == [nxt, "2025-10-26T14"]  # now is inside hour 14


def test_coverage_without_configured_sources_is_never_complete(conn):
    from fart.util import hour_bounds

    hour = "2025-10-26T12"
    _, end = hour_bounds(hour)
    assert coverage.compute_hour(conn, hour, now=end + 1)["complete"] is False
