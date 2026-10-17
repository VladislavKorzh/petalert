"""Bot state: subscriptions, what has been sent to whom, query state, invite codes.

stdlib SQLite with a single connection used from the event loop: queries on a
database this small take under a millisecond, so to_thread or aiosqlite would
only add moving parts.

Everything user-initiated (pause/resume/delete) is also filtered by `chat_id`:
button callback data comes from the client, and forging someone else's
`sub_id` costs nothing.
"""

from __future__ import annotations

import pathlib
import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from .models import Filter

Status = Literal["baselining", "active", "paused"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_code (
    code       TEXT PRIMARY KEY,           -- normalized: no dash, upper case
    created_at TEXT NOT NULL,
    used_by    INTEGER,                    -- telegram user_id; NULL = unused
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS user (
    user_id   INTEGER PRIMARY KEY,
    code      TEXT REFERENCES access_code(code),
    joined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,
    filter_json TEXT NOT NULL,             -- Filter.to_json(); (source, filter_json) is the group key
    status      TEXT NOT NULL CHECK (status IN ('baselining', 'active', 'paused')),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscription_chat ON subscription(chat_id);

-- Slots a subscription already knows about: sent, or seen during baseline.
CREATE TABLE IF NOT EXISTS delivered (
    sub_id   INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    slot_key TEXT NOT NULL,                -- "ths:<assignment_id>"
    end_date TEXT NOT NULL,                -- the row can be pruned after this date
    at       TEXT NOT NULL,
    PRIMARY KEY (sub_id, slot_key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS query_state (
    source       TEXT NOT NULL,
    filter_json  TEXT NOT NULL,
    last_ok_at   TEXT,
    last_full_at TEXT,
    PRIMARY KEY (source, filter_json)
);
"""

# No look-alike characters (0/O, 1/I/L): codes get read out and retyped by hand.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


@dataclass(frozen=True)
class Subscription:
    id: int
    chat_id: int
    source: str
    filter: Filter
    status: Status

    @property
    def group(self) -> tuple[str, str]:
        return self.source, self.filter.to_json()


def _sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        chat_id=row["chat_id"],
        source=row["source"],
        filter=Filter.from_json(row["filter_json"]),
        status=row["status"],
    )


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: pathlib.Path | str):
        if str(path) != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- subscriptions --------------------------------------------------------

    def add_subscription(self, chat_id: int, source: str, flt: Filter) -> int:
        cur = self.conn.execute(
            "INSERT INTO subscription (chat_id, source, filter_json, status, created_at)"
            " VALUES (?, ?, ?, 'baselining', ?)",
            (chat_id, source, flt.to_json(), utcnow().isoformat()),
        )
        return int(cur.lastrowid)

    def subscription(self, sub_id: int, chat_id: int) -> Subscription | None:
        row = self.conn.execute(
            "SELECT * FROM subscription WHERE id = ? AND chat_id = ?", (sub_id, chat_id)
        ).fetchone()
        return _sub(row) if row else None

    def subscriptions(self, chat_id: int) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM subscription WHERE chat_id = ? ORDER BY id", (chat_id,)
        )
        return [_sub(r) for r in rows]

    def due_subscriptions(self) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM subscription WHERE status IN ('baselining', 'active') ORDER BY id"
        )
        return [_sub(r) for r in rows]

    def set_status(self, sub_id: int, chat_id: int, status: Status) -> bool:
        cur = self.conn.execute(
            "UPDATE subscription SET status = ? WHERE id = ? AND chat_id = ?",
            (status, sub_id, chat_id),
        )
        return cur.rowcount == 1

    def delete(self, sub_id: int, chat_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM subscription WHERE id = ? AND chat_id = ?", (sub_id, chat_id)
        )
        return cur.rowcount == 1

    def pause_chat(self, chat_id: int) -> int:
        cur = self.conn.execute(
            "UPDATE subscription SET status = 'paused' WHERE chat_id = ? AND status != 'paused'",
            (chat_id,),
        )
        return cur.rowcount

    def finish_baseline(self, sub_id: int, items: Iterable[tuple[str, date]]) -> bool:
        """Mark everything seen and switch to active — atomically.

        Only if the subscription is still `baselining`: during the full crawl
        (a minute or two) it may have been deleted or paused, and then the
        result belongs to nobody.
        """
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE subscription SET status = 'active' WHERE id = ? AND status = 'baselining'",
                (sub_id,),
            )
            if cur.rowcount != 1:
                return False
            self._insert_delivered(conn, sub_id, items)
        return True

    # --- dedup ----------------------------------------------------------------

    def delivered_keys(self, sub_id: int) -> set[str]:
        rows = self.conn.execute("SELECT slot_key FROM delivered WHERE sub_id = ?", (sub_id,))
        return {r[0] for r in rows}

    def mark_delivered(self, sub_id: int, items: Iterable[tuple[str, date]]) -> None:
        with self.tx() as conn:
            self._insert_delivered(conn, sub_id, items)

    @staticmethod
    def _insert_delivered(conn: sqlite3.Connection, sub_id: int, items) -> None:
        now = utcnow().isoformat()
        # WHERE EXISTS: the subscription may have been deleted while the message
        # was in flight — then silently do nothing instead of a foreign-key
        # IntegrityError.
        conn.executemany(
            "INSERT OR IGNORE INTO delivered (sub_id, slot_key, end_date, at)"
            " SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM subscription WHERE id = ?)",
            [(sub_id, key, end.isoformat(), now, sub_id) for key, end in items],
        )

    # --- query state ----------------------------------------------------------

    def query_state(self, source: str, filter_json: str) -> tuple[datetime | None, datetime | None]:
        row = self.conn.execute(
            "SELECT last_ok_at, last_full_at FROM query_state WHERE source = ? AND filter_json = ?",
            (source, filter_json),
        ).fetchone()
        return (_ts(row[0]), _ts(row[1])) if row else (None, None)

    def touch_query(self, source: str, filter_json: str, at: datetime, *, full: bool) -> None:
        self.conn.execute(
            "INSERT INTO query_state (source, filter_json, last_ok_at, last_full_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (source, filter_json) DO UPDATE SET last_ok_at = excluded.last_ok_at,"
            " last_full_at = COALESCE(excluded.last_full_at, query_state.last_full_at)",
            (source, filter_json, at.isoformat(), at.isoformat() if full else None),
        )

    def prune(self, today: date) -> tuple[int, int]:
        with self.tx() as conn:
            delivered = conn.execute(
                "DELETE FROM delivered WHERE end_date < ?", (today.isoformat(),)
            ).rowcount
            states = conn.execute(
                "DELETE FROM query_state WHERE NOT EXISTS (SELECT 1 FROM subscription s"
                " WHERE s.source = query_state.source AND s.filter_json = query_state.filter_json)"
            ).rowcount
        return delivered, states

    # --- access ---------------------------------------------------------------

    def is_allowed(self, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM user WHERE user_id = ?", (user_id,)).fetchone() is not None

    def redeem(self, code: str, user_id: int) -> bool:
        """Single use: a second user with the same code gets False."""
        now = utcnow().isoformat()
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE access_code SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
                (user_id, now, normalize_code(code)),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "INSERT OR IGNORE INTO user (user_id, code, joined_at) VALUES (?, ?, ?)",
                (user_id, normalize_code(code), now),
            )
        return True

    def generate_codes(self, n: int) -> list[str]:
        codes: list[str] = []
        now = utcnow().isoformat()
        with self.tx() as conn:
            while len(codes) < n:
                code = new_code()
                cur = conn.execute(
                    "INSERT OR IGNORE INTO access_code (code, created_at) VALUES (?, ?)", (code, now)
                )
                if cur.rowcount == 1:
                    codes.append(code)
        return codes

    def unused_codes(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT code FROM access_code WHERE used_by IS NULL ORDER BY created_at, code"
        )
        return [r[0] for r in rows]
