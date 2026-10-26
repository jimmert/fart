"""Milestone 5, point 3: the community channel, OFF by default. (a) nothing is ever queued for or sent to
another chat than the owner unless TELEGRAM_COMMUNITY_CHAT_ID is set AND TELEGRAM_COMMUNITY_ENABLED=1;
(b) the Dutch community format; (c) TELEGRAM_COMMUNITY_PREVIEW=1 sends it to the owner headed "VOORBEELD
community-bericht" within the owner limits; (d) the community rules (1 per coin/theme per 24 h, 6 per hour,
quiet hours, the weekly accountability message) apply even while the channel is off.

No real message is ever sent: the HTTP layer is the stub of tests/test_notify.py."""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from fart import db
from fart.config import load_settings
from fart.notify import format as fmt
from fart.notify import outbox
from fart.notify.service import Notifier
from fart.notify.telegram import TelegramSender
from tests.test_notify import FOOTER, Clock, T0, add_decision, install_fake_http, make_budget

OWNER = "4242"
COMMUNITY = "-1001234567890"
COIN = {"name": "Zorblax", "symbol": "ZORB", "age_s": 240, "mcap_sol": 312.5, "unique_buyers_5m": 31, "net_sol_5m": 14.25, "bundled": False,
        "largest_buyer_share_15m": 0.12, "creator_prior_coins_24h": 2, "x_mentions_1h": 4, "x_distinct_authors_1h": 3}
META = {"n_coins": 5, "distinct_creators": 4, "sol_inflow": 44.2, "unique_buyers": 30, "burst_ratio": None, "burst_ratio_is_inf": True,
        "sample_mints": ["MintA", "MintB", "MintC", "MintD"]}


def community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="0", preview="0", notify="1"):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_NOTIFY_ENABLED", "TELEGRAM_COMMUNITY_CHAT_ID",
              "TELEGRAM_COMMUNITY_ENABLED", "TELEGRAM_COMMUNITY_PREVIEW", "X_BEARER_TOKEN", "RPC_HTTP_URL", "RPC_WSS_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN=123:PLACEHOLDER-TG\nTELEGRAM_OWNER_CHAT_ID={OWNER}\nTELEGRAM_NOTIFY_ENABLED={notify}\n"
        f"TELEGRAM_COMMUNITY_CHAT_ID={chat_id}\nTELEGRAM_COMMUNITY_ENABLED={enabled}\nTELEGRAM_COMMUNITY_PREVIEW={preview}\n")
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    return s


def notifier_for(settings, clock=None):
    conn = db.connect(settings.db_path)
    clock = clock or Clock()
    budget = make_budget(settings, conn, clock)
    n = Notifier(conn, settings, budget, TelegramSender(budget, "t"), clock=clock)
    n.build(T0 - 1000)  # the first run only sets the pointers (no history dump); the daily digest of the first run too
    conn.execute("DELETE FROM outbox")
    return conn, n


def chats(conn):
    return {r[0] for r in conn.execute("SELECT DISTINCT chat FROM outbox")}


# ----- (a) never another chat without both flags ------------------------------------------------------------------
def test_defaults_are_off_and_describe_shows_no_values(tmp_path, monkeypatch):
    s = community_settings(tmp_path, monkeypatch, chat_id="", enabled="0")
    assert not s.community_configured and not s.community_enabled and not s.community_preview
    d = s.describe()
    assert d["telegram_community"] == "not configured" and d["telegram_community_enabled"] is False and d["telegram_community_preview"] is False
    assert COMMUNITY not in json.dumps(d) and "PLACEHOLDER" not in json.dumps(d)
    # a chat id alone: configured, not enabled; the flag alone: not even configured; the owner's own id is never "community"
    assert community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="0").community_enabled is False
    assert community_settings(tmp_path, monkeypatch, chat_id="", enabled="1").community_enabled is False
    assert community_settings(tmp_path, monkeypatch, chat_id=OWNER, enabled="1").community_enabled is False
    # both set, but the owner channel itself off: nothing is enabled
    assert community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="1", notify="0").community_enabled is False
    assert community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="1").community_enabled is True


