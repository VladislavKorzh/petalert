"""Worker: every ~30 minutes, poll the sources and send out new date slots.

Key decisions:

* **Grouping.** Identical (source, filter) pairs across subscriptions share one
  HTTP request.
* **Page 1 vs full crawl.** New listings, and new dates on most listings, show
  up at the top of the `published desc` feed, so the 100 newest are usually
  enough (THS publishes ~50 listings an hour worldwide). But an owner can add
  dates to an old listing without bumping `published` (~0.5% of new slots,
  measured 2026-10-17), and only a full crawl finds those. So: a full crawl when
  a group has a new subscription (baseline), every `full_every` (3 h by
  default), and after more than 2 hours of downtime — at most a few scheduled
  ones per tick, oldest first, so they don't all land in the same tick. Filters
  with at most a few hundred results are crawled fully on every tick by the
  source itself. A watermark on `published` is deliberately not used: it is
  naive London time and breaks on DST.
* **Baseline.** A new (or resumed) subscription first silently marks everything
  that matches right now, and only then becomes `active`. Otherwise the first
  tick would be hundreds of alerts about old listings.
* **Mark after sending.** A failed send is not lost: a later tick finds the
  same slot again (the next one, or the next full crawl if only that saw it).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .db import Store, Subscription, utcnow
from .models import Filter, Listing, Slot, appeared_after, listing_matches, matching_slots, slot_key
from .sources.base import Source

log = logging.getLogger(__name__)

CATCHUP_AFTER = timedelta(hours=2)
MAX_SCHEDULED_FULL = 4  # time-based full crawls per tick; baselines don't count
TOP = 100  # listings a page-1 tick sees; fresh slots beyond it count as full_crawl_new
PRUNE_EVERY = timedelta(hours=24)
MAX_ALERTS_PER_CHAT = 10  # photo alerts per chat per tick; the rest go into one digest
MAX_DIGEST = 30  # digest entries; whatever doesn't fit goes out next tick
FAIL_STREAK_ALERT = 3  # ~1.5 hours of a source failing in a row — tell the admin


class ChatGone(Exception):
    """The user blocked the bot or the chat is gone — nowhere left to send."""


class Outbox(Protocol):
    async def alert(self, chat_id: int, listing: Listing, slots: list[Slot], sub_ids: list[int]) -> None: ...
    async def digest(self, chat_id: int, items: list[tuple[Listing, list[Slot]]]) -> int:
        """Returns how many of the first items actually fit into the message."""
        ...
    async def text(self, chat_id: int, text: str) -> None: ...


Admin = Callable[[str], Awaitable[None]]


@dataclass
class _Pending:
    """One listing for one chat: new slots from all of that chat's subscriptions."""

    listing: Listing
    subs: dict[int, Subscription] = field(default_factory=dict)
    by_sub: dict[int, dict[str, Slot]] = field(default_factory=dict)

    def add(self, sub: Subscription, slot: Slot) -> None:
        self.subs[sub.id] = sub
        self.by_sub.setdefault(sub.id, {})[slot_key(self.listing, slot)] = slot

    def keep(self, alive: set[int]) -> "_Pending | None":
        """Only the subscriptions still alive; None if nothing is left."""
        subs = {i: s for i, s in self.subs.items() if i in alive}
        if not subs:
            return None
        return _Pending(self.listing, subs, {i: self.by_sub[i] for i in subs})

    def ordered_slots(self) -> list[Slot]:
        slots = {k: s for per_sub in self.by_sub.values() for k, s in per_sub.items()}
        return sorted(slots.values(), key=lambda s: s.start)


@dataclass
class TickStats:
    groups: int = 0
    full: int = 0
    listings: int = 0
    fresh: int = 0
    sent: int = 0
    failed: int = 0
    full_crawl_new: int = 0  # new slots only a full crawl could see (beyond page 1)


