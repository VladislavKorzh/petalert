from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from petalert.db import ALERT_LIST_TTL, CODE_ALPHABET, Store, format_code, normalize_code, utcnow
from petalert.models import Filter

FLT = Filter("europe", country="portugal")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


def test_subscription_lifecycle_and_ownership(store):
    sub_id = store.add_subscription(10, FLT)
    (sub,) = store.subscriptions(10)
    assert (sub.id, sub.status, sub.filter) == (sub_id, "active", FLT)

    # Someone else's chat_id with a forged button changes nothing.
    assert not store.pause(sub_id, 99)
    assert not store.delete(sub_id, 99)
    assert store.pause(sub_id, 10)
    assert store.due_subscriptions() == []
    assert store.delete(sub_id, 10)
    assert store.subscriptions(10) == []


def test_finish_baseline_only_while_still_unbaselined_active_and_on(store):
    sub_id = store.add_subscription(10, FLT)
    sub = store.subscription(sub_id, 10)
    at = datetime(2026, 10, 18, 12, tzinfo=UTC)
    assert store.finish_baseline(sub, "ths", [("ths:1", date(2026, 12, 1))], watermark=5, at=at)
    assert store.delivered_keys(sub_id) == {"ths:1"}
    assert store.watches(10) == {(sub_id, "ths"): (at, 5, False)}
    assert not store.finish_baseline(sub, "ths", [("ths:9", date(2026, 12, 1))])  # already baselined there
    assert store.finish_baseline(sub, "mmh", [])  # another site is its own baseline

    paused = store.subscription(store.add_subscription(10, FLT), 10)
    store.pause(paused.id, 10)  # paused in the middle of the crawl
    assert not store.finish_baseline(paused, "ths", [("ths:2", date(2026, 12, 1))])
    assert store.delivered_keys(paused.id) == set()

    off = store.subscription(store.add_subscription(10, FLT), 10)
    store.set_source(10, "mmh", False)  # the site turned off in the middle of the crawl
    assert not store.finish_baseline(off, "mmh", [])
    assert store.finish_baseline(off, "ths", [])


def test_sites_are_on_by_default_and_off_drops_only_that_chats_watches(store):
    mine = store.subscription(store.add_subscription(10, FLT), 10)
    theirs = store.subscription(store.add_subscription(20, FLT), 20)
    for sub in (mine, theirs):
        store.finish_baseline(sub, "mmh", [])
        store.finish_baseline(sub, "ths", [])
    assert store.enabled_sources(10, ["ths", "mmh"]) == ["ths", "mmh"]

    store.set_source(10, "mmh", False)
    assert store.enabled_sources(10, ["ths", "mmh"]) == ["ths"]
    assert set(store.watches(10)) == {(mine.id, "ths")}
    assert set(store.watches(20)) == {(theirs.id, "ths"), (theirs.id, "mmh")}

    store.set_source(10, "mmh", True)  # back on: indexed again, not resumed
    assert store.enabled_sources(10, ["ths", "mmh"]) == ["ths", "mmh"]
    assert (mine.id, "mmh") not in store.watches(10)
    assert store.reindex("ths") == 2
    assert {k: v[2] for k, v in store.watches().items()} == {
        (mine.id, "ths"): True, (theirs.id, "ths"): True, (theirs.id, "mmh"): False
    }
    assert store.still_watching([(mine, "ths"), (theirs, "mmh")]) == {(theirs.id, "mmh")}
    at = store.watches()[(mine.id, "ths")][0]
    assert store.finish_reindex(mine, "ths", [("ths:7", date(2026, 12, 1))])
    assert not store.finish_reindex(mine, "ths", [])  # done once
    assert store.watches()[(mine.id, "ths")] == (at, None, False)  # the baseline time stays
    assert "ths:7" in store.delivered_keys(mine.id)


def test_ids_are_never_reused_and_stale_snapshots_do_nothing(store):
    first = store.subscription(store.add_subscription(10, FLT), 10)
    store.delete(first.id, 10)  # deleted while its baseline crawl was running
    new_id = store.add_subscription(20, Filter("asia"))
    assert new_id != first.id

    assert not store.finish_baseline(first, "ths", [("ths:1", date(2026, 12, 1))])
    store.mark_delivered(first, [("ths:2", date(2026, 12, 1))])
    assert store.watches() == {}
    assert store.delivered_keys(new_id) == set()
    assert store.still_watching([(first, "ths")]) == set()


def test_delete_cascades_and_marking_a_deleted_sub_is_a_noop(store):
    sub = store.subscription(store.add_subscription(10, FLT), 10)
    store.mark_delivered(sub, [("ths:1", date(2026, 12, 1))])
    store.delete(sub.id, 10)
    assert store.conn.execute("SELECT count(*) FROM delivered").fetchone()[0] == 0
    store.mark_delivered(sub, [("ths:2", date(2026, 12, 1))])  # no IntegrityError
    assert store.conn.execute("SELECT count(*) FROM delivered").fetchone()[0] == 0


