"""SQLite access: WAL mode, schema versioning via PRAGMA user_version, idempotent migrations.

Time columns: `slot`/`block_time` are chain time, `received_at`/`created_at`/`measured_at`
are our clock. `decisions` and `outcomes` are append-only (enforced by triggers).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable

SCHEMA_VERSION = 2

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


def _apply_pragmas(conn: sqlite3.Connection, readonly: bool) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
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
    conn = sqlite3.connect(str(p), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, readonly=False)
    if migrate_schema:
        migrate(conn)
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Read-only connection (mode=ro). Fails if the database does not exist."""
    p = Path(path)
    uri = f"file:{p.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
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


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def kv_set(conn: sqlite3.Connection, key: str, value: str, ts: float) -> None:
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, ts),
    )


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
