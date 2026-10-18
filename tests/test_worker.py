"""The worker on real SQLite with a fake source and fake Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from petalert.db import Store, Watch
from petalert.models import Filter, Listing, Slot
from petalert.scheduler import MAX_LIST, ChatGone, Worker
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
        self.lists: list[tuple[int, list[tuple[str, list[str], list[int]]]]] = []
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

    async def listings(self, chat_id, items):
        self._check(chat_id)
        if self.fail_alerts:
            raise RuntimeError("telegram down")
        self.lists.append((chat_id, [(l.id, [s.id for s in slots], subs) for l, slots, subs in items]))

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
    sub = env.store.add_subscription(10, FLT)

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
    sub = env.store.add_subscription(10, FLT)
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("old"), slot("new", "2027-01-05", "2027-01-20"))]
    env.tick()
    assert env.outbox.alerts == [(10, "1", ["new"], [sub])]


def test_reopened_slot_is_alerted(env):
    env.source.listings = [listing("1", slot("a", is_open=False))]
    env.store.add_subscription(10, FLT)
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("a"))]
    env.tick()
    assert [a[2] for a in env.outbox.alerts] == [["a"]]


def test_two_subscriptions_of_one_chat_get_one_message(env):
    env.source.listings = []
    a = env.store.add_subscription(10, FLT)
    b = env.store.add_subscription(10, Filter("europe", country="portugal"))
    env.tick()

    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    env.tick()
    assert env.outbox.alerts == [(10, "1", ["s"], [a, b])]
    assert env.store.delivered_keys(a) == env.store.delivered_keys(b) == {"ths:s"}


def test_identical_filters_share_one_request(env):
    env.store.add_subscription(10, FLT)
    env.store.add_subscription(20, FLT)
    env.tick()
    assert len(env.source.calls) == 1


def test_failed_send_is_retried_next_tick(env):
    env.store.add_subscription(10, FLT)
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
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    env.outbox.gone.add(10)
    env.tick()
    assert env.store.subscription(sub, 10).status == "paused"


def test_resume_rebaselines_silently(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.store.pause(sub, 10)

    env.advance()
    env.source.listings = [listing("1", slot("during_pause"))]
    env.tick()

    assert env.store.resume(sub, 10)  # what the Resume button does
    env.advance()
    env.tick(only_baselining=True)
    assert env.outbox.alerts == []
    assert env.store.delivered_keys(sub) == {"ths:during_pause"}


def test_several_listings_go_out_as_one_table_nearest_dates_first(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [
        listing("late", slot("l", "2027-03-01", "2027-03-10")),
        listing("two", slot("t2", "2027-01-01", "2027-01-05"), slot("t1", "2026-12-05", "2026-12-20")),
        listing("soon", slot("s", "2026-12-05", "2026-12-12")),
    ]
    env.tick()
    assert env.outbox.alerts == []
    assert env.outbox.lists == [
        (10, [("soon", ["s"], [sub]), ("two", ["t1", "t2"], [sub]), ("late", ["l"], [sub])])
    ]
    assert env.store.delivered_keys(sub) >= {"ths:s", "ths:t1", "ths:t2", "ths:l"}


def test_table_overflow_goes_out_next_tick(env):
    env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [
        listing(str(i), slot(f"s{i}", f"2027-01-{i % 28 + 1:02d}", "2027-02-01")) for i in range(MAX_LIST + 2)
    ]
    env.tick()
    ((_, first),) = env.outbox.lists
    assert len(first) == MAX_LIST

    env.advance()
    env.tick()
    assert len(env.outbox.lists) == 2 and len(env.outbox.lists[1][1]) == 2  # one table: 2 left
    assert {i for _, items in env.outbox.lists for i, _, _ in items} == {str(i) for i in range(MAX_LIST + 2)}


def test_failed_table_is_retried_whole(env):
    env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("a")), listing("2", slot("b"))]
    env.outbox.fail_alerts = True
    env.tick()
    env.advance()
    env.outbox.fail_alerts = False
    env.tick()
    assert [[i for i, _, _ in items] for _, items in env.outbox.lists] == [["1", "2"]]


def test_blocked_bot_pauses_the_chat_on_a_table_too(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("a")), listing("2", slot("b"))]
    env.outbox.gone.add(10)
    env.tick()
    assert env.store.subscription(sub, 10).status == "paused"


def test_full_crawl_decision(env):
    env.store.add_subscription(10, FLT)
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
    env.store.add_subscription(10, FLT)
    env.tick()
    env.store.add_subscription(10, Filter("asia"))
    env.advance(1)
    env.tick(only_baselining=True)
    assert env.source.calls == [None, None]  # only the new group


def test_local_filter_guards_against_api_ignoring_a_filter(env):
    env.store.add_subscription(10, Filter("europe", country="portugal"))
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"), country="spain")]
    env.tick()
    assert env.outbox.alerts == []


def test_health_alert_after_three_failed_ticks_and_recovery(env):
    env.store.add_subscription(10, FLT)
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
        sub = env.store.add_subscription(10, FLT)
        env.worker.wake()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return sub

    env.store.add_subscription(20, Filter("asia"))
    sub = asyncio.run(scenario())
    assert env.store.subscription(sub, 10).status == "active"
    assert env.source.calls == [None, None]


def test_excluded_country_never_alerts(env):
    env.store.add_subscription(10, Filter("europe", exclude_countries=("united-kingdom",)))
    env.tick()
    env.advance()
    env.source.listings = [
        listing("uk", slot("a"), country="united-kingdom"),
        listing("pt", slot("b"), country="portugal"),
    ]
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["pt"]


def test_full_crawl_every_three_hours(env):
    env.store.add_subscription(10, FLT)
    env.tick()  # baseline
    for _ in range(6):
        env.advance(30)
        env.tick()
    assert env.source.calls == [None, 1, 1, 1, 1, 1, None]


def test_expired_date_window_pauses_and_tells_once(env):
    sub = env.store.add_subscription(10, Filter("europe", date_to=date(2026, 10, 17)))
    env.tick()
    assert env.store.subscription(sub, 10).status == "paused"
    assert env.source.calls == []  # no request for a window that can't match
    assert "has ended" in env.outbox.texts[-1][1]
    env.advance()
    env.tick()
    assert len(env.outbox.texts) == 1


def test_subscription_paused_while_the_tick_was_fetching_gets_nothing(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    real_search = env.source.search

    async def search_then_pause(flt, *, max_pages):
        result = await real_search(flt, max_pages=max_pages)
        env.store.pause(sub, 10)  # the user taps Pause mid-tick
        return result

    env.source.search = search_then_pause
    env.tick()
    assert env.outbox.alerts == []


def test_failed_send_is_retried_without_forcing_full_crawls(env):
    # A persistent failure (Telegram down for hours) must not turn every tick
    # into a 20+ request full crawl.
    env.store.add_subscription(10, FLT)
    env.tick()
    env.source.listings = [listing("1", slot("s"))]
    env.outbox.fail_alerts = True
    for _ in range(3):
        env.advance()
        env.tick()
    assert env.source.calls[1:] == [1, 1, 1]
    env.advance()
    env.outbox.fail_alerts = False
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["1"]


def test_scheduled_full_crawls_are_capped_per_tick(env):
    from petalert.scheduler import MAX_SCHEDULED_FULL

    for country in ("a", "b", "c", "d", "e", "f"):
        env.store.add_subscription(10, Filter("europe", country=country))
    env.tick()  # six baselines: all full, no cap
    assert env.source.calls == [None] * 6
    env.now += timedelta(hours=3)
    env.tick()
    assert env.source.calls[6:].count(None) == MAX_SCHEDULED_FULL
    env.advance()
    env.tick()  # the remaining two get their turn
    assert env.source.calls[12:].count(None) == 2


def test_full_crawl_new_counts_only_what_page_one_could_not_see(env):
    env.store.add_subscription(10, FLT)
    env.tick()
    env.source.listings = [listing(str(i), slot(f"s{i}")) for i in range(120)]
    env.now += timedelta(hours=3)
    stats = env.tick()
    assert stats.fresh == 120 and stats.full_crawl_new == 20


def test_failing_full_crawl_still_delivers_page_one_and_yields_its_slot(env):
    env.store.add_subscription(10, FLT)
    env.tick()
    real = env.source.search

    async def full_breaks(flt, *, max_pages):
        if max_pages is None:
            raise SourceError("THS ignores filters.published")
        return await real(flt, max_pages=max_pages)

    env.source.search = full_breaks
    env.source.listings = [listing("1", slot("s"))]
    env.now += timedelta(hours=3)
    stats = env.tick()
    assert stats.failed == 1 and [a[1] for a in env.outbox.alerts] == ["1"]


def test_deferred_catch_up_is_not_lost(env):
    from petalert.scheduler import MAX_SCHEDULED_FULL

    for country in "abcdef":
        env.store.add_subscription(10, Filter("europe", country=country))
    env.tick()
    env.now += timedelta(hours=2, minutes=30)  # downtime: everyone needs a catch-up
    env.tick()
    env.advance(1)  # well before the 3 h schedule would make them due again
    env.tick()  # the two deferred groups still get their catch-up crawl
    assert env.source.calls[6:].count(None) == 6


def test_failing_full_crawl_never_finishes_a_baseline_on_page_one(env):
    old = env.store.add_subscription(10, FLT)
    env.tick()
    new = env.store.add_subscription(20, FLT)  # same group, still baselining
    real = env.source.search

    async def full_breaks(flt, *, max_pages):
        if max_pages is None:
            raise SourceError("boom")
        return await real(flt, max_pages=max_pages)

    env.source.search = full_breaks
    env.source.listings = [listing("1", slot("s"))]
    env.advance()
    env.tick()
    assert [a[:2] for a in env.outbox.alerts] == [(10, "1")]  # the active one still hears about it
    assert set(env.store.watches()) == {(old, "ths")}  # the new one is still unbaselined


def test_old_listing_seen_for_the_first_time_is_not_news(env):
    from datetime import UTC, datetime

    def dated(lid, published, *slots):
        return Listing(source="ths", id=lid, title=lid, url=f"u/{lid}", continent="europe",
                       country="sweden", place="p", photo_url=None, pets=(), slots=slots,
                       published=published)

    def seq_slot(n):
        return Slot(str(n), date(2026, 12, 12), date(2027, 1, 10), True, seq=n)

    long_ago = datetime(2026, 10, 8, 8, 23, tzinfo=UTC)
    env.source.listings = [dated("seen", long_ago, seq_slot(2131000))]
    sub = env.store.add_subscription(10, FLT)
    env.tick()  # baseline sees ids up to 2131000; "stan" is out of reach
    assert env.store.watches(10)[(sub, "ths")][1] == 2131000

    env.advance()
    later = env.now + timedelta(minutes=5)
    env.source.listings += [
        dated("stan", long_ago, seq_slot(2111592)),         # old listing, old slot: just unseen
        dated("newdates", long_ago, seq_slot(2131900)),     # old listing, dates added now
        dated("republished", later, seq_slot(2083944)),      # re-published after the baseline
    ]
    env.tick()
    assert env.outbox.alerts == []
    ((_, items),) = env.outbox.lists
    assert sorted(i for i, _, _ in items) == ["newdates", "republished"]
    assert "ths:2111592" in env.store.delivered_keys(sub)  # noted silently, never alerted later


class SecondSource(FakeSource):
    key = "mmh"
    title = "Second"


def test_each_site_is_baselined_on_its_own_and_a_site_turned_on_later_sends_nothing_old(env):
    second = SecondSource()
    env.worker.sources["mmh"] = second
    env.source.listings = [listing("1", slot("a"))]
    second.listings = [Listing(**{**listing("m1", slot("x")).__dict__, "source": "mmh"})]
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    ((_, live),) = env.outbox.texts
    assert f"Subscription #{sub} is live" in live and "Fake: 1 listings" in live and "Second: 1 listings" in live

    env.store.set_source(10, "mmh", False)
    env.advance()
    second.listings.append(Listing(**{**listing("m2", slot("y")).__dict__, "source": "mmh"}))
    env.tick()
    assert env.outbox.alerts == [] and len(second.calls) == 1  # off: not even fetched

    env.store.set_source(10, "mmh", True)
    env.advance()
    env.tick()  # "m2" appeared while the site was off: indexed silently, not sent
    assert env.outbox.alerts == []
    assert "is live on Second" in env.outbox.texts[-1][1]

    env.advance()
    second.listings.append(Listing(**{**listing("m3", slot("z")).__dict__, "source": "mmh"}))
    env.source.listings.append(listing("2", slot("b")))
    env.tick()
    assert sorted(a[1] for a in env.outbox.alerts) == [] and len(env.outbox.lists) == 1
    assert sorted(i for i, _, _ in env.outbox.lists[0][1]) == ["2", "m3"]


def test_site_turned_off_mid_tick_gets_nothing(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("s"))]
    real = env.source.search

    async def off_meanwhile(flt, *, max_pages):
        env.store.set_source(10, "ths", False)
        return await real(flt, max_pages=max_pages)

    env.source.search = off_meanwhile
    env.tick()
    assert env.outbox.alerts == [] and env.store.delivered_keys(sub) == set()


def test_complete_sources_are_full_every_tick_without_taking_scheduled_slots(env):
    env.source.always_complete = True
    for i in range(6):
        env.store.add_subscription(10, Filter("europe", min_days=i + 1))
    env.tick()
    env.now += timedelta(hours=5)  # every group overdue for a scheduled full crawl
    groups = {}
    for sub in env.store.due_subscriptions():
        groups[("ths", sub.filter_json)] = [Watch(sub, "ths", env.now)]
    assert env.worker._full_groups(groups, env.now) == (set(), set())  # no slot taken
    stats = env.tick()
    assert stats.full == 6 and stats.failed == 0  # yet each search counts as a full crawl


def mmh(lid: str, *slots: Slot) -> Listing:
    return Listing(**{**listing(lid, *slots).__dict__, "source": "mmh"})


def test_one_site_off_mid_tick_only_drops_that_sites_alerts(env):
    second = SecondSource()
    env.worker.sources["mmh"] = second
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.advance()
    env.source.listings = [listing("1", slot("a"))]
    second.listings = [mmh("m1", slot("x"))]
    real = second.search

    async def off_meanwhile(flt, *, max_pages):
        env.store.set_source(10, "mmh", False)
        return await real(flt, max_pages=max_pages)

    second.search = off_meanwhile
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["1"] and env.outbox.lists == []
    assert env.store.delivered_keys(sub) == {"ths:a"}


def test_reindex_marks_quietly_and_keeps_alerting_after(env):
    sub = env.store.add_subscription(10, FLT)
    env.tick()
    env.outbox.texts.clear()
    env.store.reindex("ths")
    env.advance()
    env.source.listings = [listing("old", slot("o"))]  # e.g. newly matchable after a code change
    env.tick()
    assert env.outbox.alerts == [] and env.outbox.texts == []  # no "is live" either
    assert "ths:o" in env.store.delivered_keys(sub)
    env.advance()
    env.source.listings.append(listing("new", slot("n")))
    env.tick()
    assert [a[1] for a in env.outbox.alerts] == ["new"]


def test_no_live_message_for_a_subscription_paused_mid_tick(env):
    sub = env.store.add_subscription(10, FLT)
    real = env.source.search

    async def pause_meanwhile(flt, *, max_pages):
        result = await real(flt, max_pages=max_pages)
        env.store.pause(sub, 10)
        return result

    env.source.search = pause_meanwhile
    env.tick()
    assert env.outbox.texts == []


def test_wake_ticks_do_not_count_towards_a_failure_streak(env):
    env.store.add_subscription(10, FLT)
    env.source.error = SourceError("down")
    for _ in range(5):
        env.tick(only_baselining=True)
    assert env.worker.fail_streak.get("ths", 0) == 0 and env.admin == []