def test_nothing_is_queued_or_sent_to_another_chat_unless_both_flags_are_set(tmp_path, monkeypatch):
    for chat_id, enabled in (("", "0"), (COMMUNITY, "0"), ("", "1")):
        settings = community_settings(tmp_path, monkeypatch, chat_id=chat_id, enabled=enabled, preview="1")
        conn, n = notifier_for(settings)
        assert n.allowed_chats == {OWNER} and not n.community_enabled
        add_decision(conn, "observe", T0 + 10, mint="M1", features=COIN)
        add_decision(conn, "observe_meta", T0 + 20, term="goldberg cat", features=META)
        add_decision(conn, "fast_post", T0 + 30, term="x:2", features={"author": "a", "likes_per_min": 1, "likes": 1, "matched": [], "mints": [], "post_id": "2", "text": "hi"})
        built = n.build(T0 + 400)
        assert built["observe"] == 1 and built["fast_post"] == 1 and built["community"] == 2  # two previews to the owner
        assert chats(conn) == {OWNER}, (chat_id, enabled)
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='community'").fetchone()[0] == 0
        # a direct attempt for another chat is refused before it reaches the outbox
        assert n._enqueue(conn, "community", "forged:1", COMMUNITY, "x", T0) is None and n.stats["refused_chat"] == 1
        # a forged pending row for another chat is never sent: no HTTP call, the row ends as gave_up with the reason
        outbox.enqueue(conn, "community", "forged:2", COMMUNITY, "x", T0 + 401)
        calls = install_fake_http(monkeypatch, [("ok", 1)] * 10)
        res = n.send_due(T0 + 402)
        assert res["refused_chat"] == 1 and all(c["body"]["chat_id"] == OWNER for c in calls)
        row = conn.execute("SELECT status, error FROM outbox WHERE dedupe_key='forged:2'").fetchone()
        assert row["status"] == "gave_up" and "chat not allowed" in row["error"]
        conn.close()
        (tmp_path / "data" / "fart.db").unlink()


def test_with_both_flags_the_community_chat_receives_the_community_text(tmp_path, monkeypatch):
    settings = community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="1", preview="0")
    conn, n = notifier_for(settings)
    assert n.allowed_chats == {OWNER, COMMUNITY} and n.community_enabled and not n.community_preview
    d1 = add_decision(conn, "observe", T0 + 10, mint="MintZorb", features=COIN)
    built = n.build(T0 + 400)
    assert built["observe"] == 1 and built["community"] == 1
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM outbox")}
    assert rows["community"]["chat"] == COMMUNITY and rows["community"]["dedupe_key"] == f"community:{d1}"
    assert rows["observe"]["chat"] == OWNER and "community_preview" not in rows
    assert "VOORBEELD" not in rows["community"]["text"]
    calls = install_fake_http(monkeypatch, [("ok", 1), ("ok", 2)])
    assert n.send_due(T0 + 401)["sent"] == 2
    assert {c["body"]["chat_id"] for c in calls} == {OWNER, COMMUNITY}
    # switching the channel off afterwards stops what was still queued for it
    add_decision(conn, "observe", T0 + 5000, mint="MintLater", features=COIN)
    n.build(T0 + 5400)
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='community' AND status='pending'").fetchone()[0] == 1
    n.community_enabled = False
    install_fake_http(monkeypatch, [("ok", 3)] * 5)
    res = n.send_due(T0 + 5401)
    assert res["refused_chat"] == 1 and conn.execute("SELECT status FROM outbox WHERE kind='community' AND created_at > ?", (T0 + 5000,)).fetchone()[0] == "gave_up"
    conn.close()


