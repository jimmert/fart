import base64
import json

import pytest

from fart.chain import events as ev
from fart.chain.base58 import b58decode, b58encode, is_pubkey
from fart.chain.tx import create_from_tx, events_from_transaction, migration_from_tx


@pytest.fixture
def payloads(fixtures_dir):
    return json.loads((fixtures_dir / "events_payloads.json").read_text())


def test_base58_roundtrip():
    for raw in (b"\x00" * 32, bytes(range(32)), b"\x00\x01\x02", b"\xff" * 32, b""):
        assert b58decode(b58encode(raw)) == raw
    assert b58encode(b"\x00" * 32) == "1" * 32
    assert is_pubkey("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM")
    assert is_pubkey("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    assert not is_pubkey("0OIl")
    assert not is_pubkey("abc")


def test_discriminators_match_known_values():
    # the two prefixes everyone sees in pump.fun logs: "G3KpTd7rY3Y=" and "vdt/007mYe4="
    assert ev.event_discriminator("CreateEvent").hex() == "1b72a94ddeeb6376"
    assert ev.event_discriminator("TradeEvent").hex() == "bddb7fd34ee661ee"
    assert ev.event_discriminator("CompleteEvent").hex() == "5f72619cd42e9808"
    assert ev.ANCHOR_EVENT_CPI_DISC.hex() == "e445a52e51cb9a1d"


def test_decode_create_v2_full(payloads):
    d = ev.decode_event_bytes(base64.b64decode(payloads["create_v2"]))
    assert isinstance(d, ev.DecodedEvent)
    assert d.name == "CreateEvent"
    assert d.layout == "CreateEvent/partial/12of20"  # the synthetic fixture carries the 12 pre-2026 fields; the IDL lists 20
    exp = payloads["expected"]
    assert d.fields["mint"] == exp["mint"]
    assert d.fields["bonding_curve"] == exp["curve"]
    assert d.fields["creator"] == exp["creator"]
    assert d.fields["name"] == "Goldberg Cat" and d.fields["symbol"] == "GOLDCAT"
    assert d.fields["timestamp"] == exp["ts"]
    assert d.fields["virtual_sol_reserves"] == 30_000_000_000
    assert d.extra_bytes == 0 and not d.suspect


def test_decode_create_v1_partial_is_still_usable(payloads):
    d = ev.decode_event_bytes(base64.b64decode(payloads["create_v1"]))
    assert isinstance(d, ev.DecodedEvent)
    assert d.parsed_fields == 6 and d.layout == "CreateEvent/partial/6of20"
    assert d.fields["mint"] == payloads["expected"]["mint"]
    assert "creator" not in d.fields


def test_decode_trade_and_cpi_wrapped(payloads):
    plain = ev.decode_event_bytes(base64.b64decode(payloads["trade_dev"]))
    wrapped = ev.decode_event_bytes(base64.b64decode(payloads["cpi_wrapped_trade"]))
    for d in (plain, wrapped):
        assert isinstance(d, ev.DecodedEvent)
        assert d.name == "TradeEvent"
        assert d.fields["is_buy"] is True
        assert d.fields["sol_amount"] == 1_500_000_000
        assert d.fields["virtual_sol_reserves"] == 31_500_000_000
        assert d.parsed_fields == 16  # fee fields present, volume-tracking fields absent
    assert plain.fields == wrapped.fields


def test_decode_migration_and_complete(payloads):
    m = ev.decode_event_bytes(base64.b64decode(payloads["migration"]))
    assert isinstance(m, ev.DecodedEvent) and m.name == "CompletePumpAmmMigrationEvent"
    assert m.fields["pool"] == payloads["expected"]["pool"]
    assert m.fields["mint"] == payloads["expected"]["mint"]
    c = ev.decode_event_bytes(base64.b64decode(payloads["complete"]))
    assert isinstance(c, ev.DecodedEvent) and c.name == "CompleteEvent"


def test_malformed_payloads_never_raise(payloads):
    cases = {
        "set_params": "ignored_event",
        "unknown": "unknown_discriminator",
        "truncated": "truncated_or_drift",
        "bad_len": "truncated_or_drift",
    }
    for key, reason in cases.items():
        r = ev.decode_event_bytes(base64.b64decode(payloads[key]))
        assert isinstance(r, ev.Undecoded), key
        assert r.reason.startswith(reason), (key, r.reason)
    assert ev.decode_event_bytes(b"").reason == "too_short"
    assert ev.decode_event_bytes(b"\x01\x02").reason == "too_short"
    # random garbage of a plausible length
    r = ev.decode_event_bytes(bytes(range(256)))
    assert isinstance(r, ev.Undecoded)


def test_implausible_timestamp_marks_suspect(payloads):
    d = ev.decode_event_bytes(base64.b64decode(payloads["bad_ts"]))
    assert isinstance(d, ev.DecodedEvent)
    assert d.suspect and "timestamp" in d.suspect[0]
    assert d.layout.startswith("CompleteEvent/suspect")


def test_decode_logs_from_notification(fixtures_dir):
    note = json.loads((fixtures_dir / "logs_notification_create.json").read_text())
    logs = note["params"]["result"]["value"]["logs"]
    events, undecoded = ev.decode_logs(logs)
    assert [e.name for e in events] == ["CreateEvent", "TradeEvent"]
    assert undecoded == []


def test_decode_logs_mixed_bad_counts_everything(fixtures_dir):
    logs = json.loads((fixtures_dir / "logs_mixed_bad.json").read_text())["logs"]
    events, undecoded = ev.decode_logs(logs)
    names = [e.name for e in events]
    assert names == ["CreateEvent", "CompleteEvent"]  # v1 create + suspect-but-decodable complete
    summary = ev.summarize_undecoded(undecoded)
    assert summary == {"ignored_event": 1, "unknown_discriminator": 1, "truncated_or_drift": 2, "too_short": 1}


def test_events_from_transaction_logs_and_cpi(fixtures_dir, payloads):
    tx = json.loads((fixtures_dir / "tx_create.json").read_text())["result"]
    te = events_from_transaction(tx)
    assert te.slot == 370_000_001 and te.block_time == payloads["expected"]["ts"]
    # same events in logs and in CPI inner instructions -> deduplicated
    assert [e.name for e in te.events] == ["CreateEvent", "TradeEvent"]
    rec = create_from_tx(te)
    assert rec["mint"] == payloads["expected"]["mint"]
    assert rec["creator"] == payloads["expected"]["creator"]
    assert rec["dev_buy_sol"] == pytest.approx(1.5)
    assert rec["block_time"] == payloads["expected"]["ts"]
    assert rec["signature"] == tx["transaction"]["signatures"][0]


def test_events_from_transaction_cpi_only(fixtures_dir, payloads):
    tx = json.loads((fixtures_dir / "tx_create_cpi_only.json").read_text())["result"]
    te = events_from_transaction(tx)
    assert [e.name for e in te.events] == ["CreateEvent", "TradeEvent"]
    assert create_from_tx(te)["mint"] == payloads["expected"]["mint"]


def test_migration_from_transaction(fixtures_dir, payloads):
    tx = json.loads((fixtures_dir / "tx_migration.json").read_text())["result"]
    te = events_from_transaction(tx)
    m = migration_from_tx(te)
    assert m["mint"] == payloads["expected"]["mint"]
    assert m["pool_address"] == payloads["expected"]["pool"]
    assert m["dex"] == "pump-amm"
    assert m["block_time"] == payloads["expected"]["ts"] + 90


def test_events_from_transaction_tolerates_garbage():
    assert events_from_transaction(None).err == "no_transaction"
    te = events_from_transaction({"meta": {"logMessages": None, "innerInstructions": [{"instructions": [{"data": 5}, {"data": "0OIl"}]}]}})
    assert te.events == [] and te.signature is None
    assert create_from_tx(te) is None and migration_from_tx(te) is None


@pytest.mark.parametrize("name,kind", [("tx_create_real.json", "create"), ("tx_migration_real.json", "migrate")])
def test_real_recordings_if_present(fixtures_dir, name, kind):
    """Drop real getTransaction responses into tests/fixtures/ (see docs/PUMPFUN-EVENTS.md)."""
    path = fixtures_dir / name
    if not path.exists():
        pytest.skip(f"{name} not recorded yet (docs/PUMPFUN-EVENTS.md, step 5)")
    data = json.loads(path.read_text())
    tx = data.get("result", data)
    te = events_from_transaction(tx)
    assert te.signature and te.slot and te.block_time
    assert not te.undecoded or all(u.reason == "ignored_event" for u in te.undecoded), [u.reason for u in te.undecoded]
    if kind == "create":
        ev = te.first("CreateEvent")
        rec = create_from_tx(te)
        assert rec and rec["mint"] and rec["creator"]
    else:
        ev = te.first("CompletePumpAmmMigrationEvent")
        rec = migration_from_tx(te)
        assert rec and rec["mint"] and rec["pool_address"]
    assert ev is not None and not ev.suspect and ev.extra_bytes == 0, (ev.layout, ev.suspect, ev.extra_bytes)


# ----- milestone 2: pump-amm events, program attribution, classification -------------------------------
def test_decode_pump_amm_events(payloads):
    exp = payloads["expected"]
    b = ev.decode_event_bytes(base64.b64decode(payloads["amm_buy"]))
    assert isinstance(b, ev.DecodedEvent) and b.layout == "BuyEvent/partial/28of40" and not b.suspect  # IDL: 40 fields
    assert b.fields["pool"] == exp["pool"] and b.fields["user"] == exp["amm_buyer"]
    assert b.fields["quote_amount_in"] == 2_000_000_000 and b.fields["base_amount_out"] == 10_000_000_000_000
    assert b.fields["pool_quote_token_reserves"] == 100_000_000_000 and b.fields["timestamp"] == exp["ts"] + 600
    s = ev.decode_event_bytes(base64.b64decode(payloads["amm_sell"]))
    assert isinstance(s, ev.DecodedEvent) and s.layout == "SellEvent/partial/23of33" and s.fields["quote_amount_out"] == 800_000_000
    c = ev.decode_event_bytes(base64.b64decode(payloads["amm_create_pool"]))
    assert isinstance(c, ev.DecodedEvent) and c.layout == "CreatePoolEvent/partial/20of24"
    assert c.fields["quote_mint"] == "So11111111111111111111111111111111111111112"
    assert c.fields["base_mint"] == exp["mint"] and c.fields["pool"] == exp["pool"] and c.fields["index"] == 0 and c.fields["pool_bump"] == 254
    # basis points above 10,000 mark a layout suspect
    raw = base64.b64decode(payloads["amm_buy"])
    off = 8 + 8 * 8  # lp_fee_basis_points
    bad = raw[:off] + (50_000).to_bytes(8, "little") + raw[off + 8:]
    d = ev.decode_event_bytes(bad)
    assert isinstance(d, ev.DecodedEvent) and d.suspect and "lp_fee_basis_points" in d.suspect[0]


def test_attribute_program_data_nested_frames(fixtures_dir):
    data = json.loads((fixtures_dir / "logs_nested_jupiter.json").read_text())
    attributed = ev.attribute_program_data(data["logs"])
    assert [p for p, _ in attributed] == [data["programs"]["pump"], data["programs"]["jupiter"]]
    events, undecoded = ev.decode_logs(data["logs"])
    assert [e.name for e in events] == ["TradeEvent"] and events[0].program == data["programs"]["pump"]
    assert len(undecoded) == 1 and undecoded[0].program == data["programs"]["jupiter"] and undecoded[0].reason == "unknown_discriminator"
    c = ev.classify_discriminator(undecoded[0].discriminator_hex)
    assert c["status"] == "other_program" and "jupiter_v6" in c["programs"] and c["name"] == "SwapEvent"
    # garbage lines and unbalanced frames never raise
    assert ev.attribute_program_data(["Program data: !!!", "Program X success", None, 5]) == [(None, b"")]
    assert ev.extract_program_data(["Program data: " + base64.b64encode(b"abc").decode()]) == [b"abc"]


def test_classify_discriminators_seen_on_the_vps():
    expect = {
        "b1310cd2a076a774": ("CreatePoolEvent", "decoded"),
        "67f4521f2cf57777": ("BuyEvent", "decoded"),
        "40c6cde8260871e2": ("SwapEvent", "other_program"),
        "e1ca49af932ba096": ("Traded", "other_program"),
        "bddb7fd34ee661ee": ("TradeEvent", "decoded"),
        "e445a52e51cb9a1d": (None, "emit_cpi_tag"),
        # the IDLs of 26 Oct 2026 name three of the VPS's "unknown" discriminators
        "ae7c4af90451f611": ("InitBoostEvent", "ignored"),
        "9ba768dcd56cf303": ("MigrateBondingCurveCreatorEvent", "ignored"),
        "a537817004b3ca28": ("DistributeCreatorFeesEvent", "ignored"),
        "8569aac8b874fb58": (None, "unknown"),
        "zz": (None, "unknown"),
    }
    for disc, (name, status) in expect.items():
        c = ev.classify_discriminator(disc)
        assert c["status"] == status, disc
        if name:
            assert c["name"] == name
    table = ev.discriminator_table()
    assert {"CreateEvent", "BuyEvent", "SellEvent", "CreatePoolEvent"} <= {r["name"] for r in table}
    assert ev.event_discriminator("SellEvent").hex() == "3e2f370aa503dc2a"


def test_pump_amm_trade_records_resolve_mint_through_the_pool(payloads):
    """Milestone 3: the mint of a pump-amm swap comes ONLY from the pool -> base-mint mapping, never from the subscription."""
    from fart.ingest import store

    exp = payloads["expected"]
    wsol = "So11111111111111111111111111111111111111112"
    events = [ev.decode_event_bytes(base64.b64decode(payloads[k])) for k in ("amm_buy", "amm_sell", "amm_create_pool")]
    # CreatePoolEvent in the same transaction maps the pool
    recs, rejected = store.pump_amm_trade_records(events, "sig", 5, None, wsol)
    assert rejected == [] and [r["is_buy"] for r in recs] == [True, False]
    assert recs[0]["signature"] == "sig:0" and recs[1]["signature"] == "sig:1"
    assert recs[0]["sol"] == pytest.approx(2.0) and recs[0]["tokens"] == pytest.approx(10_000_000.0) and recs[0]["mint"] == exp["mint"]
    assert recs[0]["block_time"] == exp["ts"] + 600 and recs[0]["slot"] == 5 and recs[0]["buyer"] == exp["amm_buyer"] and recs[0]["pool"] == exp["pool"]
    swaps = events[:2]
    # no CreatePoolEvent and no lookup: rejected, never guessed
    recs2, rejected2 = store.pump_amm_trade_records(swaps, "sig", 5, None, wsol)
    assert recs2 == [] and rejected2 == ["pool_unknown", "pool_unknown"]
    # lookup through migrations.pool_address (a callable)
    recs3, rejected3 = store.pump_amm_trade_records(swaps, "sig", 5, lambda pool: "MINTX" if pool == exp["pool"] else None, wsol)
    assert rejected3 == [] and {r["mint"] for r in recs3} == {"MINTX"}
    assert store.pump_amm_trade_records(swaps, "sig", 5, lambda pool: None, wsol)[1] == ["pool_unknown", "pool_unknown"]
    # a pool quoted in something other than WSOL is never a trade source (its amounts are not lamports)
    recs4, rejected4 = store.pump_amm_trade_records(events, "sig", 5, None, "USDCmint111111111111111111111111111111111111")
    assert recs4 == [] and rejected4 == ["quote_not_wsol", "quote_not_wsol"]
    assert store.pool_base_mints(events, wsol) == {exp["pool"]: exp["mint"]}
    assert store.pool_base_mints(events, "OtherQuote") == {exp["pool"]: None}
    assert store.pool_from_create_pool_event(events) == {"mint": exp["mint"], "pool_address": exp["pool"], "dex": "pump-amm", "quote_mint": wsol}
