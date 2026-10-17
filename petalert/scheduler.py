"""Worker: every ~30 minutes, poll the sources and send out new date slots.

Key decisions:

* **Grouping.** Identical (source, filter) pairs across subscriptions share one
  HTTP request.
* **Page 1 vs full crawl.** The 100 newest listings are usually enough (THS
  publishes ~50 listings an hour worldwide). A full crawl happens when a group
  has a new subscription (baseline), once a day, and after more than 2 hours of
  downtime. A watermark on `published` is deliberately not used: it is naive
  London time and breaks on DST.
* **Baseline.** A new (or resumed) subscription first silently marks everything
  that matches right now, and only then becomes `active`. Otherwise the first
  tick would be hundreds of alerts about old listings. THS only returns the
  1000 newest results, so for a broad filter the baseline covers exactly those;
  an older listing triggers an alert only once it is re-published and moves to
  the top of the feed, which is worth knowing about anyway.
* **Mark after sending.** A failed send is not lost: the next tick finds the
  same slot again.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from .db import Store, Subscription, utcnow
from .models import Filter, Listing, Slot, listing_matches, matching_slots, slot_key
from .sources.base import Source

log = logging.getLogger(__name__)

FULL_EVERY = timedelta(hours=24)
CATCHUP_AFTER = timedelta(hours=2)
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
    slots: dict[str, Slot] = field(default_factory=dict)
    by_sub: dict[int, list[tuple[str, date]]] = field(default_factory=dict)

    def add(self, sub_id: int, slot: Slot) -> None:
        key = slot_key(self.listing, slot)
        self.slots.setdefault(key, slot)
        self.by_sub.setdefault(sub_id, []).append((key, slot.end))

    def ordered_slots(self) -> list[Slot]:
        return sorted(self.slots.values(), key=lambda s: s.start)


@dataclass
class TickStats:
    groups: int = 0
    full: int = 0
    listings: int = 0
    fresh: int = 0
    sent: int = 0
    failed: int = 0
    full_crawl_new: int = 0  # new slots found by a full crawl (see risks in README)


class Worker:
    def __init__(
        self,
        store: Store,
        sources: dict[str, Source],
        outbox: Outbox,
        admin: Admin,
        interval: timedelta,
        *,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.store = store
        self.sources = sources
        self.outbox = outbox
        self.admin = admin
        self.interval = interval
        self.clock = clock
        self.fail_streak: dict[str, int] = {}
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

    def _needs_full(self, subs: list[Subscription], now: datetime) -> bool:
        if any(s.status == "baselining" for s in subs):
            return True
        last_ok, last_full = self.store.query_state(*subs[0].group)
        return (
            last_full is None
            or now - last_full >= FULL_EVERY
            or last_ok is None
            or now - last_ok >= CATCHUP_AFTER
        )

    async def tick(self, *, only_baselining: bool = False) -> TickStats:
        now = self.clock()
        today = now.date()
        stats = TickStats()
        groups: dict[tuple[str, str], list[Subscription]] = {}
        for sub in self.store.due_subscriptions():
            groups.setdefault(sub.group, []).append(sub)
        if only_baselining:
            groups = {k: v for k, v in groups.items() if any(s.status == "baselining" for s in v)}

        pending: dict[int, dict[tuple[str, str], _Pending]] = {}
        failures: dict[str, Exception] = {}
        succeeded: set[str] = set()
        for (source_key, filter_json), subs in groups.items():
            source = self.sources.get(source_key)
            if source is None:
                log.error("unknown source %r in subscriptions %s", source_key, [s.id for s in subs])
                continue
            flt = Filter.from_json(filter_json)
            full = self._needs_full(subs, now)
            stats.groups += 1
            stats.full += full
            try:
                listings = await source.search(flt, max_pages=None if full else 1)
            except Exception as exc:
                log.warning("%s %s: %r", source_key, filter_json, exc)
                failures[source_key] = exc
                stats.failed += 1
                continue
            succeeded.add(source_key)
            self.store.touch_query(source_key, filter_json, now, full=full)
            stats.listings += len(listings)

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

            for sub in subs:
                known = self.store.delivered_keys(sub.id)
                fresh = [(l, s) for l, s in matches if slot_key(l, s) not in known]
                if sub.status == "baselining":
                    if self.store.finish_baseline(sub.id, [(slot_key(l, s), s.end) for l, s in fresh]):
                        n = len({l.id for l, _ in matches})
                        await self._text(
                            sub.chat_id,
                            f"✅ <b>Subscription #{sub.id} is live</b>\n\n"
                            f"🔎 {n} listings match right now — consider these already seen.\n"
                            "🔔 I'll message you as soon as new dates appear.",
                        )
                    continue
                stats.fresh += len(fresh)
                if full:
                    stats.full_crawl_new += len(fresh)
                chat = pending.setdefault(sub.chat_id, {})
                for listing, slot in fresh:
                    chat.setdefault((listing.source, listing.id), _Pending(listing)).add(sub.id, slot)

        for chat_id, items in pending.items():
            stats.sent += await self._deliver(chat_id, list(items.values()))
        await self._health(failures, succeeded)
        log.info(
            "tick%s: groups=%d full=%d listings=%d fresh=%d sent=%d failed=%d full_crawl_new=%d",
            " (baseline)" if only_baselining else "", stats.groups, stats.full, stats.listings,
            stats.fresh, stats.sent, stats.failed, stats.full_crawl_new,
        )
        return stats

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
                    await self.outbox.alert(chat_id, item.listing, item.ordered_slots(), sorted(item.by_sub))
                except ChatGone:
                    raise
                except Exception:
                    log.exception("alert %s to chat %s failed — will retry next tick", item.listing.id, chat_id)
                    continue
                self._mark(item)
                sent += 1
            rest = items[MAX_ALERTS_PER_CHAT:][:MAX_DIGEST]
            if rest:
                try:
                    included = await self.outbox.digest(chat_id, [(i.listing, i.ordered_slots()) for i in rest])
                except ChatGone:
                    raise
                except Exception:
                    log.exception("digest to chat %s failed", chat_id)
                else:
                    for item in rest[:included]:
                        self._mark(item)
                    sent += included
        except ChatGone:
            self.store.pause_chat(chat_id)
            log.info("chat %s is gone — subscriptions paused", chat_id)
        return sent

    def _mark(self, item: _Pending) -> None:
        for sub_id, keys in item.by_sub.items():
            self.store.mark_delivered(sub_id, keys)

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