# ----- (b) the community format ---------------------------------------------------------------------------------------
def test_community_format_is_plain_dutch_with_rules_warning_and_mint_as_text(settings):
    ecfg = settings.config("engine")
    ccfg = settings.config("notify")["community"]
    dec = {"id": 7, "kind": "observe", "mint": "MintZorb111", "as_of": T0, "rules_version": "150ca73cb354", "features": COIN}
    t = fmt.community_message(dec, 200.0, "150ca73cb354", ecfg, ccfg, FOOTER, theme="zorblax")
    assert t.startswith("<b>Gezien</b> · 26 okt 14:05 (13:05 UTC)") and t.endswith(f"<i>{FOOTER}</i>")
    assert "Munt <b>Zorblax</b> ($ZORB), leeftijd 4 min, thema “zorblax”" in t
    assert "Kopers in 5 minuten: 31 · instroom 14,25 SOL netto · marktkapitalisatie 312,5 SOL (≈ $62.500)" in t
    assert "Mint-adres (tekst, geen link): <code>MintZorb111</code>" in t and "href" not in t and "http" not in t.lower()
    assert "<b>Waarom dit opvalt</b>: 31 verschillende kopers in 5 minuten, 14,25 SOL meer gekocht dan verkocht, grootste koper 12% van de instroom, geen bundel gezien, 4 vermeldingen op X" in t
    assert "<b>Regels</b> (bevroren versie <code>150ca73cb354</code>): minstens 25 unieke kopers en 10,0 SOL netto instroom in 5 minuten; grootste koper hoogstens 35% van de instroom; geen bundel" in t
    assert f"<b>Let op</b>: {ccfg['warning']}" in t and "data: beslissing #7" in t
    for word in ("koop", "BUY", "LAUNCH", "advies", "tier", "signaal"):
        assert word not in t.replace("Geen advies", "").replace("geen advies", "")
    # the preview copy differs only by its header
    p = fmt.community_message(dec, 200.0, "150ca73cb354", ecfg, ccfg, FOOTER, theme="zorblax", preview=True)
    assert p.startswith("<b>VOORBEELD community-bericht</b>\n\n<b>Gezien</b>") and p.split("\n\n", 1)[1] == t
    # a theme decision
    md = {"id": 8, "kind": "observe_meta", "term": "goldberg cat", "as_of": T0, "features": META}
    m = fmt.community_message(md, None, "150ca73cb354", ecfg, ccfg, FOOTER)
    assert "Thema “goldberg cat”: 5 nieuwe munten van 4 verschillende makers in 3 uur" in m and "oneindig veel vaker dan normaal" in m
    assert "Voorbeelden (mint-adres als tekst): <code>MintA</code>, <code>MintB</code>, <code>MintC</code>" in m and "MintD" not in m
    assert "minstens 3 munten van 3 makers in 3 uur, 3,0× de eigen basislijn, 20,0 SOL instroom en 25 unieke kopers" in m
    assert fmt.coin_rules_text(ecfg["coin"]).startswith("minstens 25 unieke kopers")


