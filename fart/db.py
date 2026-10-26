"""SQLite access: WAL mode, schema versioning via PRAGMA user_version, idempotent migrations,
and the ONE way to write: `write_tx()`.

Time columns: `slot`/`block_time` are chain time, `received_at`/`created_at`/`measured_at`
are our clock. `decisions` and `outcomes` are append-only (enforced by triggers).

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

SCHEMA_VERSION = 4

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
