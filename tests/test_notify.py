from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date, datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import Chat, Message

from petalert.bot.notify import TelegramOutbox
from petalert.models import Listing, Slot
from petalert.scheduler import ChatGone

LISTING = Listing(
    source="ths", id="1", title="Flat", url="https://x/l/1/", continent="europe", country="pt",
    place="Lisbon", photo_url="https://img/1.jpg", pets=(("cat", 1),),
)
SLOTS = [Slot("s", date(2030, 1, 1), date(2030, 1, 10), True)]


class Session(BaseSession):
    def __init__(self, errors):
        super().__init__()
        self.errors = errors  # {method type: [exceptions in order]}
        self.sent: list = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        queue = self.errors.get(type(method), [])
        if queue:
            raise queue.pop(0)(method)
        self.sent.append(method)
        return Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"))

    async def stream_content(self, *a, **kw) -> AsyncGenerator[bytes, None]:
        yield b""


def alert(errors) -> list:
    session = Session(errors)
    outbox = TelegramOutbox(Bot("42:TEST", session=session), {"ths": "THS"}, pause=0)
    asyncio.run(outbox.alert(1, LISTING, SLOTS, [1]))
    return session.sent


def test_photo_alert():
    (sent,) = alert({})
    assert isinstance(sent, SendPhoto) and sent.photo == LISTING.photo_url


def test_bad_photo_falls_back_to_text():
    bad = lambda m: TelegramBadRequest(m, "Bad Request: wrong file identifier/HTTP URL specified")
    (sent,) = alert({SendPhoto: [bad]})
    assert isinstance(sent, SendMessage) and "Flat" in sent.text


def test_blocked_bot_and_missing_chat_are_chat_gone():
    with pytest.raises(ChatGone):
        alert({SendPhoto: [lambda m: TelegramForbiddenError(m, "Forbidden: bot was blocked by the user")]})
    gone = lambda m: TelegramBadRequest(m, "Bad Request: chat not found")
    with pytest.raises(ChatGone):
        alert({SendPhoto: [gone]})


def test_flood_limit_waits_and_retries_once(monkeypatch):
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("petalert.bot.notify.asyncio.sleep", fake_sleep)
    flood = lambda m: TelegramRetryAfter(m, "Too Many Requests", retry_after=3)
    (sent,) = alert({SendPhoto: [flood]})
    assert isinstance(sent, SendPhoto) and 3 in waited


def test_throttle_waits_before_sending_not_after(monkeypatch):
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("petalert.bot.notify.asyncio.sleep", fake_sleep)
    session = Session({})
    outbox = TelegramOutbox(Bot("42:TEST", session=session), {"ths": "THS"}, pause=10)

    async def two():
        await outbox.text(1, "a")
        assert waited == []  # nothing pending after a send: a cancel can't land between send and mark
        await outbox.text(1, "b")

    asyncio.run(two())
    assert len(session.sent) == 2 and len(waited) == 1 and 9 < waited[0] <= 10
