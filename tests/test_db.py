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

    # Чужий chat_id з підробленою кнопкою нічого не змінює.
    assert not store.set_status(sub_id, 99, "paused")
    assert not store.delete(sub_id, 99)
    assert store.set_status(sub_id, 10, "paused")
    assert store.due_subscriptions() == []
    assert store.delete(sub_id, 10)
    assert store.subscriptions(10) == []


def test_finish_baseline_only_while_still_baselining(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    assert store.finish_baseline(sub_id, [("ths:1", date(2026, 12, 1))])
    assert store.delivered_keys(sub_id) == {"ths:1"}
    assert store.subscription(sub_id, 10).status == "active"

    other = store.add_subscription(10, "ths", FLT)
    store.set_status(other, 10, "paused")  # паузу натиснули посеред обходу
    assert not store.finish_baseline(other, [("ths:2", date(2026, 12, 1))])
    assert store.delivered_keys(other) == set()


def test_delete_cascades_and_marking_a_deleted_sub_is_a_noop(store):
    sub_id = store.add_subscription(10, "ths", FLT)
    store.mark_delivered(sub_id, [("ths:1", date(2026, 12, 1))])
    store.delete(sub_id, 10)
    assert store.conn.execute("SELECT count(*) FROM delivered").fetchone()[0] == 0
    store.mark_delivered(sub_id, [("ths:2", date(2026, 12, 1))])  # без IntegrityError
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
    store.mark_delivered(sub_id, [("ths:old", date(2026, 10, 1)), ("ths:new", date(2026, 12, 1))])
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
    assert store.redeem(format_code(code).lower(), 1)  # «abcd-efgh» теж приймається
    assert store.is_allowed(1)
    assert not store.redeem(code, 2)  # той самий код удруге — ні
    assert not store.is_allowed(2)
    assert not store.redeem("NOPE-NOPE", 2)
    assert code not in store.unused_codes() and len(store.unused_codes()) == 99


def test_code_normalization():
    assert normalize_code(" k7qm-4xpa ") == "K7QM4XPA"
    assert format_code("K7QM4XPA") == "K7QM-4XPA"
