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

from petalert.bot.browse import unpack
from petalert.bot.notify import TelegramOutbox
from petalert.db import Store
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


def outbox(session: Session, *, pause: float = 0) -> TelegramOutbox:
    return TelegramOutbox(Bot("42:TEST", session=session), {"ths": "THS"}, Store(":memory:"), pause=pause)


def alert(errors) -> list:
    session = Session(errors)
    asyncio.run(outbox(session).alert(1, LISTING, SLOTS, [1]))
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
    sender = outbox(session, pause=10)

    async def two():
        await sender.text(1, "a")
        assert waited == []  # nothing pending after a send: a cancel can't land between send and mark
        await sender.text(1, "b")

    asyncio.run(two())
    assert len(session.sent) == 2 and len(waited) == 1 and 9 < waited[0] <= 10


def test_table_is_stored_and_its_buttons_point_to_it():
    session = Session({})
    sender = outbox(session)
    other = Listing(**{**LISTING.__dict__, "id": "2", "url": "https://x/l/2/"})
    asyncio.run(sender.listings(5, [(LISTING, SLOTS, [1]), (other, SLOTS, [1])]))
    (sent,) = session.sent
    assert isinstance(sent, SendMessage) and "<pre>" in sent.text and sent.link_preview_options.is_disabled
    numbers = sent.reply_markup.inline_keyboard[0]
    assert [(b.text, b.url) for b in numbers] == [("1(T)", LISTING.url), ("2(T)", "https://x/l/2/")]
    stored, header = unpack(sender.store.alert_list(1, 5))  # the first list of a fresh database
    assert [i.listing for i in stored] == [LISTING, other] and stored[0].slots == SLOTS
    assert header is None  # an alert table: the header is the default one


def test_table_stays_stored_when_the_send_fails():
    # A timeout can come after Telegram delivered the message: its buttons must work.
    session = Session({SendMessage: [lambda m: TelegramBadRequest(m, "Bad Request: message is too long")]})
    sender = outbox(session)
    with pytest.raises(TelegramBadRequest):  # the worker retries next tick
        asyncio.run(sender.listings(5, [(LISTING, SLOTS, [1]), (LISTING, SLOTS, [1])]))
    assert sender.store.conn.execute("SELECT COUNT(*) FROM alert_list").fetchone()[0] == 1


def test_text_fallback_is_not_cut_to_the_caption_limit():
    many = [Slot(str(i), date(2030, 1, 1), date(2030, 1, 10), True) for i in range(20)]
    session = Session({SendPhoto: [lambda m: TelegramBadRequest(m, "Bad Request: wrong file identifier")]})
    asyncio.run(outbox(session).alert(1, LISTING, many, [1]))
    (sent,) = session.sent
    assert isinstance(sent, SendMessage) and "more date" not in sent.text
