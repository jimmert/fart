"""SQLite access: WAL mode, schema versioning via PRAGMA user_version, idempotent migrations,
and the ONE way to write: `write_tx()`.

Time columns: `slot`/`block_time` are chain time, `received_at`/`created_at`/`measured_at`
are our clock. `decisions` and `outcomes` are append-only (enforced by triggers); so are the milestone-6
shadow tables `shadow_decisions` and `shadow_confirmations`. The milestone-7 planner table `x_keys` is the one
X table that is updated in place (one row per confirmation key episode: counts, confirmation, stop reason).

Write rules (milestone 3, after the VPS lock storm of 26 Oct 2026):

* Every transaction that writes starts with `BEGIN IMMEDIATE` (`immediate()` / `write_tx()`).
  A plain `BEGIN` that reads first and writes later fails at once with SQLITE_BUSY_SNAPSHOT in WAL
  mode as soon as any other connection committed in between; the busy handler is *not* consulted
  for that error, so `busy_timeout` never helped. `BEGIN IMMEDIATE` takes the write lock up front
  and waits (busy handler, `BUSY_TIMEOUT_MS`) instead of failing.
* Keep write transactions short: compute features, cluster, decode, call the network *outside*
  (plain SELECTs or a closed `read_tx()` snapshot), then write in one short IMMEDIATE transaction.
* `write_tx()` retries a busy/locked error up to `WRITE_RETRY_ATTEMPTS` times with a short jitter
  instead of failing the whole tick or handler. The function it runs must only touch the database
  (it may run more than once).
* A write that still fails is counted (`record_failure()`: `<key>` and `<key>.<day>` in kv) so
  `fart status` and the daily report show it.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

from .log import get_logger
from .util import day_key

log = get_logger("db")

SCHEMA_VERSION = 10

BUSY_TIMEOUT_MS = 15000
WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_BASE_S = 0.05
WRITE_RETRY_MAX_S = 1.0

# Quote / system mints that can never be a pump.fun coin. PumpPortal migration messages put USDC and
# PUMP in their `mint` field on 26 Oct 2026 (docs/PUMPFUN-EVENTS.md); schema v3 removes the rows they
# caused. The runtime filter reads the same list from config/pumpfun.json -> system_mints (tests keep
# both in sync).
SCHEMA_V3_SYSTEM_MINTS = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",    # WSOL
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",    # PUMP
)

# One migration per schema version. Each must be idempotent (CREATE ... IF NOT EXISTS, guarded
# ALTER TABLE). A migration is either an SQL script or a callable taking the connection; both run
# inside one transaction together with the PRAGMA user_version bump.
MIGRATIONS: dict[int, "str | Callable[[sqlite3.Connection], None]"] = {
    1: """
    CREATE TABLE IF NOT EXISTS coins(
        mint            TEXT PRIMARY KEY,
        chain           TEXT NOT NULL DEFAULT 'solana',
        platform        TEXT NOT NULL DEFAULT 'pump',
        name            TEXT,
        symbol          TEXT,
        uri             TEXT,
        creator         TEXT,
        bonding_curve   TEXT,
        signature       TEXT,
        slot            INTEGER,
        block_time      REAL,
        received_at     REAL NOT NULL,
        dev_buy_sol     REAL,
        initial_mcap_sol REAL,
        source          TEXT NOT NULL,
        sources         TEXT NOT NULL DEFAULT '[]',
        raw_ref         TEXT
    );
    CREATE INDEX IF NOT EXISTS coins_received_at ON coins(received_at);
    CREATE INDEX IF NOT EXISTS coins_block_time ON coins(block_time);
    CREATE INDEX IF NOT EXISTS coins_creator ON coins(creator);
    CREATE INDEX IF NOT EXISTS coins_signature ON coins(signature);

    CREATE TABLE IF NOT EXISTS migrations(
        mint            TEXT PRIMARY KEY,
        slot            INTEGER,
        block_time      REAL,
        received_at     REAL NOT NULL,
        signature       TEXT,
        pool_address    TEXT,
        dex             TEXT,
        source          TEXT NOT NULL,
        sources         TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS migrations_received_at ON migrations(received_at);
    CREATE INDEX IF NOT EXISTS migrations_block_time ON migrations(block_time);

    CREATE TABLE IF NOT EXISTS trades(
        mint            TEXT NOT NULL,
        slot            INTEGER,
        block_time      REAL,
        received_at     REAL NOT NULL,
        signature       TEXT NOT NULL,
        buyer           TEXT,
        is_buy          INTEGER NOT NULL,
        sol             REAL NOT NULL,
        tokens          REAL NOT NULL,
        virtual_sol     REAL,
        virtual_token   REAL,
        source          TEXT NOT NULL,
        PRIMARY KEY(signature, mint)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS trades_mint_received ON trades(mint, received_at);

    CREATE TABLE IF NOT EXISTS watchlist(
        mint            TEXT PRIMARY KEY,
        since           REAL NOT NULL,
        reason          TEXT,
        until           REAL,
        active          INTEGER NOT NULL DEFAULT 1,
        last_trade_at   REAL
    );

    CREATE TABLE IF NOT EXISTS coverage(
        hour                 TEXT PRIMARY KEY,
        creates_pp           INTEGER,
        creates_rpc          INTEGER,
        creates_union        INTEGER,
        grads                INTEGER,
        grads_without_create INTEGER,
        gaps_json            TEXT,
        complete             INTEGER,
        computed_at          REAL
    );
    CREATE TABLE IF NOT EXISTS coverage_gaps(
        id      INTEGER PRIMARY KEY,
        start   REAL NOT NULL,
        end     REAL,
        source  TEXT NOT NULL,
        reason  TEXT
    );
    CREATE INDEX IF NOT EXISTS coverage_gaps_start ON coverage_gaps(start);

    CREATE TABLE IF NOT EXISTS features_asof(
        id            INTEGER PRIMARY KEY,
        kind          TEXT NOT NULL,
        key           TEXT NOT NULL,
        mint          TEXT,
        as_of         REAL NOT NULL,
        computed_at   REAL NOT NULL,
        rules_version TEXT NOT NULL,
        json          TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS features_asof_key ON features_asof(kind, key, as_of);

    CREATE TABLE IF NOT EXISTS decisions(
        id            INTEGER PRIMARY KEY,
        as_of         REAL NOT NULL,
        created_at    REAL NOT NULL,
        kind          TEXT NOT NULL,
        mint          TEXT,
        term          TEXT,
        features_json TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        verdict       TEXT NOT NULL,
        reasons_json  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS decisions_as_of ON decisions(as_of);
    CREATE INDEX IF NOT EXISTS decisions_mint ON decisions(mint);
    CREATE INDEX IF NOT EXISTS decisions_term ON decisions(term);
    CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
        BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
        BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;

    CREATE TABLE IF NOT EXISTS outcomes(
        id             INTEGER PRIMARY KEY,
        decision_id    INTEGER,
        subject        TEXT NOT NULL,
        mint           TEXT NOT NULL,
        anchor_time    REAL NOT NULL,
        horizon_s      INTEGER NOT NULL,
        measured_at    REAL NOT NULL,
        peak_mcap_usd  REAL,
        peak_src       TEXT,
        volume_usd     REAL,
        hit_100k       INTEGER,
        bundled        INTEGER,
        sol_usd        REAL,
        json           TEXT,
        method_version TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS outcomes_subject ON outcomes(subject, mint, horizon_s);
    CREATE INDEX IF NOT EXISTS outcomes_decision ON outcomes(decision_id);
    CREATE INDEX IF NOT EXISTS outcomes_anchor ON outcomes(anchor_time);
    CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes
        BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
        BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;

    CREATE TABLE IF NOT EXISTS paper(
        id             INTEGER PRIMARY KEY,
        decision_id    INTEGER NOT NULL,
        entry_as_of    REAL,
        entry_price    REAL,
        size_sol       REAL,
        fill_price     REAL,
        slippage_bps   REAL,
        fees           REAL,
        exit_as_of     REAL,
        exit_price     REAL,
        exit_reason    TEXT,
        pnl            REAL,
        json           TEXT,
        method_version TEXT NOT NULL,
        computed_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS paper_decision ON paper(decision_id);

    CREATE TABLE IF NOT EXISTS api_ledger(
        id             INTEGER PRIMARY KEY,
        ts             REAL NOT NULL,
        source         TEXT NOT NULL,
        method         TEXT NOT NULL,
        cost           REAL NOT NULL DEFAULT 0,
        ok             INTEGER,
        status         INTEGER,
        latency_ms     REAL,
        blocked_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS api_ledger_source_ts ON api_ledger(source, ts);

    CREATE TABLE IF NOT EXISTS rate_buckets(
        source     TEXT PRIMARY KEY,
        tokens     REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS health(
        source          TEXT PRIMARY KEY,
        last_ok         REAL,
        last_err        REAL,
        last_err_msg    TEXT,
        ok_count        INTEGER NOT NULL DEFAULT 0,
        err_count       INTEGER NOT NULL DEFAULT 0,
        blocked_count   INTEGER NOT NULL DEFAULT 0,
        consecutive_err INTEGER NOT NULL DEFAULT 0,
        latency_ms_ewma REAL,
        backoff_until   REAL,
        updated_at      REAL
    );

    CREATE TABLE IF NOT EXISTS kv(
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at REAL
    );

    CREATE TABLE IF NOT EXISTS mcap_samples(
        mint           TEXT NOT NULL,
        ts             REAL NOT NULL,
        src            TEXT NOT NULL,
        mcap_usd       REAL,
        fdv_usd        REAL,
        liquidity_usd  REAL,
        volume_h24_usd REAL,
        price_usd      REAL,
        sol_usd        REAL,
        json           TEXT,
        PRIMARY KEY(mint, ts, src)
    ) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS raw_undecoded(
        id            INTEGER PRIMARY KEY,
        received_at   REAL NOT NULL,
        source        TEXT NOT NULL,
        signature     TEXT,
        slot          INTEGER,
        discriminator TEXT,
        payload_b64   TEXT,
        reason        TEXT
    );

    CREATE TABLE IF NOT EXISTS enrich_queue(
        key          TEXT NOT NULL,
        kind         TEXT NOT NULL,
        requested_at REAL NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_attempt REAL,
        done         INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        PRIMARY KEY(key, kind)
    );
    """,
}

# ----- schema version 2 (milestone 2) --------------------------------------------------------------
# New tables for the social layer (X), the Telegram outbox, per-mint websocket subscriptions and
# the X spend ledger; new columns on coverage_gaps (sweep recovery) and raw_undecoded (emitting
# program). Every statement is idempotent so a half-applied migration can simply be re-run. All
# milestone-1 tables and rows are untouched.
SCHEMA_V2_SQL = [
    """CREATE TABLE IF NOT EXISTS social_posts(
        id             TEXT PRIMARY KEY,
        platform       TEXT NOT NULL DEFAULT 'x',
        author         TEXT,
        author_id      TEXT,
        text           TEXT,
        created_at     REAL,
        received_at    REAL NOT NULL,
        likes          INTEGER,
        quotes         INTEGER,
        replies        INTEGER,
        impressions    INTEGER,
        kind           TEXT NOT NULL,
        followers      INTEGER,
        query_key      TEXT,
        fast           INTEGER NOT NULL DEFAULT 0,
        fast_at        REAL,
        refreshed_at   REAL,
        likes_per_min  REAL,
        quotes_per_min REAL,
        json           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS social_posts_received ON social_posts(received_at)",
    "CREATE INDEX IF NOT EXISTS social_posts_kind_created ON social_posts(kind, created_at)",
    "CREATE INDEX IF NOT EXISTS social_posts_query_key ON social_posts(query_key)",
    """CREATE TABLE IF NOT EXISTS social_post_metrics(
        id          TEXT NOT NULL,
        ts          REAL NOT NULL,
        likes       INTEGER,
        quotes      INTEGER,
        replies     INTEGER,
        impressions INTEGER,
        PRIMARY KEY(id, ts)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS social_counts(
        key         TEXT NOT NULL,
        kind        TEXT NOT NULL,
        ts          REAL NOT NULL,
        n_last_hour INTEGER,
        n_24h       INTEGER,
        query       TEXT,
        PRIMARY KEY(key, ts)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS social_counts_ts ON social_counts(ts)",
    """CREATE TABLE IF NOT EXISTS x_spend(
        id   INTEGER PRIMARY KEY,
        day  TEXT NOT NULL,
        kind TEXT NOT NULL,
        n    INTEGER NOT NULL,
        usd  REAL NOT NULL,
        ts   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS x_spend_day_kind ON x_spend(day, kind)",
    """CREATE TABLE IF NOT EXISTS x_dedupe(
        day  TEXT NOT NULL,
        kind TEXT NOT NULL,
        key  TEXT NOT NULL,
        PRIMARY KEY(day, kind, key)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS outbox(
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        dedupe_key  TEXT NOT NULL UNIQUE,
        chat        TEXT NOT NULL,
        text        TEXT NOT NULL,
        created_at  REAL NOT NULL,
        attempts    INTEGER NOT NULL DEFAULT 0,
        next_try_at REAL,
        status      TEXT NOT NULL DEFAULT 'pending',
        message_id  INTEGER,
        error       TEXT,
        sent_at     REAL
    )""",
    "CREATE INDEX IF NOT EXISTS outbox_status_next ON outbox(status, next_try_at)",
    "CREATE INDEX IF NOT EXISTS outbox_kind_created ON outbox(kind, created_at)",
    """CREATE TABLE IF NOT EXISTS ws_subscriptions(
        id                   INTEGER PRIMARY KEY,
        mint                 TEXT NOT NULL,
        address              TEXT NOT NULL,
        kind                 TEXT NOT NULL,
        reason               TEXT,
        since                REAL NOT NULL,
        until                REAL,
        notifications        INTEGER NOT NULL DEFAULT 0,
        trades               INTEGER NOT NULL DEFAULT 0,
        last_notification_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS ws_subscriptions_until ON ws_subscriptions(until)",
    "CREATE INDEX IF NOT EXISTS ws_subscriptions_mint ON ws_subscriptions(mint, since)",
]
SCHEMA_V2_COLUMNS = [
    ("coverage_gaps", "recovered_at", "REAL"),
    ("coverage_gaps", "recovered_by", "TEXT"),
    ("raw_undecoded", "program", "TEXT"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_v2(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V2_SQL:
        conn.execute(stmt)
    for table, column, ctype in SCHEMA_V2_COLUMNS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")


MIGRATIONS[2] = _migrate_v2

# ----- schema version 3 (milestone 3) --------------------------------------------------------------
# * `rejected_messages`: raw PumpPortal / RPC messages the ingest refused (system mint, unknown coin,
#   unknown pool) so the case can be inspected without digging through the hourly gzip files.
# * `ws_subscriptions.sub_id`: the server-side subscription id, so a raw rpc_trades notification can be
#   mapped back to its mint when it is replayed (`fart ingest replay`).
# * index on `migrations(pool_address)`: pump-amm swaps resolve their mint through the pool.
# * cleanup of the rows the USDC / PUMP "migrations" caused (trades, migrations, mcap_samples,
#   watchlist, ws_subscriptions, coins); the counts land in kv `schema.v3.cleanup` for the deployer.
#   `decisions` / `outcomes` are append-only and stay; the report's universe comes from `migrations`.
# Every statement is idempotent: a second run deletes nothing and changes nothing.
SCHEMA_V3_SQL = [
    """CREATE TABLE IF NOT EXISTS rejected_messages(
        id          INTEGER PRIMARY KEY,
        received_at REAL NOT NULL,
        source      TEXT NOT NULL,
        kind        TEXT,
        reason      TEXT NOT NULL,
        mint        TEXT,
        raw_json    TEXT,
        raw_ref     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS rejected_messages_received ON rejected_messages(received_at)",
    "CREATE INDEX IF NOT EXISTS migrations_pool ON migrations(pool_address)",
]
SCHEMA_V3_COLUMNS = [
    ("ws_subscriptions", "sub_id", "INTEGER"),
    ("coverage", "covered", "INTEGER"),
]


def _cleanup_system_mints(conn: sqlite3.Connection, mints: Iterable[str], now: float) -> dict[str, int]:
    mints = tuple(mints)
    if not mints:
        return {}
    marks = ",".join("?" for _ in mints)
    out: dict[str, int] = {}
    out["trades"] = conn.execute(f"DELETE FROM trades WHERE mint IN ({marks})", mints).rowcount
    out["migrations"] = conn.execute(f"DELETE FROM migrations WHERE mint IN ({marks})", mints).rowcount
    out["mcap_samples"] = conn.execute(f"DELETE FROM mcap_samples WHERE mint IN ({marks})", mints).rowcount
    out["coins"] = conn.execute(f"DELETE FROM coins WHERE mint IN ({marks})", mints).rowcount
    out["watchlist"] = conn.execute(
        f"UPDATE watchlist SET active=0, until=COALESCE(until, ?) WHERE active=1 AND mint IN ({marks})", (now, *mints)
    ).rowcount
    out["ws_subscriptions"] = conn.execute(
        f"UPDATE ws_subscriptions SET until=? WHERE until IS NULL AND mint IN ({marks})", (now, *mints)
    ).rowcount
    return out


def _migrate_v3(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V3_SQL:
        conn.execute(stmt)
    for table, column, ctype in SCHEMA_V3_COLUMNS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
    now = time.time()
    counts = _cleanup_system_mints(conn, SCHEMA_V3_SYSTEM_MINTS, now)
    kv_set(conn, "schema.v3.cleanup", json.dumps({"at": now, "mints": list(SCHEMA_V3_SYSTEM_MINTS), "deleted": counts}), now)


MIGRATIONS[3] = _migrate_v3


# ----- schema version 4 (milestone 4) --------------------------------------------------------------
# * `watchlist.activated_at`: the last (re)activation of a row. The idle expiry counts from it, while
#   `since` keeps the original time so every ordering by `since` (eviction, the trade stream's cap)
#   stays stable when a coin comes back (`fart/ingest/watch.py`).
# * `migrations.coin_unknown`: a graduation stored without a known create (a PumpPortal migration of a
#   coin created before fart started, or an RPC-decoded migration of a missed create). The universe
#   stays complete and the coin is never watched; the flag clears when the create arrives later.
# * `ws_subscriptions.close_reason`: why a trade subscription was closed (envelope, capped, unwatched,
#   graduated, cu_policy, stream_cap, address_changed, error, disconnect, shutdown, schema_v4), so
#   `fart status` can show what closed today instead of listing closed rows as if they were open.
# * `api_ledger.bytes`: the bytes a per-byte priced row (websocket frames) was booked for.
# * cleanup: the `graduation` watchlist rows milestone 2 wrote (TTL 24 h) belong to the PumpPortal trade
#   subscription that only exists with a funded key; they are closed together with their open
#   ws_subscriptions rows, and every migration without a coin row gets `coin_unknown=1`. The counts
#   land in kv `schema.v4.cleanup` (printed by `install.sh`); a second run changes nothing.
SCHEMA_V4_COLUMNS = [
    ("watchlist", "activated_at", "REAL"),
    ("migrations", "coin_unknown", "INTEGER NOT NULL DEFAULT 0"),
    ("ws_subscriptions", "close_reason", "TEXT"),
    ("api_ledger", "bytes", "INTEGER"),
]


def _cleanup_v4(conn: sqlite3.Connection, now: float) -> dict[str, int]:
    out: dict[str, int] = {}
    out["watchlist_graduation_closed"] = conn.execute(
        "UPDATE watchlist SET active=0, until=? WHERE active=1 AND reason='graduation'", (now,)
    ).rowcount
    out["ws_subscriptions_closed"] = conn.execute(
        "UPDATE ws_subscriptions SET until=?, close_reason='schema_v4' WHERE until IS NULL AND reason='graduation'", (now,)
    ).rowcount
    out["migrations_coin_unknown"] = conn.execute(
        "UPDATE migrations SET coin_unknown=1 WHERE coin_unknown=0 AND NOT EXISTS (SELECT 1 FROM coins c WHERE c.mint = migrations.mint)"
    ).rowcount
    out["watchlist_activated_at_filled"] = conn.execute("UPDATE watchlist SET activated_at=since WHERE activated_at IS NULL").rowcount
    return out


def _migrate_v4(conn: sqlite3.Connection) -> None:
    for table, column, ctype in SCHEMA_V4_COLUMNS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
    conn.execute("CREATE INDEX IF NOT EXISTS ws_subscriptions_close ON ws_subscriptions(until, close_reason)")
    now = time.time()
    counts = _cleanup_v4(conn, now)
    kv_set(conn, "schema.v4.cleanup", json.dumps({"at": now, "counts": counts}), now)


MIGRATIONS[4] = _migrate_v4


# ----- schema version 5 (milestone 5) --------------------------------------------------------------
# No new tables or columns. A one-off cleanup of the M4 leftover: after the M4 upgrade (VPS, 26 Oct 2026
# 12:18 UTC) 195 `meta_burst` watchlist rows written by milestone 3 kept their slots (the M4 rules only
# limit new additions; rows expire by idleness or TTL), so probation got 5 slots instead of its reserve of
# 100. The migration closes the `meta_burst` rows above SCHEMA_V5_META_BURST_MAX_TOTAL (the engine's
# `meta_burst_max_total`, pinned by a test), oldest first, keeping the mints of the engine's frozen choice
# (kv `engine.meta_burst_sets`) and any row that traded in the last 10 minutes; the engine does the same
# every tick from now on (`watch.trim`). Counts land in kv `schema.v5.cleanup`; a second run changes nothing.
# The open `ws_subscriptions` rows of the closed mints are left to the trade stream, which unsubscribes a
# mint 60 s after its watchlist row went inactive (`close_reason` unwatched) and closes everything at restart.
SCHEMA_V5_META_BURST_MAX_TOTAL = 40


def _cleanup_v5(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    from .ingest.watch import trim  # local import: fart.ingest.watch has no dependency on this module

    raw = kv_get(conn, "engine.meta_burst_sets")
    keep: list[str] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                keep = [str(m) for v in data.values() if isinstance(v, dict) for m in v.get("mints", [])]
        except json.JSONDecodeError:
            keep = []
    before = int(conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1 AND reason='meta_burst'").fetchone()[0])
    closed = trim(conn, "meta_burst", SCHEMA_V5_META_BURST_MAX_TOTAL, keep, now)
    after = int(conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1 AND reason='meta_burst'").fetchone()[0])
    return {"meta_burst_active_before": before, "meta_burst_closed": closed, "meta_burst_active_after": after,
            "frozen_mints_kept": len(keep), "max_total": SCHEMA_V5_META_BURST_MAX_TOTAL}


def _migrate_v5(conn: sqlite3.Connection) -> None:
    now = time.time()
    counts = _cleanup_v5(conn, now)
    kv_set(conn, "schema.v5.cleanup", json.dumps({"at": now, "counts": counts}), now)


MIGRATIONS[5] = _migrate_v5


# ----- schema version 6 (milestone 6) --------------------------------------------------------------
# Three new tables, no new columns, no cleanup; every statement is idempotent.
# * `shadow_decisions`: what the X layer WOULD have written to `decisions` while X runs in shadow mode
#   (X_MODE unset or `shadow` with a bearer token): the same columns as `decisions` plus `would_kind`, the
#   `decisions.kind` the row would have had (`fast_post`). `kind` is always `shadow`. Append-only like
#   `decisions`, never read by the engine, the notifier or the tracker; the reports count them.
# * `shadow_confirmations`: every X confirmation count (counts/recent per key) taken in shadow mode, linked to
#   the observe / observe_meta decision it would have confirmed, with the threshold of the moment and whether
#   the last hour / the last 24 h reached it. In live mode the same counts go to `social_counts` and reach the
#   features; in shadow mode they never do. Append-only.
# * `engine_candidates`: one row per UTC day per watched coin (kind `coin`, key = mint) and per meta term the
#   engine ranked (kind `meta`, key = term) with the highest value of every threshold feature seen in any tick
#   of that day, the number of ticks, the watchlist reason at first sight and whether every threshold was met
#   at once in some tick. Written in the engine's decision transaction (an UPSERT per coin and term per tick);
#   `fart engine explain --day` and the daily report read it. `features_asof` stays what it was: a snapshot per
#   decision (a row per watched coin per 15 s tick would be over a million rows a day). Pruned by retention
#   (config/ingest.json -> retention.candidates_days).
SCHEMA_V6_SQL = [
    """CREATE TABLE IF NOT EXISTS shadow_decisions(
        id            INTEGER PRIMARY KEY,
        as_of         REAL NOT NULL,
        created_at    REAL NOT NULL,
        kind          TEXT NOT NULL,
        mint          TEXT,
        term          TEXT,
        features_json TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        verdict       TEXT NOT NULL,
        reasons_json  TEXT NOT NULL,
        would_kind    TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS shadow_decisions_as_of ON shadow_decisions(as_of)",
    "CREATE INDEX IF NOT EXISTS shadow_decisions_term ON shadow_decisions(would_kind, term)",
    """CREATE TRIGGER IF NOT EXISTS shadow_decisions_no_update BEFORE UPDATE ON shadow_decisions
        BEGIN SELECT RAISE(ABORT, 'shadow_decisions is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS shadow_decisions_no_delete BEFORE DELETE ON shadow_decisions
        BEGIN SELECT RAISE(ABORT, 'shadow_decisions is append-only'); END""",
    """CREATE TABLE IF NOT EXISTS shadow_confirmations(
        id             INTEGER PRIMARY KEY,
        ts             REAL NOT NULL,
        decision_id    INTEGER,
        decision_kind  TEXT,
        decision_as_of REAL,
        key            TEXT NOT NULL,
        kind           TEXT NOT NULL,
        n_last_hour    INTEGER,
        n_24h          INTEGER,
        query          TEXT,
        threshold      INTEGER NOT NULL,
        confirmed_1h   INTEGER NOT NULL DEFAULT 0,
        confirmed_24h  INTEGER NOT NULL DEFAULT 0,
        rules_version  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS shadow_confirmations_ts ON shadow_confirmations(ts)",
    "CREATE INDEX IF NOT EXISTS shadow_confirmations_decision ON shadow_confirmations(decision_id)",
    """CREATE TRIGGER IF NOT EXISTS shadow_confirmations_no_update BEFORE UPDATE ON shadow_confirmations
        BEGIN SELECT RAISE(ABORT, 'shadow_confirmations is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS shadow_confirmations_no_delete BEFORE DELETE ON shadow_confirmations
        BEGIN SELECT RAISE(ABORT, 'shadow_confirmations is append-only'); END""",
    """CREATE TABLE IF NOT EXISTS engine_candidates(
        day                         TEXT NOT NULL,
        kind                        TEXT NOT NULL,
        key                         TEXT NOT NULL,
        first_as_of                 REAL NOT NULL,
        last_as_of                  REAL NOT NULL,
        ticks                       INTEGER NOT NULL DEFAULT 0,
        reason                      TEXT,
        rules_version               TEXT,
        max_unique_buyers_5m        INTEGER,
        max_net_sol_5m              REAL,
        min_largest_buyer_share_15m REAL,
        max_largest_buyer_share_15m REAL,
        bundled_last                INTEGER,
        bundled_ever                INTEGER NOT NULL DEFAULT 0,
        max_trades_total            INTEGER,
        passed_momentum             INTEGER NOT NULL DEFAULT 0,
        passed_all                  INTEGER NOT NULL DEFAULT 0,
        max_burst_ratio             REAL,
        burst_inf                   INTEGER NOT NULL DEFAULT 0,
        max_sol_inflow              REAL,
        max_unique_buyers           INTEGER,
        max_n_coins                 INTEGER,
        max_distinct_creators       INTEGER,
        PRIMARY KEY(day, kind, key)
    ) WITHOUT ROWID""",
]


def _migrate_v6(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V6_SQL:
        conn.execute(stmt)


MIGRATIONS[6] = _migrate_v6


# ----- schema version 7 (milestone 7) --------------------------------------------------------------
# One new table and one new column, no cleanup; every statement is idempotent.
# * `x_keys`: the state of the X confirmation planner (`fart/social/planner.py`), one row per key episode
#   (a key = a mint or `meta:<term>`, an episode = the observe / observe_meta decision that admitted it; a key is
#   planned at most once per `confirm.window_s`). The row carries the rank at admission, the next due count, the
#   counts and searches taken, the last and peak counts, the streak of counts without growth, the confirmation
#   time, the money spent on the key (`usd`, of which `usd_search` for the one search and its author lookups),
#   and when and why the key stopped (`stop_reason`: confirmed, refuted, expired, graduated, dead, displaced,
#   exhausted). Updated in place by the social daemon; read by `fart status`, the dry run and the reports. The
#   count rows themselves stay in `shadow_confirmations` (shadow) or `social_counts` (live), append-only.
# * `x_spend.unit`: what a ledger row's `n` counts (post, request, author), so `fart social calibrate` can put
#   the console's amount next to the units per price component; rows from before milestone 7 have NULL (mixed).
SCHEMA_V7_SQL = [
    """CREATE TABLE IF NOT EXISTS x_keys(
        key            TEXT NOT NULL,
        decision_id    INTEGER NOT NULL,
        kind           TEXT NOT NULL,
        decision_kind  TEXT NOT NULL,
        decision_as_of REAL NOT NULL,
        mint           TEXT,
        term           TEXT,
        query          TEXT NOT NULL,
        prio_class     INTEGER NOT NULL DEFAULT 0,
        prio_value     REAL NOT NULL DEFAULT 0,
        rank           INTEGER,
        started_at     REAL NOT NULL,
        next_due_at    REAL,
        counts_n       INTEGER NOT NULL DEFAULT 0,
        searches_n     INTEGER NOT NULL DEFAULT 0,
        authors_n      INTEGER NOT NULL DEFAULT 0,
        last_count_at  REAL,
        last_n_1h      INTEGER,
        last_n_24h     INTEGER,
        peak_n_1h      INTEGER,
        flat_streak    INTEGER NOT NULL DEFAULT 0,
        confirmed_at   REAL,
        search_pending INTEGER NOT NULL DEFAULT 0,
        usd            REAL NOT NULL DEFAULT 0,
        usd_search     REAL NOT NULL DEFAULT 0,
        stopped_at     REAL,
        stop_reason    TEXT,
        rules_version  TEXT,
        PRIMARY KEY(key, decision_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS x_keys_open ON x_keys(stopped_at, next_due_at)",
    "CREATE INDEX IF NOT EXISTS x_keys_started ON x_keys(started_at)",
    "CREATE INDEX IF NOT EXISTS x_keys_stopped ON x_keys(stopped_at, stop_reason)",
    "CREATE INDEX IF NOT EXISTS x_keys_decision ON x_keys(decision_id)",
]
SCHEMA_V7_COLUMNS = [
    ("x_spend", "unit", "TEXT"),
]


def _migrate_v7(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V7_SQL:
        conn.execute(stmt)
    for table, column, ctype in SCHEMA_V7_COLUMNS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")


MIGRATIONS[7] = _migrate_v7


# ----- schema version 8 (milestone 10) -------------------------------------------------------------
# Two indexes, no new table, no column, no cleanup; both statements are idempotent.
# * `trades(received_at)` and `trades(block_time)`: every time-window query over all trades (the website's live views,
#   `SELECT ... FROM trades WHERE received_at >= ?`) scanned the whole table through `trades_mint_received` (720 ms per
#   query on the VPS with 174,305 rows; the public dashboard timed out). With the index a window is a range search; the
#   website bounds every window on `received_at` first (the column with the index) and applies the event time
#   `COALESCE(block_time, received_at)` on the rows in that range (`fart/web/public.py`). Building both on the VPS
#   database takes seconds inside one short IMMEDIATE transaction (the daemons retry a busy write, as always).
SCHEMA_V8_SQL = [
    "CREATE INDEX IF NOT EXISTS trades_received_at ON trades(received_at)",
    "CREATE INDEX IF NOT EXISTS trades_block_time ON trades(block_time)",
]


def _migrate_v8(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V8_SQL:
        conn.execute(stmt)


MIGRATIONS[8] = _migrate_v8


# ----- schema version 9 (milestone 12) -------------------------------------------------------------
# Four new tables next to the engine, no change to any existing table, no cleanup; every statement is idempotent. None of
# them is read by the engine or by the frozen rule files: the research harness, the plays layer, the terminal feed and the
# `fart status` lines read them, the measure-track timer and the plays daemon write them.
# * `creator_stats`: one row per creator wallet with what our own data says about its earlier coins (coins, graduations,
#   winners, bundled, early dev sells, first and last coin); recomputed incrementally by a step of `fart measure track`
#   (`fart/measure/creators.py`). The research harness never reads it (it computes the same figures as-of the entry time);
#   the terminal shows it.
# * `pool_candles`: the 1-minute OHLCV candles of a PumpSwap pool as GeckoTerminal serves them (`fart/measure/candles.py`),
#   fetched once per graduation an hour after it, within the shared `geckoterminal` budget source. A missing candle stays
#   missing. `candle_jobs` is the bounded queue with the backlog `fart status` shows (one row per graduation).
# * `plays`: one row per (rule, version, mint) the plays layer fired (`fart/plays`): the features at the signal, the curve
#   state at the entry, the paper position (size, fill, fees), the exit and the outcome after costs. Updated in place while
#   the position is open (status signal -> open -> closed / unknown / no_fill); never read by the engine.
SCHEMA_V9_SQL = [
    """CREATE TABLE IF NOT EXISTS creator_stats(
        creator        TEXT PRIMARY KEY,
        coins          INTEGER NOT NULL DEFAULT 0,
        graduations    INTEGER NOT NULL DEFAULT 0,
        winners        INTEGER NOT NULL DEFAULT 0,
        not_winners    INTEGER NOT NULL DEFAULT 0,
        bundled        INTEGER NOT NULL DEFAULT 0,
        dev_sold_early INTEGER NOT NULL DEFAULT 0,
        first_coin_at  REAL,
        last_coin_at   REAL,
        computed_at    REAL NOT NULL,
        json           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS creator_stats_last ON creator_stats(last_coin_at)",
    """CREATE TABLE IF NOT EXISTS pool_candles(
        pool        TEXT NOT NULL,
        minute      INTEGER NOT NULL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume_usd  REAL,
        fetched_at  REAL NOT NULL,
        PRIMARY KEY(pool, minute)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS candle_jobs(
        mint          TEXT PRIMARY KEY,
        pool          TEXT,
        graduated_at  REAL NOT NULL,
        due_at        REAL NOT NULL,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_attempt  REAL,
        done          INTEGER NOT NULL DEFAULT 0,
        candles       INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        created_at    REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS candle_jobs_due ON candle_jobs(done, due_at)",
    """CREATE TABLE IF NOT EXISTS plays(
        id             INTEGER PRIMARY KEY,
        rule           TEXT NOT NULL,
        version        INTEGER NOT NULL,
        hypothesis     TEXT,
        variant        TEXT,
        mint           TEXT NOT NULL,
        as_of          REAL NOT NULL,
        created_at     REAL NOT NULL,
        features_json  TEXT NOT NULL,
        entry_json     TEXT,
        size_sol       REAL,
        status         TEXT NOT NULL,
        entry_time     REAL,
        entry_price    REAL,
        fill_price     REAL,
        tokens         REAL,
        fees_sol       REAL,
        peak_price     REAL,
        last_price     REAL,
        last_seen      REAL,
        exit_time      REAL,
        exit_price     REAL,
        exit_reason    TEXT,
        exit_sol_net   REAL,
        pnl_sol        REAL,
        pnl_pct        REAL,
        alerted_at     REAL,
        rules_version  TEXT,
        json           TEXT,
        UNIQUE(rule, version, mint)
    )""",
    "CREATE INDEX IF NOT EXISTS plays_status ON plays(status, as_of)",
    "CREATE INDEX IF NOT EXISTS plays_mint ON plays(mint)",
    "CREATE INDEX IF NOT EXISTS plays_as_of ON plays(as_of)",
]


def _migrate_v9(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V9_SQL:
        conn.execute(stmt)


MIGRATIONS[9] = _migrate_v9


# ----- schema version 10 (milestone 13) ------------------------------------------------------------
# Two new tables next to the engine, no change to any existing table, no cleanup; every statement is idempotent. Neither is
# read by the engine or by the frozen rule files: the plays daemon writes them (`fart/plays/follow.py`), the research harness,
# the plays layer, the terminal feed and `fart status` read them.
# * `followed`: one row per coin the plays daemon follows for 24 hours after its first signal (a pre-registered follow trigger
#   of fart/research/hypotheses.py::FOLLOW_TRIGGERS, an active play rule, or an observe decision of the engine): when it
#   started (`since`, the signal time), why (`reason`, the triggers in the order they fired), until when (`until`), its state
#   (active | graduated | done | closed), the bonding-curve address, the poll schedule (`next_poll_at`, `interval_s`), the
#   last poll and the last point's source, the counters (polls, errors, misses) and when the reserves last changed.
# * `path_points`: the price points the follow step adds between the trades: one row per poll of the BondingCurve account
#   (`source` 'poll') with the virtual and real reserves, the `complete` flag and the slot of the answer. Together with the
#   trades, the tracker samples and the candles they form the price path of `fart/research/harness.py::price_path`. A missing
#   poll stays missing: nothing here is interpolated.
SCHEMA_V10_SQL = [
    """CREATE TABLE IF NOT EXISTS followed(
        mint          TEXT PRIMARY KEY,
        since         REAL NOT NULL,
        reason        TEXT,
        until         REAL NOT NULL,
        state         TEXT NOT NULL,
        curve         TEXT,
        next_poll_at  REAL,
        interval_s    REAL,
        last_poll_at  REAL,
        last_point_t  REAL,
        last_source   TEXT,
        polls         INTEGER NOT NULL DEFAULT 0,
        errors        INTEGER NOT NULL DEFAULT 0,
        misses        INTEGER NOT NULL DEFAULT 0,
        changed_at    REAL,
        graduated_at  REAL,
        closed_reason TEXT,
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL,
        json          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS followed_state ON followed(state, next_poll_at)",
    "CREATE INDEX IF NOT EXISTS followed_since ON followed(since)",
    """CREATE TABLE IF NOT EXISTS path_points(
        mint          TEXT NOT NULL,
        t             REAL NOT NULL,
        source        TEXT NOT NULL,
        virtual_sol   INTEGER,
        virtual_token INTEGER,
        real_sol      INTEGER,
        real_token    INTEGER,
        complete      INTEGER,
        slot          INTEGER,
        PRIMARY KEY(mint, t, source)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS path_points_t ON path_points(t)",
]


def _migrate_v10(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V10_SQL:
        conn.execute(stmt)


MIGRATIONS[10] = _migrate_v10


def _apply_pragmas(conn: sqlite3.Connection, readonly: bool) -> None:
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if not readonly:
        # Changing the journal mode needs an exclusive lock; only do it when not already WAL.
        for attempt in range(5):
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")


def connect(path: str | Path, migrate_schema: bool = True) -> sqlite3.Connection:
    """Read-write connection with WAL. Creates parent dirs and the schema."""
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, readonly=False)
    if migrate_schema:
        migrate(conn)
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Read-only connection (mode=ro). Fails if the database does not exist."""
    p = Path(path)
    uri = f"file:{p.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, readonly=True)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order inside one transaction each. Returns the version."""
    current = schema_version(conn)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        m = MIGRATIONS[version]
        try:
            if isinstance(m, str):
                # executescript commits any open transaction first, so the transaction lives inside the script.
                conn.executescript(f"BEGIN IMMEDIATE;\n{m}\nPRAGMA user_version={int(version)};\nCOMMIT;")
            else:
                conn.execute("BEGIN IMMEDIATE")
                m(conn)
                conn.execute(f"PRAGMA user_version={int(version)}")
                conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        current = version
    return current


# ----- transactions ---------------------------------------------------------------------------------
def is_busy_error(exc: BaseException) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED family (the only errors a short wait can cure)."""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # no transaction is active (BEGIN itself failed)


@contextmanager
def immediate(conn: sqlite3.Connection):
    """One short write transaction: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any error."""
    conn.execute("BEGIN IMMEDIATE")
    ok = False
    try:
        yield conn
        conn.execute("COMMIT")
        ok = True
    finally:
        if not ok:
            _rollback(conn)


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """A closed read-only snapshot (deferred BEGIN ... COMMIT). Never write inside it: the first write
    after another connection's commit would fail with SQLITE_BUSY_SNAPSHOT."""
    conn.execute("BEGIN")
    ok = False
    try:
        yield conn
        conn.execute("COMMIT")
        ok = True
    finally:
        if not ok:
            _rollback(conn)


def write_tx(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], Any], *, what: str = "write",
             attempts: int | None = None, base_sleep_s: float | None = None) -> Any:
    """Run `fn(conn)` inside BEGIN IMMEDIATE and return its result. A busy/locked error (another
    writer held the lock longer than busy_timeout) is retried `attempts` times with a short random
    sleep; any other error rolls back and propagates at once. `fn` must only touch the database."""
    n = WRITE_RETRY_ATTEMPTS if attempts is None else int(attempts)
    base = WRITE_RETRY_BASE_S if base_sleep_s is None else float(base_sleep_s)
    for i in range(1, max(1, n) + 1):
        try:
            with immediate(conn):
                return fn(conn)
        except sqlite3.OperationalError as e:
            if not is_busy_error(e) or i >= n:
                raise
            wait = min(WRITE_RETRY_MAX_S, base * (2 ** (i - 1))) * random.uniform(0.5, 1.5)
            log.warning("db.write_retry", what=what, attempt=i, wait_s=round(wait, 3), error=str(e)[:100])
            time.sleep(wait)
    raise AssertionError("unreachable")


def counter_bump(conn: sqlite3.Connection, key: str, n: int = 1, ts: float = 0.0) -> None:
    """Integer counter in kv (created at n)."""
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT), updated_at=?",
        (key, str(int(n)), ts, int(n), ts),
    )


def day_of(ts: float) -> str:
    return day_key(ts)


def counter_bump_daily(conn: sqlite3.Connection, key: str, n: int = 1, ts: float = 0.0) -> None:
    """`key` (all time) plus `key.<UTC day>` (the daily report reads the latter)."""
    counter_bump(conn, key, n, ts)
    counter_bump(conn, f"{key}.{day_key(ts)}", n, ts)


def record_failure(conn: sqlite3.Connection, key: str, ts: float, n: int = 1, detail: str | None = None) -> bool:
    """Count a failure under `key` (all time) and `key.<UTC day>` (for the daily report), remember
    `key.last_at` / `key.last`. Best effort: returns False (and logs) when even this write fails, so
    a lock storm never turns into an exception storm. Callers keep their own in-memory count."""
    def _w(c: sqlite3.Connection) -> None:
        counter_bump_daily(c, key, n, ts)
        kv_set(c, f"{key}.last_at", str(ts), ts)
        if detail is not None:
            kv_set(c, f"{key}.last", str(detail)[:300], ts)

    try:
        write_tx(conn, _w, what=f"failure:{key}", attempts=2)
        return True
    except sqlite3.OperationalError as e:
        log.warning("db.failure_not_recorded", key=key, error=str(e)[:100])
        return False


def counters(conn: sqlite3.Connection, prefixes: tuple[str, ...]) -> dict[str, str]:
    where = " OR ".join("key LIKE ?" for _ in prefixes)
    return {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM kv WHERE {where} ORDER BY key", tuple(f"{p}%" for p in prefixes))}


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def kv_set(conn: sqlite3.Connection, key: str, value: str, ts: float) -> None:
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, ts),
    )


def kv_setdefault(conn: sqlite3.Connection, key: str, value: str, ts: float) -> bool:
    """Write only when the key does not exist yet (first-start timestamps). Returns True when written."""
    cur = conn.execute("INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES(?,?,?)", (key, value, ts))
    return bool(cur.rowcount)


def table_counts(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    return out


ASOF_TABLES = ("coins", "migrations", "trades")


def asof_clause(alias: str = "") -> str:
    """SQL predicate selecting rows knowable at `as_of` (bind as_of twice).

    A row is usable if it had reached us by as_of (received_at <= as_of) AND its chain time,
    when known, is <= as_of. received_at >= block_time in practice, so the second term is
    the explicit event-time guarantee and the first one is the no-hindsight guarantee.
    """
    a = f"{alias}." if alias else ""
    return f"({a}received_at <= ? AND ({a}block_time IS NULL OR {a}block_time <= ?))"