# ----- (c) the preview to the owner within the owner limits ------------------------------------------------------------
def test_preview_goes_to_the_owner_headed_voorbeeld_and_counts_in_the_owner_limits(tmp_path, monkeypatch):
    settings = community_settings(tmp_path, monkeypatch, chat_id="", enabled="0", preview="1")
    settings.configs["notify"]["community"]["max_per_hour"] = 100  # the community cap is tested separately
    conn, n = notifier_for(settings)
    assert n.community_preview and not n.community_enabled and n.allowed_chats == {OWNER}
    d1 = add_decision(conn, "observe", T0 + 10, mint="MintZorb", features=COIN)
    d2 = add_decision(conn, "observe_meta", T0 + 20, term="goldberg cat", features=META)
    built = n.build(T0 + 400)
    assert built["observe"] == 1 and built["community"] == 2
    rows = conn.execute("SELECT kind, chat, dedupe_key, text FROM outbox ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["observe", "community_preview", "community_preview"] and all(r["chat"] == OWNER for r in rows)
    assert rows[1]["dedupe_key"] == f"community_preview:{d1}" and rows[2]["dedupe_key"] == f"community_preview:{d2}"
    assert rows[1]["text"].startswith("<b>VOORBEELD community-bericht</b>") and "Zorblax" in rows[1]["text"]
    assert rows[2]["text"].startswith("<b>VOORBEELD community-bericht</b>") and "goldberg cat" in rows[2]["text"]
    assert chats(conn) == {OWNER}
    # observe.max_batches_per_hour (6) counts the previews: 4 previews + 1 batch so far -> one more batch, then deferred
    for i in range(4):
        add_decision(conn, "observe", T0 + 700 + i, mint=f"P{i}", features=COIN)
    built = n.build(T0 + 701 + 400)
    assert built["observe"] == 1 and built["community"] == 4
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind IN ('observe', 'community_preview')").fetchone()[0] == 8
    add_decision(conn, "observe", T0 + 1200, mint="Deferred", features=COIN)
    built = n.build(T0 + 1201 + 400)
    assert built["observe"] == 0 and n.stats["observe_deferred"] == 1  # the owner's hourly batch limit is reached by the previews
    # send.max_per_minute (20) is shared: previews and observe batches together never exceed 20 in a minute
    for i in range(20):
        add_decision(conn, "observe", T0 + 5000 + i, mint=f"Q{i}", features=COIN)
    n.build(T0 + 5400)
    pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
    assert pending >= 20
    calls = install_fake_http(monkeypatch, [("ok", 100 + i) for i in range(60)])
    assert n.send_due(T0 + 5401)["sent"] == 20 and len(calls) == 20 and all(c["body"]["chat_id"] == OWNER for c in calls)
    assert n.send_due(T0 + 5430)["sent"] == 0
    n.clock.t = T0 + 5462  # the budget's token bucket (20 per minute, 0.33/s) refills with the shared clock
    assert n.send_due(T0 + 5462)["sent"] == pending - 20
    conn.close()


# ----- (d) the community rules, also while the channel is off ----------------------------------------------------------
def test_rules_one_per_key_per_24h_six_per_hour_and_quiet_hours(tmp_path, monkeypatch):
    settings = community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="1", preview="1")
    conn, n = notifier_for(settings)
    assert settings.config("notify")["community"]["max_per_hour"] == 6 and settings.config("notify")["community"]["per_key_cooldown_s"] == 86400
    # same coin twice and the same theme twice within 24 h: one message each (real + preview)
    add_decision(conn, "observe", T0 + 10, mint="SAME", features=COIN)
    add_decision(conn, "observe", T0 + 2000, mint="SAME", features=COIN)
    add_decision(conn, "observe_meta", T0 + 20, term="goldberg cat", features=META)
    add_decision(conn, "observe_meta", T0 + 2100, term="goldberg cat", features=META)
    built = n.build(T0 + 2500)
    assert built["community"] == 4 and n.stats["community_skipped_key_cooldown"] == 2
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='community'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='community_preview'").fetchone()[0] == 2
    assert db.kv_get(conn, "notify.community.key.coin:SAME") == str(T0 + 2500)
    # the same coin again within 24 h: nothing; after 24 h: again
    add_decision(conn, "observe", T0 + 40000, mint="SAME", features=COIN)
    assert n.build(T0 + 40400)["community"] == 0
    add_decision(conn, "observe", T0 + 2500 + 86400 + 10, mint="SAME", features=COIN)
    assert n.build(T0 + 2500 + 86400 + 400)["community"] == 2
    # at most 6 distinct decisions per hour (real and preview of one decision count once)
    conn.execute("DELETE FROM outbox")
    base = T0 + 200000
    for i in range(9):
        add_decision(conn, "observe", base + i, mint=f"H{i}", features=COIN)
    built = n.build(base + 400)
    assert built["community"] == 12 and n.stats["community_skipped_hour_cap"] == 3
    assert conn.execute("SELECT COUNT(DISTINCT dedupe_key) FROM outbox WHERE kind='community'").fetchone()[0] == 6
    add_decision(conn, "observe", base + 1000, mint="H9", features=COIN)
    assert n.build(base + 1400)["community"] == 0 and n.stats["community_skipped_hour_cap"] == 4  # skipped, never sent late
    add_decision(conn, "observe", base + 3700, mint="H10", features=COIN)
    assert n.build(base + 4100)["community"] == 2  # the hour passed
    # quiet hours 02:00-08:00 Europe/Amsterdam (CET in late October): 03:00 UTC is 04:00 local -> quiet; 13:05 UTC is not
    t_quiet = _dt.datetime(2026, 10, 28, 3, 0, tzinfo=_dt.timezone.utc).timestamp()
    assert n.in_quiet_hours(t_quiet) and not n.in_quiet_hours(T0)
    assert n.in_quiet_hours(_dt.datetime(2026, 10, 28, 1, 0, tzinfo=_dt.timezone.utc).timestamp())      # 02:00 local: the start is quiet
    assert not n.in_quiet_hours(_dt.datetime(2026, 10, 28, 7, 0, tzinfo=_dt.timezone.utc).timestamp())  # 08:00 local: the end is not
    add_decision(conn, "observe", t_quiet - 10, mint="NIGHT", features=COIN)
    assert n.build(t_quiet)["community"] == 0 and n.stats["community_skipped_quiet"] == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE '%NIGHT%'").fetchone()[0] == 0
    # start == end disables the quiet period; a wrapped period (23:00-01:00) works too
    n.ccfg = dict(n.ccfg, quiet_hours={"start": "02:00", "end": "02:00"})
    assert not n.in_quiet_hours(t_quiet)
    n.ccfg = dict(n.ccfg, quiet_hours={"start": "23:00", "end": "01:00"})
    assert n.in_quiet_hours(_dt.datetime(2026, 10, 28, 23, 30, tzinfo=_dt.timezone.utc).timestamp())  # 00:30 local
    assert not n.in_quiet_hours(_dt.datetime(2026, 10, 28, 3, 0, tzinfo=_dt.timezone.utc).timestamp())
    conn.close()


