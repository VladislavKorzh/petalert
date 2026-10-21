"""Sending to Telegram for the worker: throttling and mapping Telegram errors.

One message per second for the whole process — comfortably within both the
1/s per-chat and the 30/s global limits; at our volume (dozens of alerts per
tick) it costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from ..db import Store
from ..models import Listing, Slot
from ..scheduler import ChatGone
from .browse import Item, list_message, pack
from .format import TEXT_LIMIT, render_alert
from .ui import NO_PREVIEW

log = logging.getLogger(__name__)


class TelegramOutbox:
    def __init__(self, bot: Bot, source_titles: dict[str, str], store: Store, *, pause: float = 1.0):
        self.bot = bot
        self.source_titles = source_titles
        self.store = store
        self.pause = pause
        self._next_send = 0.0

    async def _call(self, method: Callable[..., Awaitable[Any]], **kwargs: Any) -> None:
        for attempt in range(2):
            # Wait BEFORE sending, not after: a shutdown that cancels this sleep
            # then never lands between "sent" and "marked as delivered", which
            # would resend the alert after the restart.
            wait = self._next_send - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await method(**kwargs)
                return
            except TelegramRetryAfter as exc:
                if attempt:
                    raise
                log.warning("Telegram flood limit, waiting %s s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except TelegramForbiddenError as exc:
                raise ChatGone(str(exc)) from exc
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    raise ChatGone(str(exc)) from exc
                raise
            finally:
                self._next_send = time.monotonic() + self.pause

    async def alert(self, chat_id: int, listing: Listing, slots: list[Slot], sub_ids: list[int]) -> None:
        title = self.source_titles.get(listing.source, listing.source)
        if listing.photo_url:
            text, markup = render_alert(listing, slots, sub_ids, title)
            try:
                await self._call(
                    self.bot.send_photo, chat_id=chat_id, photo=listing.photo_url,
                    caption=text, reply_markup=markup,
                )
                return
            except TelegramBadRequest as exc:
                # Telegram couldn't fetch the photo by URL — the text matters more.
                log.info("photo %s failed (%s), sending as text", listing.photo_url, exc)
        text, markup = render_alert(listing, slots, sub_ids, title, limit=TEXT_LIMIT)
        await self._call(
            self.bot.send_message, chat_id=chat_id, text=text, reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )

    async def listings(self, chat_id: int, items: list[tuple[Listing, list[Slot], list[int]]]) -> None:
        shown = [Item(*item) for item in items]
        # Stored first: its id goes into the buttons. Kept even if the send
        # fails — a timeout can come after Telegram delivered the message, and
        # its buttons must work; an orphan row is pruned with the rest.
        list_id = self.store.add_alert_list(chat_id, pack(shown))
        text, markup = list_message(shown, list_id, sites=self.source_titles)
        await self._call(
            self.bot.send_message, chat_id=chat_id, text=text, reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )

    async def text(self, chat_id: int, text: str) -> None:
        await self._call(self.bot.send_message, chat_id=chat_id, text=text)
