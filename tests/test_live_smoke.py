"""Opt-in live smoke test: FART_LIVE=1 make live  (or: FART_LIVE=1 pytest tests/test_live_smoke.py -s)

Connects ONE websocket to PumpPortal (no key) for up to 60 seconds, subscribes to new tokens and
migrations, and checks that messages arrive and normalize. Writes only into a temporary root.
Skips cleanly when FART_LIVE is unset or when there is no outbound network (10 s connect timeout).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time

import pytest

LIVE = os.environ.get("FART_LIVE") == "1"
DURATION_S = float(os.environ.get("FART_LIVE_SECONDS", "60"))

pytestmark = pytest.mark.skipif(not LIVE, reason="set FART_LIVE=1 to run the 60 s PumpPortal smoke test")


async def _collect(url: str, duration_s: float) -> list[str]:
    import websockets

    msgs: list[str] = []
    try:
        ws = await asyncio.wait_for(websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=10), timeout=10)
    except (OSError, asyncio.TimeoutError, socket.gaierror) as e:
        pytest.skip(f"no outbound network to PumpPortal: {type(e).__name__}: {e}")
    except Exception as e:  # proxy refusals, TLS, handshake errors
        pytest.skip(f"cannot reach PumpPortal: {type(e).__name__}: {e}")
    async with ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        await ws.send(json.dumps({"method": "subscribeMigration"}))
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            try:
                text = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            msgs.append(text if isinstance(text, str) else text.decode("utf-8", "replace"))
        try:
            await ws.send(json.dumps({"method": "unsubscribeNewToken"}))
        except Exception:
            pass
    return msgs


def test_pumpportal_live_60s(settings, conn, capsys):
    from fart.ingest import store
    from fart.ingest.pumpportal import PumpPortalClient
    from fart.ingest.raw import RawWriter
    from fart.net.budget import Budget

    msgs = asyncio.run(_collect(settings.pumpportal_wss_url, DURATION_S))
    budget = Budget.from_settings(conn, settings)
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    pp = PumpPortalClient(conn, budget, raw, settings.pumpportal_wss_url, settings.config("ingest")["pumpportal"],
                          settings.config("engine")["watchlist"], connect=lambda *a, **k: None)
    kinds: dict[str, int] = {}
    for m in msgs:
        k = pp.handle_text(m, time.time())
        kinds[k] = kinds.get(k, 0) + 1
    raw.flush()
    coins = conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    sample = None
    for m in msgs:
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            continue
        if store.classify_pumpportal(d) == "create":
            sample = sorted(d.keys())
            break
    with capsys.disabled():
        print(f"\nlive: {len(msgs)} messages in {DURATION_S:.0f}s; kinds={kinds}; coins stored={coins}; create message keys={sample}")
    assert msgs, "PumpPortal delivered no messages in the window"
    assert kinds.get("unparseable", 0) == 0
    # pump.fun creates several coins per minute; one minute without a single create would be odd but not impossible
    if coins == 0:
        pytest.xfail("no create message within the window (PumpPortal quiet?); inspect the printed kinds")
