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
    assert d.layout == "CreateEvent/full/12of12"
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
    assert d.parsed_fields == 6 and d.layout == "CreateEvent/partial/6of12"
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