def _week_json(settings, end_day, verdict="NO-GO: niet alle criteria voldaan"):
    rep = {"kind": "week", "start_day": "2026-10-26", "end_day": end_day, "days": 7, "segments": [{
        "universe": {"graduations": 410, "covered_days": 6, "winners": 31, "outcome_unknown": 40},
        "hit_rate": {"decisions": 48, "hits": 9, "elapsed": 45, "unknown": 6, "pending": 3, "rate": 0.2},
        "recall": {"before_graduation": {"any": {"n": 11, "rate": 11 / 31}}},
        "lead_time": {"any_vs_graduation": {"median_s": 840}},
        "gate": {"verdict": verdict, "criteria": [
            {"key": "complete_days", "label": "minimaal 5 complete dagen (piekuren 12:00 tot 02:00 UTC gedekt)", "status": "voldaan"},
            {"key": "winners", "label": "minimaal 30 winnaars in het universum", "status": "voldaan"},
            {"key": "hit_rate_ci_low", "label": "ondergrens bootstrapinterval trefkans munt-observaties ≥ 0.15", "status": "niet voldaan"},
            {"key": "recall_before_graduation_ci_low", "label": "ondergrens bootstrapinterval recall vóór graduatie ≥ 0.25", "status": "te weinig data"},
        ]}}]}
    (settings.out_dir / "recall").mkdir(parents=True, exist_ok=True)
    (settings.out_dir / "recall" / f"week-{end_day}.json").write_text(json.dumps(rep))
    return rep


