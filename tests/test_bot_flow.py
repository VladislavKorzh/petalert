"""The dialog and subscription management through a real Dispatcher.

Telegram is replaced by a session that records Bot API calls, so the whole
wiring is exercised: access gate, router order, FSM, callback data.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator
from datetime import date, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from petalert.bot import build_dispatcher
from petalert.bot.dialog import Pick
from petalert.bot.manage import SubAction
from petalert.db import Store
from petalert.models import Country, Filter

USER = 7
CHAT = Chat(id=USER, type="private")
FROM = User(id=USER, is_bot=False, first_name="Sitter")
_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[Any] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
            return Message(message_id=100, date=datetime.now(), chat=CHAT, text=getattr(method, "text", None))
        return True

    async def stream_content(self, *a, **kw) -> AsyncGenerator[bytes, None]:
        yield b""


class FakeSource:
    key = "ths"
    title = "TrustedHousesitters"

    async def countries(self, continent):
        return [Country("portugal", "Portugal", 30), Country("spain", "Spain", 20)]

    async def search(self, flt, *, max_pages):
        return []


class FakeWorker:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


class Harness:
    def __init__(self, store: Store):
        self.store = store
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.worker = FakeWorker()
        self.dp = build_dispatcher(store, {"ths": FakeSource()}, self.worker, frozenset({USER}))
        self.loop = asyncio.new_event_loop()

    def _feed(self, update: Update) -> list[Any]:
        start = len(self.session.calls)
        self.loop.run_until_complete(self.dp.feed_update(self.bot, update))
        return self.session.calls[start:]

    def text(self, text: str) -> list[Any]:
        msg = Message(message_id=next(_ids), date=datetime.now(), chat=CHAT, from_user=FROM, text=text)
        return self._feed(Update(update_id=next(_ids), message=msg))

    def press(self, data) -> list[Any]:
        bot_msg = Message(message_id=100, date=datetime.now(), chat=CHAT, text="…")
        query = CallbackQuery(
            id=str(next(_ids)), from_user=FROM, chat_instance="ci", message=bot_msg, data=data.pack()
        )
        return self._feed(Update(update_id=next(_ids), callback_query=query))


def texts(calls) -> list[str]:
    return [c.text for c in calls if isinstance(c, (SendMessage, EditMessageText))]


def answers(calls) -> list[str | None]:
    return [c.text for c in calls if isinstance(c, AnswerCallbackQuery)]


@pytest.fixture
def h(tmp_path):
    harness = Harness(Store(tmp_path / "db.sqlite3"))
    yield harness
    harness.loop.close()
    harness.store.close()


def test_full_new_dialog_creates_a_baselining_subscription(h):
    assert "Step 1 · Continent" in texts(h.text("/new"))[0]
    calls = h.press(Pick(step="cont", value="europe"))
    (country_text,) = texts(calls)
    assert "Step 2 · Country in Europe" in country_text

    assert "Step 3 · Dates" in texts(h.press(Pick(step="ctry", value="0")))[0]
    assert "Step 4 · Length" in texts(h.text("2030-11-01 2031-01-31"))[0]
    assert "Step 5 · Pets" in texts(h.press(Pick(step="dur", value="7")))[0]

    toggled = h.press(Pick(step="pet", value="cat"))
    (markup_edit,) = [c for c in toggled if isinstance(c, EditMessageReplyMarkup)]
    assert markup_edit.reply_markup.inline_keyboard[0][1].text == "✅ 🐈 Cat"

    confirm = texts(h.press(Pick(step="pets", value="done")))[0]
    assert "Where: Portugal (Europe)" in confirm and "Pets: 🐈 cat" in confirm

    saved = texts(h.press(Pick(step="save")))[0]
    assert "Subscription #1 saved" in saved
    (sub,) = h.store.subscriptions(USER)
    assert sub.status == "baselining"
    assert sub.filter == Filter(
        "europe", country="portugal", date_from=date(2030, 11, 1), date_to=date(2031, 1, 31),
        min_days=7, pets=("cat",),
    )
    assert h.worker.wakes == 1


def test_all_except_dialog(h):
    h.text("/new")
    h.press(Pick(step="cont", value="europe"))
    assert "except the countries you tap" in texts(h.press(Pick(step="cmode", value="exclude")))[0]

    toggled = h.press(Pick(step="excl", value="0"))  # Portugal
    (edit,) = [c for c in toggled if isinstance(c, EditMessageReplyMarkup)]
    assert edit.reply_markup.inline_keyboard[0][0].text == "🚫 Portugal · 30"

    assert "Step 3 · Dates" in texts(h.press(Pick(step="edone")))[0]
    h.press(Pick(step="dates", value="*"))
    h.press(Pick(step="dur", value="*"))
    confirm = texts(h.press(Pick(step="pets", value="*")))[0]
    assert "Where: Europe, except Portugal" in confirm
    h.press(Pick(step="save"))
    (sub,) = h.store.subscriptions(USER)
    assert sub.filter == Filter("europe", exclude_countries=("portugal",))


def test_commands_win_over_dialog_text_steps(h):
    h.text("/new")
    h.press(Pick(step="cont", value="europe"))
    h.press(Pick(step="ctry", value="*"))  # now the dialog expects dates as text
    assert texts(h.text("/list")) == ["📭 No subscriptions yet. Create one with /new."]


def test_bad_dates_keep_the_dialog_on_the_same_step(h):
    h.text("/new")
    h.press(Pick(step="cont", value="asia"))
    h.press(Pick(step="ctry", value="*"))
    assert "two dates" in texts(h.text("soon"))[0]
    assert "Step 4 · Length" in texts(h.text("2030-11-01 2030-12-01"))[0]


def test_stale_button_after_cancel(h):
    h.text("/new")
    assert texts(h.text("/cancel")) == ["✖️ Cancelled."]
    assert answers(h.press(Pick(step="cont", value="europe"))) == [
        "⌛ This button has expired. Start again with /new."
    ]


def test_list_pause_resume_delete(h):
    sub_id = h.store.add_subscription(USER, "ths", Filter("europe"))
    (listing,) = texts(h.text("/list"))
    assert listing.startswith(f"🔔 <b>Subscription #{sub_id}</b> · TrustedHousesitters\n⏳ indexing")

    h.press(SubAction(action="pause", sub_id=sub_id))
    assert h.store.subscription(sub_id, USER).status == "paused"

    h.press(SubAction(action="resume", sub_id=sub_id))
    assert h.store.subscription(sub_id, USER).status == "baselining"
    assert h.worker.wakes == 1

    h.press(SubAction(action="delete", sub_id=sub_id))
    assert h.store.subscriptions(USER) == []


def test_someone_elses_subscription_cannot_be_touched(h):
    other = h.store.add_subscription(999, "ths", Filter("europe"))
    assert answers(h.press(SubAction(action="delete", sub_id=other))) == ["Subscription not found"]
    assert h.store.subscription(other, 999) is not None
