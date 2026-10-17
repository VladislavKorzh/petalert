"""The worker on real SQLite with a fake source and fake Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from petalert.db import Store
from petalert.models import Filter, Listing, Slot
from petalert.scheduler import MAX_ALERTS_PER_CHAT, ChatGone, Worker
from petalert.sources.base import SourceError

FLT = Filter("europe")


def slot(sid: str, start: str = "2026-12-01", end: str = "2026-12-10", is_open: bool = True) -> Slot:
    return Slot(sid, date.fromisoformat(start), date.fromisoformat(end), is_open)


def listing(lid: str, *slots: Slot, country: str = "portugal") -> Listing:
    return Listing(
        source="ths", id=lid, title=f"L{lid}", url=f"u/{lid}", continent="europe",
        country=country, place="p", photo_url=None, pets=(("cat", 1),), slots=slots,
    )


class FakeSource:
    key = "ths"
    title = "Fake"

    def __init__(self):
        self.listings: list[Listing] = []
        self.calls: list[int | None] = []
        self.error: Exception | None = None

    async def search(self, flt, *, max_pages):
        self.calls.append(max_pages)
        if self.error:
            raise self.error
        return list(self.listings)

    async def countries(self, continent):
        return []


class FakeOutbox:
    def __init__(self):
        self.alerts: list[tuple[int, str, list[str], list[int]]] = []
        self.digests: list[tuple[int, list[str]]] = []
        self.texts: list[tuple[int, str]] = []
        self.gone: set[int] = set()
        self.fail_alerts = False

    def _check(self, chat_id):
        if chat_id in self.gone:
            raise ChatGone

    async def alert(self, chat_id, listing, slots, sub_ids):
        self._check(chat_id)
        if self.fail_alerts:
            raise RuntimeError("telegram down")
        self.alerts.append((chat_id, listing.id, [s.id for s in slots], sub_ids))

    async def digest(self, chat_id, items):
        self._check(chat_id)
        self.digests.append((chat_id, [l.id for l, _ in items]))
        return len(items)

    async def text(self, chat_id, text):
        self._check(chat_id)
        self.texts.append((chat_id, text))


class Env:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "db.sqlite3")
        self.source = FakeSource()
        self.outbox = FakeOutbox()
        self.admin: list[str] = []
        self.now = datetime(2026, 10, 17, 12, tzinfo=UTC)

        async def admin(text):
            self.admin.append(text)

        self.worker = Worker(
            self.store, {"ths": self.source}, self.outbox, admin, timedelta(minutes=30),
            clock=lambda: self.now,
        )

    def tick(self, **kw):
        return asyncio.run(self.worker.tick(**kw))

    def advance(self, minutes: int = 30):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_baseline_sends_nothing_then_only_new_slots(env):
    env.source.listings = [listing("1", slot("a")), listing("2", slot("b"), slot("x", is_open=False))]
    sub = env.store.add_subscription(10, "ths", FLT)

    env.tick()
    assert env.outbox.alerts == []
    ((chat, text),) = env.outbox.texts
    assert chat == 10 and f"Subscription #{sub} is live" in text and "2 listings match" in text
    assert env.store.subscription(sub, 10).status == "active"

    env.advance()
    env.source.listings.insert(0, listing("3", slot("c")))
    env.tick()
    assert env.outbox.alerts == [(10, "3", ["c"], [sub])]

    env.advance()
    env.tick()  # nothing new — nothing sent
    assert len(env.outbox.alerts) == 1


def test_republished_listing_alerts_only_its_new_dates(env):
    env.source.listings = [listing("1", slot("old"))]
    sub = env.store.add_subscription(10, "ths", FLT)
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("old"), slot("new", "2027-01-05", "2027-01-20"))]
    env.tick()
    assert env.outbox.alerts == [(10, "1", ["new"], [sub])]


def test_reopened_slot_is_alerted(env):
    env.source.listings = [listing("1", slot("a", is_open=False))]
    env.store.add_subscription(10, "ths", FLT)
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("a"))]
    env.tick()
    assert [a[2] for a in env.outbox.alerts] == [["a"]]


def test_two_subscriptions_of_one_chat_get_one_message(env):
    env.source.listings = []
    a = env.store.add_subscription(10, "ths", FLT)
    b = env.store.add_subscription(10, "ths", Filter("europe", country="portugal"))
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    env.tick()
    assert env.outbox.alerts == [(10, "1", ["s"], [a, b])]
    assert env.store.delivered_keys(a) == env.store.delivered_keys(b) == {"ths:s"}


def test_identical_filters_share_one_request(env):
    env.store.add_subscription(10, "ths", FLT)
    env.store.add_subscription(20, "ths", FLT)
    env.tick()
    assert len(env.source.calls) == 1


def test_failed_send_is_retried_next_tick(env):
    env.store.add_subscription(10, "ths", FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    env.outbox.fail_alerts = True
    env.tick()
    assert env.outbox.alerts == []

    env.advance()
    env.outbox.fail_alerts = False
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["1"]


def test_blocked_bot_pauses_the_chat(env):
    sub = env.store.add_subscription(10, "ths", FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    env.outbox.gone.add(10)
    env.tick()
    assert env.store.subscription(sub, 10).status == "paused"


def test_resume_rebaselines_silently(env):
    sub = env.store.add_subscription(10, "ths", FLT)
    env.tick()
    env.store.set_status(sub, 10, "paused")

    env.advance()
    env.source.listings = [listing("1", slot("during_pause"))]
    env.tick()

    env.store.set_status(sub, 10, "baselining")  # this is what the Resume button does
    env.advance()
    env.tick(only_baselining=True)
    assert env.outbox.alerts == []
    assert env.store.delivered_keys(sub) == {"ths:during_pause"}


def test_overflow_goes_into_one_digest(env):
    env.store.add_subscription(10, "ths", FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing(str(i), slot(f"s{i}")) for i in range(MAX_ALERTS_PER_CHAT + 3)]
    env.tick()
    assert len(env.outbox.alerts) == MAX_ALERTS_PER_CHAT
    assert env.outbox.digests == [(10, [str(i) for i in range(MAX_ALERTS_PER_CHAT, MAX_ALERTS_PER_CHAT + 3)])]


def test_full_crawl_decision(env):
    env.store.add_subscription(10, "ths", FLT)
    env.tick()  # baseline — full
    env.advance(30)
    env.tick()  # regular — page 1
    env.advance(3 * 60)
    env.tick()  # downtime over 2 h — full
    env.advance(30)
    env.tick()
    env.now += timedelta(hours=24)
    env.tick()  # daily
    assert env.source.calls == [None, 1, None, 1, None]


def test_only_baselining_tick_skips_settled_groups(env):
    env.store.add_subscription(10, "ths", FLT)
    env.tick()
    env.store.add_subscription(10, "ths", Filter("asia"))
    env.advance(1)
    env.tick(only_baselining=True)
    assert env.source.calls == [None, None]  # only the new group


def test_local_filter_guards_against_api_ignoring_a_filter(env):
    env.store.add_subscription(10, "ths", Filter("europe", country="portugal"))
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"), country="spain")]
    env.tick()
    assert env.outbox.alerts == []


def test_health_alert_after_three_failed_ticks_and_recovery(env):
    env.store.add_subscription(10, "ths", FLT)
    env.source.error = SourceError("THS HTTP 500: boom")
    for _ in range(4):
        env.tick()
        env.advance()
    assert len(env.admin) == 1 and "failing for 3 ticks" in env.admin[0] and "boom" in env.admin[0]

    env.source.error = None
    env.tick()
    assert len(env.admin) == 2 and "is back" in env.admin[1]
    # The subscription was still waiting for its baseline — it only becomes active now, without spam.
    assert env.outbox.alerts == []


def test_run_loop_polls_at_start_and_baselines_on_wake(env):
    async def scenario():
        task = asyncio.create_task(env.worker.run())
        await asyncio.sleep(0.05)
        assert env.source.calls == [None]  # startup tick: baseline of the "asia" subscription
        sub = env.store.add_subscription(10, "ths", FLT)
        env.worker.wake()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return sub

    env.store.add_subscription(20, "ths", Filter("asia"))
    sub = asyncio.run(scenario())
    assert env.store.subscription(sub, 10).status == "active"
    assert env.source.calls == [None, None]


def test_excluded_country_never_alerts(env):
    env.store.add_subscription(10, "ths", Filter("europe", exclude_countries=("united-kingdom",)))
    env.tick()
    env.advance()
    env.source.listings = [
        listing("uk", slot("a"), country="united-kingdom"),
        listing("pt", slot("b"), country="portugal"),
    ]
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["pt"]
