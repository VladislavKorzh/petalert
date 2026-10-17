"""Воркер: раз на ~30 хв опитати джерела й розіслати нові проміжки дат.

Ключові рішення:

* **Групування.** Однакові (source, filter) у різних підписках — один HTTP-запит.
* **Сторінка 1 чи повний обхід.** Зазвичай вистачає 100 найновіших (по світу
  THS публікує ~50 оголошень на годину). Повний обхід — коли в групі є нова
  підписка (baseline), раз на добу і після простою понад 2 год. Watermark по
  `published` свідомо не використовується: це naive London time, і на DST він
  ламається.
* **Baseline.** Нова (чи відновлена після паузи) підписка спершу мовчки
  позначає все, що підходить зараз, і лише потім стає `active`. Інакше перший
  тік — це сотні алертів про старі оголошення. THS віддає лише 1000 найновіших
  результатів, тож для широкого фільтра baseline покриває саме їх; старіше
  оголошення дасть алерт, лише коли його перепублікують і воно підніметься
  нагору стрічки, — а це й так подія, про яку варто знати.
* **Позначати після відправки.** Невдала відправка не губиться: наступний тік
  знайде той самий проміжок знову.
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
MAX_ALERTS_PER_CHAT = 10  # фото-алертів на чат за тік; решта — одним дайджестом
MAX_DIGEST = 30  # рядків у дайджесті; те, що не влізло, піде наступним тіком
FAIL_STREAK_ALERT = 3  # ~1.5 год поспіль без відповіді джерела — кажемо адміну


class ChatGone(Exception):
    """Користувач заблокував бота або чат видалено — слати більше нікуди."""


class Outbox(Protocol):
    async def alert(self, chat_id: int, listing: Listing, slots: list[Slot], sub_ids: list[int]) -> None: ...
    async def digest(self, chat_id: int, items: list[tuple[Listing, list[Slot]]]) -> int:
        """Повертає, скільки перших пунктів реально влізло в повідомлення."""
        ...
    async def text(self, chat_id: int, text: str) -> None: ...


Admin = Callable[[str], Awaitable[None]]


@dataclass
class _Pending:
    """Одне оголошення для одного чату: нові проміжки з усіх його підписок."""

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
    full_crawl_new: int = 0  # нове, знайдене лише повним обходом (див. ризики в README)


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
        """Нова підписка чи resume — baseline одразу, а не через пів години."""
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
                    # Лише за сигналом, а не «поки є baselining»: якщо джерело
                    # лежить, це був би гарячий цикл запитів.
                    await self.tick(only_baselining=True)
                if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY:
                    delivered, states = self.store.prune(now.date())
                    self._last_prune = now
                    log.info("prune: delivered=%d query_state=%d", delivered, states)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("тік упав")
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
            log.exception("не вдалося написати адміну: %s", text)

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
                log.error("невідоме джерело %r у підписках %s", source_key, [s.id for s in subs])
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
                    continue  # очікувано: виключення API не передаються
                slots = matching_slots(listing, flt, today) if listing_matches(listing, flt) else []
                dropped += not slots
                matches.extend((listing, slot) for slot in slots)
            if dropped:
                # API мав би повернути лише те, що підходить; масові розбіжності —
                # ознака, що він почав ігнорувати якийсь фільтр.
                log.warning("%s %s: локальний фільтр відкинув %d з %d", source_key, filter_json, dropped, len(listings))

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
            log.info("чат %s недоступний — підписки на паузі", chat_id)
        except Exception:
            log.exception("не вдалося написати в чат %s", chat_id)

    async def _deliver(self, chat_id: int, items: list[_Pending]) -> int:
        sent = 0
        try:
            for item in items[:MAX_ALERTS_PER_CHAT]:
                try:
                    await self.outbox.alert(chat_id, item.listing, item.ordered_slots(), sorted(item.by_sub))
                except ChatGone:
                    raise
                except Exception:
                    log.exception("алерт %s у чат %s не пішов — повториться наступним тіком", item.listing.id, chat_id)
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
                    log.exception("дайджест у чат %s не пішов", chat_id)
                else:
                    for item in rest[:included]:
                        self._mark(item)
                    sent += included
        except ChatGone:
            self.store.pause_chat(chat_id)
            log.info("чат %s недоступний — підписки на паузі", chat_id)
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
