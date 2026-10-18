"""Bot state: subscriptions, what has been sent to whom, query state, invite codes.

stdlib SQLite with a single connection used from the event loop: queries on a
database this small take under a millisecond, so to_thread or aiosqlite would
only add moving parts.

Everything user-initiated (pause/resume/delete) is also filtered by `chat_id`:
button callback data comes from the client, and forging someone else's
`sub_id` costs nothing.

A subscription is a filter; it watches every site its chat has turned on
(`chat_source`, on by default). Per subscription and site, a `watch` row says
it has been baselined there: no row means "index silently first" — a new
subscription, a resumed one, a site just turned on.
"""

from __future__ import annotations

import pathlib
import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from .models import Filter

Status = Literal["active", "paused"]

# AUTOINCREMENT: ids are never reused. Otherwise a stale /list button or an
# in-flight tick holding the id of a deleted subscription would act on a newer
# one that got the same id (plain INTEGER PRIMARY KEY reuses the max id).
SUBSCRIPTION_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,             -- legacy: the one site of an old subscription; '' now
    filter_json TEXT NOT NULL,             -- Filter.to_json(); (site, filter_json) is the group key
    status      TEXT NOT NULL CHECK (status IN ('baselining', 'active', 'paused')),  -- 'baselining': legacy
    created_at  TEXT NOT NULL,
    baseline_at TEXT,                      -- legacy, moved to watch
    watermark   INTEGER                    -- legacy, moved to watch
);"""

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

{subscription}
CREATE INDEX IF NOT EXISTS subscription_chat ON subscription(chat_id);

-- Slots a subscription already knows about: sent, or seen during baseline.
CREATE TABLE IF NOT EXISTS delivered (
    sub_id   INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    slot_key TEXT NOT NULL,                -- "ths:<assignment_id>"
    end_date TEXT NOT NULL,                -- the row can be pruned after this date
    at       TEXT NOT NULL,
    PRIMARY KEY (sub_id, slot_key)
) WITHOUT ROWID;

-- A table of several new sits sent as one message: what its buttons open.
CREATE TABLE IF NOT EXISTS alert_list (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    items_json TEXT NOT NULL,              -- the listings and their new slots, as sent
    created_at TEXT NOT NULL
);

-- Sites a chat gets alerts from. No row = on.
CREATE TABLE IF NOT EXISTS chat_source (
    chat_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (chat_id, source)
) WITHOUT ROWID;

-- A subscription baselined on a site (see the module docstring).
CREATE TABLE IF NOT EXISTS watch (
    sub_id      INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    baseline_at TEXT NOT NULL,             -- see models.appeared_after
    watermark   INTEGER,                   -- highest slot id the baseline saw (growing-id sites)
    reindex     INTEGER NOT NULL DEFAULT 0, -- 1: mark what matches now as seen, quietly (rebaseline CLI)
    PRIMARY KEY (sub_id, source)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS query_state (
    source       TEXT NOT NULL,
    filter_json  TEXT NOT NULL,
    last_ok_at   TEXT,
    last_full_at TEXT,
    PRIMARY KEY (source, filter_json)
);
"""

# The buttons of an alert table keep working this long.
ALERT_LIST_TTL = timedelta(days=30)

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
    filter: Filter
    status: Status
    filter_json: str  # as stored; with id and chat_id it identifies this exact subscription


@dataclass(frozen=True)
class Watch:
    """A subscription on one site. `baseline_at` None: not baselined there yet."""

    sub: Subscription
    source: str
    baseline_at: datetime | None = None
    watermark: int | None = None
    reindex: bool = False  # baselined, but to be re-marked quietly (see Store.reindex)

    @property
    def baselined(self) -> bool:
        return self.baseline_at is not None

    @property
    def group(self) -> tuple[str, str]:
        return self.source, self.sub.filter_json


