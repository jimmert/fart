"""Generate SYNTHETIC pump.fun fixtures from the documented layouts (docs/PUMPFUN-EVENTS.md).

These are not recorded from mainnet (the build sandbox had no network). Replace or add real
recordings at deploy; the tests are written so that a real `getTransaction` response of a
create and of a migration can be dropped in as tx_create_real.json / tx_migration_real.json.

Run: python3 tests/fixtures/gen_fixtures.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from fart.chain.base58 import b58decode, b58encode  # noqa: E402
from fart.chain.events import ANCHOR_EVENT_CPI_DISC as ANCHOR_CPI  # noqa: E402


def disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


def pk(seed: str) -> str:
    return b58encode(hashlib.sha256(seed.encode()).digest())


def enc(fields: list[tuple[str, object]]) -> bytes:
    out = b""
    for t, v in fields:
        if t == "string":
            b = str(v).encode()
            out += struct.pack("<I", len(b)) + b
        elif t == "pubkey":
            out += b58decode(str(v))
        elif t == "u64":
            out += struct.pack("<Q", int(v))
        elif t == "i64":
            out += struct.pack("<q", int(v))
        elif t == "bool":
            out += b"\x01" if v else b"\x00"
        else:
            raise ValueError(t)
    return out


MINT = pk("mint-1")
CURVE = pk("curve-1")
CREATOR = pk("creator-1")
POOL = pk("pool-1")
TS = 1_761_480_000  # 2025-10-26T12:00:00Z-ish synthetic chain time

create_v2 = disc("CreateEvent") + enc([
    ("string", "Goldberg Cat"), ("string", "GOLDCAT"), ("string", "https://ipfs.io/ipfs/QmSyntheticUri"),
    ("pubkey", MINT), ("pubkey", CURVE), ("pubkey", CREATOR), ("pubkey", CREATOR), ("i64", TS),
    ("u64", 1_073_000_000_000_000), ("u64", 30_000_000_000), ("u64", 793_100_000_000_000), ("u64", 1_000_000_000_000_000),
])
create_v1 = disc("CreateEvent") + enc([
    ("string", "Old Layout"), ("string", "OLD"), ("string", "https://example.invalid/old.json"),
    ("pubkey", MINT), ("pubkey", CURVE), ("pubkey", CREATOR),
])
# dev buy of 1.5 SOL in the same tx
trade_dev = disc("TradeEvent") + enc([
    ("pubkey", MINT), ("u64", 1_500_000_000), ("u64", 51_000_000_000_000), ("bool", True), ("pubkey", CREATOR), ("i64", TS),
    ("u64", 31_500_000_000), ("u64", 1_022_000_000_000_000), ("u64", 1_500_000_000), ("u64", 742_100_000_000_000),
    ("pubkey", pk("fee-recipient")), ("u64", 95), ("u64", 14_250_000), ("pubkey", CREATOR), ("u64", 5), ("u64", 750_000),
])
migration = disc("CompletePumpAmmMigrationEvent") + enc([
    ("pubkey", pk("migrator")), ("pubkey", MINT), ("u64", 206_900_000_000_000), ("u64", 84_990_000_000), ("u64", 150_000_000),
    ("pubkey", CURVE), ("i64", TS + 90), ("pubkey", POOL),
])
complete = disc("CompleteEvent") + enc([("pubkey", pk("buyer-9")), ("pubkey", MINT), ("pubkey", CURVE), ("i64", TS + 80)])
set_params = disc("SetParamsEvent") + b"\x00" * 40
unknown = hashlib.sha256(b"event:SomethingNew").digest()[:8] + b"\x01" * 20
truncated = create_v2[: 8 + 4 + 12]  # cuts inside the name string
bad_len = disc("CreateEvent") + struct.pack("<I", 999_999) + b"abc"
bad_ts = disc("CompleteEvent") + enc([("pubkey", pk("x")), ("pubkey", MINT), ("pubkey", CURVE), ("i64", 42)])


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

logs_create = [
    f"Program {PROGRAM} invoke [1]",
    "Program log: Instruction: Create",
    "Program 11111111111111111111111111111111 invoke [2]",
    "Program 11111111111111111111111111111111 success",
    f"Program data: {b64(create_v2)}",
    "Program log: Instruction: Buy",
    f"Program data: {b64(trade_dev)}",
    f"Program {PROGRAM} consumed 123456 of 400000 compute units",
    f"Program {PROGRAM} success",
]
logs_migration = [
    f"Program {PROGRAM} invoke [1]",
    "Program log: Instruction: Migrate",
    f"Program data: {b64(migration)}",
    f"Program {PROGRAM} success",
]
logs_mixed_bad = [
    f"Program data: {b64(set_params)}",
    f"Program data: {b64(unknown)}",
    f"Program data: {b64(truncated)}",
    f"Program data: {b64(bad_len)}",
    "Program data: !!!not-base64!!!",
    f"Program data: {b64(create_v1)}",
    f"Program data: {b64(bad_ts)}",
]

SIG_CREATE = b58encode(hashlib.sha256(b"sig-create").digest() * 2)
SIG_MIGR = b58encode(hashlib.sha256(b"sig-migr").digest() * 2)


def logs_notification(sig: str, logs: list[str], slot: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {"context": {"slot": slot}, "value": {"signature": sig, "err": None, "logs": logs}},
            "subscription": 1,
        },
    }


def tx_response(sig: str, logs: list[str], slot: int, block_time: int, cpi_payloads: list[bytes], accounts: list[str]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "slot": slot,
            "blockTime": block_time,
            "meta": {
                "err": None,
                "fee": 5000,
                "logMessages": logs,
                "innerInstructions": [
                    {
                        "index": 0,
                        "instructions": [
                            {"programId": PROGRAM, "accounts": [pk("event-authority")], "data": b58encode(ANCHOR_CPI + p)}
                            for p in cpi_payloads
                        ],
                    }
                ],
                "preBalances": [],
                "postBalances": [],
            },
            "transaction": {
                "signatures": [sig],
                "message": {
                    "accountKeys": [{"pubkey": a, "signer": i == 0, "writable": True} for i, a in enumerate(accounts)],
                    "instructions": [{"programId": PROGRAM, "accounts": accounts, "data": "synthetic"}],
                },
            },
        },
    }


# PumpPortal message shapes (from PumpPortal docs/observed streams; UNVERIFIED exact field set)
pp_new_token = {
    "signature": SIG_CREATE, "mint": MINT, "traderPublicKey": CREATOR, "txType": "create",
    "initialBuy": 51000000.0, "solAmount": 1.5, "bondingCurveKey": CURVE,
    "vTokensInBondingCurve": 1022000000.0, "vSolInBondingCurve": 31.5, "marketCapSol": 30.82,
    "name": "Goldberg Cat", "symbol": "GOLDCAT", "uri": "https://ipfs.io/ipfs/QmSyntheticUri", "pool": "pump",
}
pp_trade = {
    "signature": b58encode(hashlib.sha256(b"sig-trade").digest() * 2), "mint": MINT, "traderPublicKey": pk("buyer-2"),
    "txType": "buy", "tokenAmount": 20000000.0, "solAmount": 0.65, "newTokenBalance": 20000000.0,
    "bondingCurveKey": CURVE, "vTokensInBondingCurve": 1002000000.0, "vSolInBondingCurve": 32.15,
    "marketCapSol": 32.09, "pool": "pump",
}
pp_sell = dict(pp_trade, signature=b58encode(hashlib.sha256(b"sig-sell").digest() * 2), txType="sell",
               traderPublicKey=pk("buyer-2"), tokenAmount=5000000.0, solAmount=0.16, newTokenBalance=15000000.0,
               vTokensInBondingCurve=1007000000.0, vSolInBondingCurve=31.99)
pp_migration = {"signature": SIG_MIGR, "mint": MINT, "txType": "migrate", "pool": "pump-amm"}
pp_junk = {"message": "Successfully subscribed to token creation events."}

fixtures = {
    "events_payloads.json": {
        "_note": "synthetic; base64 of discriminator+borsh fields",
        "create_v2": b64(create_v2), "create_v1": b64(create_v1), "trade_dev": b64(trade_dev),
        "migration": b64(migration), "complete": b64(complete), "set_params": b64(set_params),
        "unknown": b64(unknown), "truncated": b64(truncated), "bad_len": b64(bad_len), "bad_ts": b64(bad_ts),
        "cpi_wrapped_trade": b64(ANCHOR_CPI + trade_dev),
        "expected": {"mint": MINT, "curve": CURVE, "creator": CREATOR, "pool": POOL, "ts": TS},
    },
    "logs_notification_create.json": logs_notification(SIG_CREATE, logs_create, 370_000_001),
    "logs_notification_migration.json": logs_notification(SIG_MIGR, logs_migration, 370_000_900),
    "logs_mixed_bad.json": {"logs": logs_mixed_bad},
    "tx_create.json": tx_response(SIG_CREATE, logs_create, 370_000_001, TS, [create_v2, trade_dev], [CREATOR, MINT, CURVE]),
    "tx_create_cpi_only.json": tx_response(SIG_CREATE, ["Program log: Instruction: Create"], 370_000_001, TS, [create_v2, trade_dev], [CREATOR, MINT, CURVE]),
    "tx_migration.json": tx_response(SIG_MIGR, logs_migration, 370_000_900, TS + 90, [migration], [pk("migrator"), MINT, POOL]),
    "pumpportal_messages.json": {"new_token": pp_new_token, "trade": pp_trade, "sell": pp_sell, "migration": pp_migration, "junk": pp_junk},
}

for name, obj in fixtures.items():
    (HERE / name).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {len(fixtures)} fixtures to {HERE}")