def test_weekly_accountability_message_exists_while_the_channel_is_off(tmp_path, monkeypatch):
    # preview only (the measurement week): the summary goes to the owner, says the channel is off
    settings = community_settings(tmp_path, monkeypatch, chat_id="", enabled="0", preview="1")
    conn, n = notifier_for(settings)
    monday_0645 = _dt.datetime(2026, 11, 2, 6, 45, tzinfo=_dt.timezone.utc).timestamp()
    _week_json(settings, "2026-11-01")
    assert n.build_community_weekly(monday_0645 - 60) == 0                                                   # 06:44: not yet
    assert n.build_community_weekly(_dt.datetime(2026, 11, 3, 7, 0, tzinfo=_dt.timezone.utc).timestamp()) == 0  # a Tuesday: never
    assert n.build_community_weekly(monday_0645) == 1
    row = conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "community_weekly_preview" and row["chat"] == OWNER and row["dedupe_key"] == "community_weekly_preview:2026-11-01"
    t = row["text"]
    assert t.startswith("<b>VOORBEELD community-bericht</b> (wekelijkse verantwoording)") and "Verantwoording week 26 okt 2026 t/m 1 nov 2026" in t
    assert "410 graduaties op 6 gedekte dagen, 31 winnaars" in t and "9 daarvan werden winnaar (20% van de 45 met verstreken 24 uur; 6 onbekend meegeteld, 3 nog bezig)" in t
    assert "11 vooraf gezien (35%), mediaan 14 minuten vóór graduatie" in t
    assert "• minimaal 30 winnaars in het universum: ✓ voldaan" in t and "✗ niet voldaan" in t and "? te weinig data" in t
    assert "<b>Uitkomst</b>: NO-GO: niet alle criteria voldaan. Dit bericht beslist niets" in t
    assert "Community-berichten deze week: geen (het kanaal staat uit tot na de meetweek)." in t and t.endswith(f"<i>{FOOTER}</i>")
    assert n.build_community_weekly(monday_0645 + 600) == 0  # once per week
    assert chats(conn) == {OWNER}
    conn.close()
    (tmp_path / "data" / "fart.db").unlink()
    # neither enabled nor preview: the builder exists and produces nothing
    settings2 = community_settings(tmp_path, monkeypatch, chat_id="", enabled="0", preview="0")
    conn2, n2 = notifier_for(settings2)
    _week_json(settings2, "2026-11-01")
    assert n2.build_community_weekly(monday_0645) == 0 and conn2.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    conn2.close()
    (tmp_path / "data" / "fart.db").unlink()
    # enabled: the community chat gets it, with the count of community messages of that week
    settings3 = community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="1", preview="0")
    conn3, n3 = notifier_for(settings3)
    week_start = _dt.datetime(2026, 10, 26, tzinfo=_dt.timezone.utc).timestamp()
    outbox.enqueue(conn3, "community", "community:901", COMMUNITY, "x", week_start + 3600)
    conn3.execute("UPDATE outbox SET status='sent', sent_at=? WHERE dedupe_key='community:901'", (week_start + 3700,))
    _week_json(settings3, "2026-11-01", verdict="GO: alle criteria voldaan")
    assert n3.build_community_weekly(monday_0645) == 1
    row = conn3.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "community_weekly" and row["chat"] == COMMUNITY and "Community-berichten deze week: 1." in row["text"]
    assert "VOORBEELD" not in row["text"] and "GO: alle criteria voldaan" in row["text"]
    conn3.close()


def test_week_summary_falls_back_to_a_fresh_week_report_without_a_json(tmp_path, monkeypatch):
    settings = community_settings(tmp_path, monkeypatch, chat_id="", enabled="0", preview="1")
    conn, n = notifier_for(settings)
    monday_0645 = _dt.datetime(2026, 11, 2, 6, 45, tzinfo=_dt.timezone.utc).timestamp()
    assert n.build_community_weekly(monday_0645) == 1  # built from the (empty) database: every day NIET GEDEKT
    t = conn.execute("SELECT text FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "Verantwoording week 26 okt 2026 t/m 1 nov 2026" in t and "0 graduaties op 0 gedekte dagen" in t and "te weinig data" in t
    conn.close()


def test_status_and_once_show_the_community_state(tmp_path, monkeypatch, capsys):
    from fart.cli import main

    settings = community_settings(tmp_path, monkeypatch, chat_id=COMMUNITY, enabled="0", preview="1")
    db.connect(settings.db_path).close()
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "telegram community: configured, disabled (TELEGRAM_COMMUNITY_ENABLED=0 or no chat id), preview to owner on" in out
    assert COMMUNITY not in out
    assert main(["--root", str(settings.root), "--quiet", "notify", "--once"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["enabled"] is True and s["community"] == {"enabled": False, "preview": True}
    assert s["built"]["community"] == 0 and s["built"]["community_weekly"] in (0, 1)  # 1 when the real clock is a Monday after 06:45 UTC


def test_env_example_names_the_community_settings_off():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    names = dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line)
    assert names["TELEGRAM_COMMUNITY_CHAT_ID"] == "" and names["TELEGRAM_COMMUNITY_ENABLED"] == "0" and names["TELEGRAM_COMMUNITY_PREVIEW"] == "0"
