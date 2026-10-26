"""The outbox: every message is a row first, sent later. `dedupe_key` is UNIQUE, so the same
message can never be queued twice; retries follow the configured ladder, then `gave_up`."""
from __future__ import annotations

import sqlite3
from typing import Any

MAX_TEXT = 4096  # Telegram message limit


def enqueue(conn: sqlite3.Connection, kind: str, dedupe_key: str, chat: str, text: str, now: float) -> int | None:
    """Queue a message; None when the dedupe_key already exists."""
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(kind, dedupe_key, chat, text, created_at, attempts, next_try_at, status)"
        " VALUES(?,?,?,?,?,0,?,'pending')",
        (kind, dedupe_key, str(chat), text, now, now),
    )
    return int(cur.lastrowid) if cur.rowcount else None


def due(conn: sqlite3.Connection, now: float, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM outbox WHERE status='pending' AND (next_try_at IS NULL OR next_try_at <= ?) ORDER BY id LIMIT ?", (now, int(limit))
    ).fetchall()


def sent_in_last(conn: sqlite3.Connection, now: float, seconds: float = 60.0) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM outbox WHERE status='sent' AND sent_at > ?", (now - seconds,)).fetchone()[0])


def mark_sent(conn: sqlite3.Connection, row_id: int, message_id: int | None, now: float) -> None:
    conn.execute("UPDATE outbox SET status='sent', message_id=?, sent_at=?, error=NULL, attempts=attempts+1 WHERE id=?", (message_id, now, row_id))


def mark_retry(conn: sqlite3.Connection, row_id: int, next_try_at: float, error: str, count_attempt: bool = True) -> None:
    conn.execute(
        "UPDATE outbox SET next_try_at=?, error=?, attempts=attempts+? WHERE id=?",
        (next_try_at, error[:300], 1 if count_attempt else 0, row_id),
    )


def mark_gave_up(conn: sqlite3.Connection, row_id: int, error: str) -> None:
    conn.execute("UPDATE outbox SET status='gave_up', error=?, attempts=attempts+1 WHERE id=?", (error[:300], row_id))


def last_rows(conn: sqlite3.Connection, n: int = 20) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (int(n),))]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status")}
