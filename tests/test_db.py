from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from petalert.db import CODE_ALPHABET, Store, format_code, normalize_code
from petalert.models import Filter

FLT = Filter("europe", country="portugal")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


def test_subscription_lifecycle_and_ownership(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    (sub,) = store.subscriptions(10)
    assert (sub.id, sub.status, sub.filter) == (sub_id, "baselining", FLT)

    # Someone else's chat_id with a forged button changes nothing.
    assert not store.set_status(sub_id, 99, "paused")
    assert not store.delete(sub_id, 99)
    assert store.set_status(sub_id, 10, "paused")
    assert store.due_subscriptions() == []
    assert store.delete(sub_id, 10)
    assert store.subscriptions(10) == []


def test_finish_baseline_only_while_still_baselining(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    assert store.finish_baseline(store.subscription(sub_id, 10), [("ths:1", date(2026, 12, 1))])
    assert store.delivered_keys(sub_id) == {"ths:1"}
    assert store.subscription(sub_id, 10).status == "active"

    other = store.add_subscription(10, "ths", FLT)
    snapshot = store.subscription(other, 10)
    store.set_status(other, 10, "paused")  # paused in the middle of the crawl
    assert not store.finish_baseline(snapshot, [("ths:2", date(2026, 12, 1))])
    assert store.delivered_keys(other) == set()


def test_ids_are_never_reused_and_stale_snapshots_do_nothing(store):
    first = store.subscription(store.add_subscription(10, "ths", FLT), 10)
    store.delete(first.id, 10)  # deleted while its baseline crawl was running
    new_id = store.add_subscription(20, "ths", Filter("asia"))
    assert new_id != first.id

    assert not store.finish_baseline(first, [("ths:1", date(2026, 12, 1))])
    store.mark_delivered(first, [("ths:2", date(2026, 12, 1))])
    assert store.subscription(new_id, 20).status == "baselining"
    assert store.delivered_keys(new_id) == set()
    assert store.still_active([first]) == set()


def test_delete_cascades_and_marking_a_deleted_sub_is_a_noop(store):
    sub = store.subscription(store.add_subscription(10, "ths", FLT), 10)
    store.mark_delivered(sub, [("ths:1", date(2026, 12, 1))])
    store.delete(sub.id, 10)
    assert store.conn.execute("SELECT count(*) FROM delivered").fetchone()[0] == 0
    store.mark_delivered(sub, [("ths:2", date(2026, 12, 1))])  # no IntegrityError
    assert store.conn.execute("SELECT count(*) FROM delivered").fetchone()[0] == 0


def test_pause_chat_touches_only_that_chat(store):
    a = store.add_subscription(10, "ths", FLT)
    b = store.add_subscription(20, "ths", FLT)
    assert store.pause_chat(10) == 1
    assert [s.id for s in store.due_subscriptions()] == [b]
    assert store.subscription(a, 10).status == "paused"


def test_query_state_keeps_last_full_on_partial_polls(store):
    t1 = datetime(2026, 10, 17, 10, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 11, tzinfo=UTC)
    store.touch_query("ths", "{}", t1, full=True)
    store.touch_query("ths", "{}", t2, full=False)
    assert store.query_state("ths", "{}") == (t2, t1)
    assert store.query_state("ths", "other") == (None, None)


def test_prune_drops_past_slots_and_orphan_query_state(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    sub = store.subscription(sub_id, 10)
    store.mark_delivered(sub, [("ths:old", date(2026, 10, 1)), ("ths:new", date(2026, 12, 1))])
    now = datetime(2026, 10, 17, tzinfo=UTC)
    store.touch_query("ths", FLT.to_json(), now, full=True)
    store.touch_query("ths", Filter("asia").to_json(), now, full=True)

    assert store.prune(date(2026, 10, 17)) == (1, 1)
    assert store.delivered_keys(sub_id) == {"ths:new"}
    assert store.query_state("ths", FLT.to_json())[0] == now


def test_codes_are_single_use(store):
    codes = store.generate_codes(100)
    assert len(set(codes)) == 100
    assert all(len(c) == 8 and set(c) <= set(CODE_ALPHABET) for c in codes)

    code = codes[0]
    assert not store.is_allowed(1)
    assert store.redeem(format_code(code).lower(), 1)  # "abcd-efgh" is accepted too
    assert store.is_allowed(1)
    assert not store.redeem(code, 2)  # the same code a second time — no
    assert not store.is_allowed(2)
    assert not store.redeem("NOPE-NOPE", 2)
    assert code not in store.unused_codes() and len(store.unused_codes()) == 99


def test_code_normalization():
    assert normalize_code(" k7qm-4xpa ") == "K7QM4XPA"
    assert format_code("K7QM4XPA") == "K7QM-4XPA"


def test_transitions_only_from_allowed_states(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    store.set_status(sub_id, 10, "active")
    # A stale Resume on a running subscription must not re-baseline it.
    assert not store.transition(sub_id, 10, "baselining", allowed_from=("paused",))
    assert store.subscription(sub_id, 10).status == "active"
    assert store.transition(sub_id, 10, "paused", allowed_from=("active", "baselining"))
    assert not store.transition(sub_id, 99, "baselining", allowed_from=("paused",))
    assert store.transition(sub_id, 10, "baselining", allowed_from=("paused",))


def test_old_database_is_migrated_to_never_reuse_ids(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE subscription (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, source TEXT NOT NULL,
            filter_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE delivered (sub_id INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
            slot_key TEXT NOT NULL, end_date TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (sub_id, slot_key)) WITHOUT ROWID;
    """)
    fj = FLT.to_json()
    old.execute("INSERT INTO subscription VALUES (1, 10, 'ths', ?, 'active', 'x'), (2, 10, 'ths', ?, 'active', 'x')", (fj, fj))
    old.execute("INSERT INTO delivered VALUES (1, 'ths:a', '2030-01-01', 'x'), (2, 'ths:b', '2030-01-01', 'x')")
    old.commit()
    old.close()

    store = Store(path)
    assert [s.id for s in store.subscriptions(10)] == [1, 2]
    assert store.delivered_keys(1) == {"ths:a"} and store.delivered_keys(2) == {"ths:b"}
    store.delete(2, 10)
    assert store.add_subscription(10, "ths", FLT) == 3  # not 2 again
    store.delete(1, 10)
    assert store.delivered_keys(1) == set()  # the cascade still works
    store.close()
    Store(path).close()  # second open: already migrated, nothing to do