class Worker:
    def __init__(
        self,
        store: Store,
        sources: dict[str, Source],
        outbox: Outbox,
        admin: Admin,
        interval: timedelta,
        *,
        full_every: timedelta = timedelta(hours=3),
        clock: Callable[[], datetime] = utcnow,
    ):
        self.store = store
        self.sources = sources
        self.outbox = outbox
        self.admin = admin
        self.interval = interval
        self.full_every = full_every
        self.clock = clock
        self.fail_streak: dict[str, int] = {}
        self._full_attempt: dict[tuple[str, str], datetime] = {}  # last failed full crawl
        self._wake = asyncio.Event()
        self._next_poll: datetime | None = None
        self._last_prune: datetime | None = None

    def wake(self) -> None:
        """New subscription or resume — baseline right away, not in half an hour."""
        self._wake.set()

    async def run(self) -> None:
        while True:
            woken = self._wake.is_set()
            self._wake.clear()
            try:
                now = self.clock()
                if self._next_poll is None or now >= self._next_poll:
                    await self.tick()
                    self._next_poll = self.clock() + self.interval * random.uniform(0.9, 1.1)
                elif woken:
                    # Only on a signal, not "while anything is baselining": if the
                    # source is down, that would be a hot request loop.
                    await self.tick(only_baselining=True)
                if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY:
                    delivered, states = self.store.prune(now.date())
                    self._last_prune = now
                    log.info("prune: delivered=%d query_state=%d", delivered, states)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("tick crashed")
                await self._admin(f"Worker tick crashed: {exc!r}")
                if self._next_poll is None or self._next_poll <= self.clock():
                    self._next_poll = self.clock() + self.interval
            assert self._next_poll is not None
            timeout = max(0.0, (self._next_poll - self.clock()).total_seconds())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass

    async def _admin(self, text: str) -> None:
        try:
            await self.admin(text)
        except Exception:
            log.exception("failed to message the admin: %s", text)

    def _full_groups(
        self, groups: dict[tuple[str, str], list[Subscription]], now: datetime
    ) -> tuple[set, set]:
        """Groups to crawl fully this tick: every baseline, plus the most
        overdue scheduled ones (a crawl of a broad filter is 20+ requests).
        Also returns the overdue groups left for later, so a page-1 tick
        doesn't clear their pending catch-up."""
        full = {g for g, subs in groups.items() if any(s.status == "baselining" for s in subs)}
        oldest = datetime.min.replace(tzinfo=now.tzinfo)
        overdue = []
        for group in groups.keys() - full:
            last_ok, last_full = self.store.query_state(*group)
            if (
                last_full is None
                or now - last_full >= self.full_every
                or last_ok is None
                or now - last_ok >= CATCHUP_AFTER
            ):
                # A group whose full crawl keeps failing goes to the back.
                tried = max(last_full or oldest, self._full_attempt.get(group, oldest))
                overdue.append((tried, group))
        overdue.sort()
        if len(overdue) > MAX_SCHEDULED_FULL:
            log.info("%d full crawls due, doing %d now", len(overdue), MAX_SCHEDULED_FULL)
        full.update(group for _, group in overdue[:MAX_SCHEDULED_FULL])
        return full, {group for _, group in overdue[MAX_SCHEDULED_FULL:]}

    async def tick(self, *, only_baselining: bool = False) -> TickStats:
        now = self.clock()
        today = now.date()
        stats = TickStats()
        groups: dict[tuple[str, str], list[Subscription]] = {}
        for sub in self.store.due_subscriptions():
            if sub.filter.date_to and sub.filter.date_to <= today:
                await self._expire(sub)
                continue
            groups.setdefault(sub.group, []).append(sub)
        if only_baselining:
            groups = {k: v for k, v in groups.items() if any(s.status == "baselining" for s in v)}

        pending: dict[int, dict[tuple[str, str], _Pending]] = {}
        failures: dict[str, Exception] = {}
        succeeded: set[str] = set()
        full_groups, deferred = self._full_groups(groups, now)
        for (source_key, filter_json), subs in groups.items():
            source = self.sources.get(source_key)
            if source is None:
                log.error("unknown source %r in subscriptions %s", source_key, [s.id for s in subs])
                continue
            flt = Filter.from_json(filter_json)
            group = (source_key, filter_json)
            full = group in full_groups
            partial = False  # page 1 instead of a failed full crawl: no baselines from it
            stats.groups += 1
            stats.full += full
            try:
                listings = await source.search(flt, max_pages=None if full else 1)
            except Exception as exc:
                log.warning("%s %s: %r", source_key, filter_json, exc)
                failures[source_key] = exc
                stats.failed += 1
                if not full:
                    continue
                # A failing full crawl must not also stop the page-1 alerts.
                self._full_attempt[group] = now
                try:
                    listings = await source.search(flt, max_pages=1)
                except Exception:
                    continue
                full, partial = False, True
            else:
                succeeded.add(source_key)
                if full or group not in deferred:
                    # (a deferred catch-up stays pending until its full crawl runs)
                    self.store.touch_query(source_key, filter_json, now, full=full)
            stats.listings += len(listings)

            top = {l.id for l in listings[:TOP]}
            matches: list[tuple[Listing, Slot]] = []
            dropped = 0
            for listing in listings:
                if listing.country in flt.exclude_countries:
                    continue  # expected: exclusions are not sent to the API
                slots = matching_slots(listing, flt, today) if listing_matches(listing, flt) else []
                dropped += not slots
                matches.extend((listing, slot) for slot in slots)
            if dropped:
                # The API should only return matches; mass disagreement is a sign
                # it has started ignoring one of the filters.
                log.warning("%s %s: local filter dropped %d of %d", source_key, filter_json, dropped, len(listings))

            watermark = max((s.seq for l in listings for s in l.slots if s.seq is not None), default=None)
            for sub in subs:
                known = self.store.delivered_keys(sub.id)
                fresh = [(l, s) for l, s in matches if slot_key(l, s) not in known]
                if sub.status == "baselining":
                    if partial:
                        continue  # a baseline needs the full crawl; retried next tick
                    keys = [(slot_key(l, s), s.end) for l, s in fresh]
                    if self.store.finish_baseline(sub, keys, watermark=watermark, at=now):
                        n = len({l.id for l, _ in matches})
                        await self._text(
                            sub.chat_id,
                            f"✅ <b>Subscription #{sub.id} is live</b>\n\n"
                            f"🔎 {n} listings match right now — consider these already seen.\n"
                            "🔔 I'll message you as soon as new dates appear.",
                        )
                    continue
                # Unseen but old (the baseline missed it, or it just came into
                # reach): note it silently instead of alerting.
                old = [(l, s) for l, s in fresh if not appeared_after(l, s, sub.baseline_at, sub.watermark)]
                if old:
                    self.store.mark_delivered(sub, [(slot_key(l, s), s.end) for l, s in old])
                    log.info("#%d: %d old slots seen for the first time, not alerted", sub.id, len(old))
                    fresh = [f for f in fresh if f not in old]
                stats.fresh += len(fresh)
                if full:
                    stats.full_crawl_new += sum(l.id not in top for l, _ in fresh)
                chat = pending.setdefault(sub.chat_id, {})
                for listing, slot in fresh:
                    chat.setdefault((listing.source, listing.id), _Pending(listing)).add(sub, slot)

        # The searches took a while; drop subscriptions paused or deleted meanwhile.
        alive = self.store.still_active(
            sub for items in pending.values() for item in items.values() for sub in item.subs.values()
        )
        for chat_id, items in pending.items():
            kept = [p for p in (item.keep(alive) for item in items.values()) if p]
            if kept:
                stats.sent += await self._deliver(chat_id, kept)
        await self._health(failures, succeeded)
        log.info(
            "tick%s: groups=%d full=%d listings=%d fresh=%d sent=%d failed=%d full_crawl_new=%d",
            " (baseline)" if only_baselining else "", stats.groups, stats.full, stats.listings,
            stats.fresh, stats.sent, stats.failed, stats.full_crawl_new,
        )
        return stats

    async def _expire(self, sub: Subscription) -> None:
        """A date window in the past can't match anything any more: stop polling
        for it (each request costs proxy traffic) and tell the user once."""
        if self.store.transition(sub.id, sub.chat_id, "paused", allowed_from=("active", "baselining")):
            await self._text(
                sub.chat_id,
                f"⌛ <b>Subscription #{sub.id} has ended</b>\n\n"
                "Its date window is over, so I've paused it.\n"
                "📋 /list to delete it · ➕ /new for a fresh one.",
            )

    async def _text(self, chat_id: int, text: str) -> None:
        try:
            await self.outbox.text(chat_id, text)
        except ChatGone:
            self.store.pause_chat(chat_id)
            log.info("chat %s is gone — subscriptions paused", chat_id)
        except Exception:
            log.exception("failed to message chat %s", chat_id)

    async def _deliver(self, chat_id: int, items: list[_Pending]) -> int:
        sent = 0
        try:
            for item in items[:MAX_ALERTS_PER_CHAT]:
                try:
                    await self.outbox.alert(chat_id, item.listing, item.ordered_slots(), sorted(item.subs))
                except ChatGone:
                    raise
                except Exception:
                    log.exception("alert %s to chat %s failed — will retry next tick", item.listing.id, chat_id)
                    continue
                self._mark(item)
                sent += 1
            rest = items[MAX_ALERTS_PER_CHAT:]
            included = 0
            if rest:
                try:
                    included = await self.outbox.digest(
                        chat_id, [(i.listing, i.ordered_slots()) for i in rest[:MAX_DIGEST]]
                    )
                except ChatGone:
                    raise
                except Exception:
                    log.exception("digest to chat %s failed", chat_id)
                for item in rest[:included]:
                    self._mark(item)
                sent += included
        except ChatGone:
            self.store.pause_chat(chat_id)
            log.info("chat %s is gone — subscriptions paused", chat_id)
        return sent

    def _mark(self, item: _Pending) -> None:
        for sub_id, slots in item.by_sub.items():
            self.store.mark_delivered(item.subs[sub_id], [(k, s.end) for k, s in slots.items()])

    async def _health(self, failures: dict[str, Exception], succeeded: set[str]) -> None:
        for source_key in failures.keys() | succeeded:
            streak = self.fail_streak.get(source_key, 0)
            if source_key in failures:
                streak += 1
                if streak == FAIL_STREAK_ALERT:
                    await self._admin(
                        f"⚠️ {source_key} failing for {streak} ticks in a row. Last error: "
                        f"{str(failures[source_key])[:300]}"
                    )
            else:
                if streak >= FAIL_STREAK_ALERT:
                    await self._admin(f"✅ {source_key} is back after {streak} failed ticks.")
                streak = 0
            self.fail_streak[source_key] = streak
