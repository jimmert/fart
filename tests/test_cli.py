import json

from fart import db
from fart.cli import main


def test_status_without_database(settings, capsys):
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "database does not exist yet" in out
    assert "PUBLIC fallback" in out


def test_status_and_coverage_on_empty_database(settings, capsys):
    db.connect(settings.db_path).close()
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["coins"] == 0
    assert set(data["budget"]) == set(settings.config("budget")["sources"])
    assert data["rules_version"] == "test-rules-v1"
    assert main(["--root", str(settings.root), "--quiet", "coverage", "--hours", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 3  # header + 2 hours


def test_status_output_contains_no_secret(settings, capsys, monkeypatch):
    (settings.root / ".env").write_text("RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER-NOT-A-REAL-KEY\n")
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    assert "PLACEHOLDER-NOT-A-REAL-KEY" not in capsys.readouterr().out


def test_events_classify_and_chain_pda(settings, capsys):
    root = str(settings.root)
    assert main(["--root", root, "--quiet", "events", "classify"]) == 0
    assert "no database yet" in capsys.readouterr().out
    conn = db.connect(settings.db_path)
    assert main(["--root", root, "--quiet", "events", "classify"]) == 0
    assert "no undecoded payloads" in capsys.readouterr().out
    conn.execute("INSERT INTO raw_undecoded(received_at, source, signature, slot, discriminator, payload_b64, reason, program)"
                 " VALUES(1, 'rpc_logs', 'sigA', 1, 'b1310cd2a076a774', 'AA==', 'unknown_discriminator', 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA')")
    conn.execute("INSERT INTO raw_undecoded(received_at, source, signature, slot, discriminator, payload_b64, reason)"
                 " VALUES(2, 'rpc_logs', 'sigB', 2, 'ae7c4af90451f611', 'AA==', 'unknown_discriminator')")
    conn.execute("INSERT INTO raw_undecoded(received_at, source, signature, slot, discriminator, payload_b64, reason)"
                 " VALUES(3, 'rpc_logs', 'sigC', 3, 'ae7c4af90451f611', 'AA==', 'unknown_discriminator')")
    conn.close()
    assert main(["--root", root, "--quiet", "events", "classify"]) == 0
    out = capsys.readouterr().out
    assert "CreatePoolEvent" in out and "pump-amm" in out and "ae7c4af90451f611" in out
    assert main(["--root", root, "--quiet", "--json", "events", "classify"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert items[0]["discriminator"] == "ae7c4af90451f611" and items[0]["n"] == 2 and items[0]["status"] == "unknown"
    assert items[1]["name"] == "CreatePoolEvent" and items[1]["program_label"] == "pump-amm"
    mint = "CcnpRLK4pnA35KjAd2aGZr4GAat16h8oTTKQq9pSZgfe"
    assert main(["--root", root, "--quiet", "--json", "chain", "pda", mint]) == 0
    pda = json.loads(capsys.readouterr().out)
    assert len(pda["bonding_curve_pda"]) >= 32 and pda["db_bonding_curve"] is None
    assert main(["--root", root, "--quiet", "chain", "pda", "not-a-key"]) == 2


def test_chain_global_offline_exits_2(settings, capsys, monkeypatch):
    import urllib.error

    from fart.net import http as nh

    def fail(req, timeout=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(nh.urllib.request, "urlopen", fail)
    assert main(["--root", str(settings.root), "--quiet", "chain", "global"]) == 2
    assert "status=unavailable" in capsys.readouterr().out


def test_status_json_has_milestone2_fields(settings, capsys):
    db.connect(settings.db_path).close()
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    s = data["settings"]
    assert s["pumpportal_api_key"] == "not configured" and s["x_bearer_token"] == "not configured" and s["telegram"] == "not configured"
    assert data["cu_by_method_today"] == [] and data["top_subscriptions"] == [] and data["outbox"] == {} and data["x_spend_today"] == []
    assert data["global_check"]["status"] is None and data["schema_version"] == 2
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "trades: rpc only (no key)" in out and "not configured" in out
