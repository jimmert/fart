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