def _sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        chat_id=row["chat_id"],
        filter=Filter.from_json(row["filter_json"]),
        status=row["status"],
        filter_json=row["filter_json"],
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
        self.conn.executescript(SCHEMA.format(subscription=SUBSCRIPTION_TABLE.format(name="subscription")))
        self._migrate_autoincrement()
        self._migrate_baseline_columns()
        self._migrate_watches()

    def _migrate_watches(self) -> None:
        """One-site subscriptions → watches: an active one keeps its baseline on
        its site; any other site it now watches gets indexed silently first.
        The legacy columns are cleared in the same transaction, so this runs
        on every open as a no-op, and an interrupted first run simply retries."""
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watch (sub_id, source, baseline_at, watermark)"
                " SELECT id, source, baseline_at, watermark FROM subscription"
                " WHERE status = 'active' AND source != '' AND baseline_at IS NOT NULL"
            )
            conn.execute(
                "UPDATE subscription SET source = '', baseline_at = NULL, watermark = NULL,"
                " status = CASE status WHEN 'baselining' THEN 'active' ELSE status END"
                " WHERE source != '' OR baseline_at IS NOT NULL OR status = 'baselining'"
            )

    def _migrate_baseline_columns(self) -> None:
        """Older databases lack baseline_at/watermark. Backfill active ones from
        what they know: created_at, and the highest slot id already delivered."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(subscription)")}
        with self.tx() as conn:
            if "baseline_at" not in have:
                conn.execute("ALTER TABLE subscription ADD COLUMN baseline_at TEXT")
            if "watermark" not in have:
                conn.execute("ALTER TABLE subscription ADD COLUMN watermark INTEGER")
            conn.execute(
                "UPDATE subscription SET baseline_at = created_at, watermark = ("
                "  SELECT MAX(CAST(substr(slot_key, instr(slot_key, ':') + 1) AS INTEGER))"
                "  FROM delivered WHERE sub_id = subscription.id)"
                " WHERE baseline_at IS NULL AND status != 'baselining' AND source != ''"
            )

    def _migrate_autoincrement(self) -> None:
        """Databases created before AUTOINCREMENT get the table rebuilt once
        (SQLite can't alter a primary key). Foreign keys are off meanwhile, or
        dropping the old table would cascade-delete every `delivered` row."""
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'subscription'"
        ).fetchone()[0]
        if "AUTOINCREMENT" in sql.upper():
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.tx() as conn:
                conn.execute(SUBSCRIPTION_TABLE.format(name="subscription_new"))
                conn.execute(
                    "INSERT INTO subscription_new (id, chat_id, source, filter_json, status, created_at)"
                    " SELECT id, chat_id, source, filter_json, status, created_at FROM subscription"
                )
                conn.execute("DROP TABLE subscription")
                conn.execute("ALTER TABLE subscription_new RENAME TO subscription")
                conn.execute("CREATE INDEX IF NOT EXISTS subscription_chat ON subscription(chat_id)")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("subscription migration broke foreign keys")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

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

    def add_subscription(self, chat_id: int, flt: Filter) -> int:
        cur = self.conn.execute(
            "INSERT INTO subscription (chat_id, source, filter_json, status, created_at)"
            " VALUES (?, '', ?, 'active', ?)",
            (chat_id, flt.to_json(), utcnow().isoformat()),
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
        rows = self.conn.execute("SELECT * FROM subscription WHERE status = 'active' ORDER BY id")
        return [_sub(r) for r in rows]

    def pause(self, sub_id: int, chat_id: int) -> bool:
        """False if it isn't active (a stale or double-tapped button)."""
        cur = self.conn.execute(
            "UPDATE subscription SET status = 'paused' WHERE id = ? AND chat_id = ? AND status = 'active'",
            (sub_id, chat_id),
        )
        return cur.rowcount == 1

    def resume(self, sub_id: int, chat_id: int) -> bool:
        """Active again, and baselined again on every site: whatever appeared
        while paused is not sent. Only from paused — a stale Resume on a running
        subscription would otherwise swallow its undelivered new dates."""
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE subscription SET status = 'active' WHERE id = ? AND chat_id = ? AND status = 'paused'",
                (sub_id, chat_id),
            )
            if cur.rowcount != 1:
                return False
            conn.execute("DELETE FROM watch WHERE sub_id = ?", (sub_id,))
        return True

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

    # --- sites and watches ----------------------------------------------------

    def enabled_sources(self, chat_id: int, sources: Iterable[str]) -> list[str]:
        """Those of `sources` (in their order) the chat gets alerts from."""
        off = {
            r[0] for r in self.conn.execute(
                "SELECT source FROM chat_source WHERE chat_id = ? AND enabled = 0", (chat_id,)
            )
        }
        return [s for s in sources if s not in off]

    def set_source(self, chat_id: int, source: str, enabled: bool) -> None:
        """Off drops the chat's watches there, so turning it back on indexes it
        silently again instead of sending what appeared while it was off."""
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO chat_source (chat_id, source, enabled) VALUES (?, ?, ?)"
                " ON CONFLICT (chat_id, source) DO UPDATE SET enabled = excluded.enabled",
                (chat_id, source, int(enabled)),
            )
            if not enabled:
                conn.execute(
                    "DELETE FROM watch WHERE source = ?"
                    " AND sub_id IN (SELECT id FROM subscription WHERE chat_id = ?)",
                    (source, chat_id),
                )

    def watches(self, chat_id: int | None = None) -> dict[tuple[int, str], tuple[datetime, int | None, bool]]:
        """(sub_id, source) → (baseline_at, watermark, reindex), of one chat or all."""
        sql = "SELECT w.sub_id, w.source, w.baseline_at, w.watermark, w.reindex FROM watch w"
        if chat_id is None:
            rows = self.conn.execute(sql)
        else:
            rows = self.conn.execute(
                sql + " JOIN subscription s ON s.id = w.sub_id WHERE s.chat_id = ?", (chat_id,)
            )
        return {(r[0], r[1]): (datetime.fromisoformat(r[2]), r[3], bool(r[4])) for r in rows}

    def reindex(self, source: str) -> int:
        """On the next tick, every subscription marks what matches on `source`
        right now as seen, quietly, keeping its baseline. For a change on our
        side that makes old listings matchable for the first time."""
        return self.conn.execute("UPDATE watch SET reindex = 1 WHERE source = ?", (source,)).rowcount

    def finish_reindex(self, sub: Subscription, source: str, items: Iterable[tuple[str, date]]) -> bool:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE watch SET reindex = 0 WHERE sub_id = ? AND source = ? AND reindex = 1"
                " AND EXISTS (SELECT 1 FROM subscription WHERE id = ? AND chat_id = ? AND filter_json = ?)",
                (sub.id, source, sub.id, sub.chat_id, sub.filter_json),
            )
            if cur.rowcount != 1:
                return False
            self._insert_delivered(conn, sub, items)
        return True

    def finish_baseline(
        self,
        sub: Subscription,
        source: str,
        items: Iterable[tuple[str, date]],
        *,
        watermark: int | None = None,
        at: datetime | None = None,
    ) -> bool:
        """Mark everything seen and start watching `source` — atomically.

        Only if that exact subscription is still active, still unbaselined
        there, and the site still on for its chat: during the full crawl (a
        minute or two) any of that may have changed, and then the result
        belongs to nobody. The chat and filter are checked too, so a stale id
        never hands a baseline to a different subscription.
        """
        with self.tx() as conn:
            current = conn.execute(
                "SELECT 1 FROM subscription WHERE id = ? AND chat_id = ? AND filter_json = ?"
                " AND status = 'active'"
                " AND NOT EXISTS (SELECT 1 FROM watch WHERE sub_id = ? AND source = ?)"
                " AND NOT EXISTS (SELECT 1 FROM chat_source WHERE chat_id = ? AND source = ? AND enabled = 0)",
                (sub.id, sub.chat_id, sub.filter_json, sub.id, source, sub.chat_id, source),
            ).fetchone()
            if current is None:
                return False
            conn.execute(
                "INSERT INTO watch (sub_id, source, baseline_at, watermark) VALUES (?, ?, ?, ?)",
                (sub.id, source, (at or utcnow()).isoformat(), watermark),
            )
            self._insert_delivered(conn, sub, items)
        return True

    def still_watching(self, pairs: Iterable[tuple[Subscription, str]]) -> set[tuple[int, str]]:
        """Those (subscription id, site) pairs that are, right now, the same
        subscription, active and baselined on that site.

        A tick awaits the network for a while; meanwhile a user can pause or
        delete a subscription, or turn a site off."""
        wanted = {(s.id, s.chat_id, s.filter_json, source) for s, source in pairs}
        rows = self.conn.execute(
            "SELECT s.id, s.chat_id, s.filter_json, w.source FROM subscription s"
            " JOIN watch w ON w.sub_id = s.id WHERE s.status = 'active' AND w.reindex = 0"
        )
        return {(r[0], r[3]) for r in rows if (r[0], r[1], r[2], r[3]) in wanted}

    # --- dedup ----------------------------------------------------------------

    def delivered_keys(self, sub_id: int) -> set[str]:
        rows = self.conn.execute("SELECT slot_key FROM delivered WHERE sub_id = ?", (sub_id,))
        return {r[0] for r in rows}

    def mark_delivered(self, sub: Subscription, items: Iterable[tuple[str, date]]) -> None:
        with self.tx() as conn:
            self._insert_delivered(conn, sub, items)

    @staticmethod
    def _insert_delivered(conn: sqlite3.Connection, sub: Subscription, items) -> None:
        now = utcnow().isoformat()
        # WHERE EXISTS: the subscription may have been deleted while the message
        # was in flight (or its id reused by a new one) — then silently do
        # nothing instead of a foreign-key IntegrityError or a wrong owner.
        conn.executemany(
            "INSERT OR IGNORE INTO delivered (sub_id, slot_key, end_date, at)"
            " SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM subscription"
            " WHERE id = ? AND chat_id = ? AND filter_json = ?)",
            [
                (sub.id, key, end.isoformat(), now, sub.id, sub.chat_id, sub.filter_json)
                for key, end in items
            ],
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

    def prune(self, today: date) -> tuple[int, int, int]:
        with self.tx() as conn:
            delivered = conn.execute(
                "DELETE FROM delivered WHERE end_date < ?", (today.isoformat(),)
            ).rowcount
            states = conn.execute(
                "DELETE FROM query_state WHERE NOT EXISTS (SELECT 1 FROM subscription s"
                " WHERE s.filter_json = query_state.filter_json)"
            ).rowcount
            lists = conn.execute(
                "DELETE FROM alert_list WHERE created_at < ?",
                ((today - ALERT_LIST_TTL).isoformat(),),
            ).rowcount
        return delivered, states, lists

    # --- alert lists ----------------------------------------------------------

    def add_alert_list(self, chat_id: int, items_json: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO alert_list (chat_id, items_json, created_at) VALUES (?, ?, ?)",
            (chat_id, items_json, utcnow().isoformat()),
        )
        return int(cur.lastrowid)

    def alert_list(self, list_id: int, chat_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT items_json FROM alert_list WHERE id = ? AND chat_id = ?", (list_id, chat_id)
        ).fetchone()
        return row[0] if row else None

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