def test_pause_chat_touches_only_that_chat(store):
    a = store.add_subscription(10, FLT)
    b = store.add_subscription(20, FLT)
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
    sub_id = store.add_subscription(10, FLT)
    sub = store.subscription(sub_id, 10)
    store.mark_delivered(sub, [("ths:old", date(2026, 10, 1)), ("ths:new", date(2026, 12, 1))])
    now = datetime(2026, 10, 17, tzinfo=UTC)
    store.touch_query("ths", FLT.to_json(), now, full=True)
    store.touch_query("ths", Filter("asia").to_json(), now, full=True)

    assert store.prune(date(2026, 10, 17)) == (1, 1, 0)
    assert store.delivered_keys(sub_id) == {"ths:new"}
    assert store.query_state("ths", FLT.to_json())[0] == now


def test_alert_lists_belong_to_their_chat_and_expire(store):
    list_id = store.add_alert_list(10, "[1]")
    assert store.alert_list(list_id, 10) == "[1]"
    assert store.alert_list(list_id, 11) is None  # forged list id from another chat
    today = utcnow().date()
    assert store.prune(today + ALERT_LIST_TTL) == (0, 0, 0)
    assert store.prune(today + ALERT_LIST_TTL + timedelta(days=1)) == (0, 0, 1)
    assert store.alert_list(list_id, 10) is None


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


def test_pause_and_resume_only_from_the_other_state(store):
    sub = store.subscription(store.add_subscription(10, FLT), 10)
    store.finish_baseline(sub, "ths", [])
    # A stale Resume on a running subscription must not re-baseline it.
    assert not store.resume(sub.id, 10)
    assert set(store.watches(10)) == {(sub.id, "ths")}
    assert not store.pause(sub.id, 99)  # someone else's chat
    assert store.pause(sub.id, 10) and not store.pause(sub.id, 10)
    assert store.still_watching([(sub, "ths")]) == set()
    assert not store.resume(sub.id, 99)
    assert store.resume(sub.id, 10)
    assert store.subscription(sub.id, 10).status == "active"
    assert store.watches(10) == {}  # baselined again on every site


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
    created = "2026-10-17T16:27:00+00:00"
    old.execute(
        "INSERT INTO subscription VALUES (1, 10, 'ths', ?, 'active', ?), (2, 10, 'ths', ?, 'active', ?),"
        " (3, 10, 'ths', ?, 'baselining', ?), (4, 10, 'ths', ?, 'paused', ?)",
        (fj, created) * 4,
    )
    old.execute("INSERT INTO delivered VALUES (1, 'ths:a', '2030-01-01', 'x'), (2, 'ths:b', '2030-01-01', 'x')")
    old.execute("INSERT INTO delivered VALUES (1, 'ths:2131639', '2030-01-01', 'x'), (1, 'ths:2088621', '2030-01-01', 'x')")
    old.commit()
    old.close()

    store = Store(path)
    assert [s.id for s in store.subscriptions(10)] == [1, 2, 3, 4]
    assert [s.status for s in store.subscriptions(10)] == ["active", "active", "active", "paused"]
    assert store.delivered_keys(1) == {"ths:a", "ths:2131639", "ths:2088621"}
    assert store.delivered_keys(2) == {"ths:b"}
    # Active ones keep their baseline on their site: the time from created_at,
    # the watermark = highest delivered slot id. The rest get indexed first.
    watches = store.watches(10)
    assert set(watches) == {(1, "ths"), (2, "ths")}
    assert watches[(1, "ths")][0].isoformat() == created and watches[(1, "ths")][1] == 2131639
    store.delete(4, 10)
    assert store.add_subscription(10, FLT) == 5  # not 4 again
    store.delete(1, 10)
    assert store.delivered_keys(1) == set()  # the cascade still works
    store.close()
    Store(path).close()  # second open: already migrated, nothing to do


def test_production_shape_database_migrates_once_and_stays_migrated(tmp_path):
    """The schema of e80412c: AUTOINCREMENT, baseline columns, no watch table."""
    import sqlite3

    path = tmp_path / "prod.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE subscription (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            source TEXT NOT NULL, filter_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('baselining', 'active', 'paused')),
            created_at TEXT NOT NULL, baseline_at TEXT, watermark INTEGER);
        CREATE TABLE delivered (sub_id INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
            slot_key TEXT NOT NULL, end_date TEXT NOT NULL, at TEXT NOT NULL,
            PRIMARY KEY (sub_id, slot_key)) WITHOUT ROWID;
    """)
    old.execute(
        "INSERT INTO subscription VALUES (1, 10, 'ths', ?, 'active', '2026-10-17T10:00:00+00:00',"
        " '2026-10-17T16:27:12.475050+00:00', 2131840)",
        (FLT.to_json(),),
    )
    old.commit()
    old.close()

    store = Store(path)
    assert store.watches(10) == {
        (1, "ths"): (datetime.fromisoformat("2026-10-17T16:27:12.475050+00:00"), 2131840, False)
    }
    store.pause(1, 10)
    store.resume(1, 10)  # the baseline is gone now…
    store.close()
    store = Store(path)
    assert store.watches(10) == {}  # …and reopening doesn't bring the old one back
    store.close()
