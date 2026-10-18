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
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, Update, User

from petalert.bot import build_dispatcher
from petalert.bot.browse import Browse, Item, pack
from petalert.bot.dialog import Pick
from petalert.bot.manage import SiteToggle, SubAction
from petalert.db import Store
from petalert.models import Country, Filter, Listing, Slot

USER = 7
CHAT = Chat(id=USER, type="private")
FROM = User(id=USER, is_bot=False, first_name="Sitter")
_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[Any] = []
        self.on_answer = None  # hook: runs while a callback answer is "in flight"
        self.answers_expired = False

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            if self.on_answer:
                hook, self.on_answer = self.on_answer, None
                await hook()
            if self.answers_expired:
                raise TelegramBadRequest(method, "Bad Request: query is too old and response timeout expired or query ID is invalid")
        if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
            return Message(message_id=100, date=datetime.now(), chat=CHAT, text=getattr(method, "text", None))
        return True

    async def stream_content(self, *a, **kw) -> AsyncGenerator[bytes, None]:
        yield b""


class FakeSource:
    key = "ths"
    title = "TrustedHousesitters"
    during_countries = None  # hook: runs while the "network" call is in flight

    async def countries(self, continent):
        if self.during_countries:
            await self.during_countries()
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
        self.source = FakeSource()
        self.dp = build_dispatcher(store, {"ths": self.source}, self.worker, frozenset({USER}))
        self.loop = asyncio.new_event_loop()

    def _feed(self, update: Update) -> list[Any]:
        start = len(self.session.calls)
        self.loop.run_until_complete(self.dp.feed_update(self.bot, update))
        return self.session.calls[start:]

    def text(self, text: str) -> list[Any]:
        msg = Message(message_id=next(_ids), date=datetime.now(), chat=CHAT, from_user=FROM, text=text)
        return self._feed(Update(update_id=next(_ids), message=msg))

    def press(self, data, *, message_id: int = 100, inaccessible: bool = False) -> list[Any]:
        if inaccessible:  # what Telegram sends for buttons on messages older than 48 h
            bot_msg = InaccessibleMessage(chat=CHAT, message_id=message_id, date=0)
        else:
            bot_msg = Message(message_id=message_id, date=datetime.now(), chat=CHAT, text="…")
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


def test_full_new_dialog_creates_an_unbaselined_subscription(h):
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
    assert sub.status == "active" and h.store.watches(USER) == {}  # indexed on the next tick
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
    sub_id = h.store.add_subscription(USER, Filter("europe"))
    (listing,) = texts(h.text("/list"))
    assert listing.startswith(f"🔔 <b>Subscription #{sub_id}</b>\n⏳ indexing TrustedHousesitters\n")
    assert listing.endswith("🌐 Sites: TrustedHousesitters")
    h.store.finish_baseline(h.store.subscription(sub_id, USER), "ths", [])
    (listing,) = texts(h.text("/list"))
    assert "\n🟢 active\n" in listing

    h.press(SubAction(action="pause", sub_id=sub_id))
    assert h.store.subscription(sub_id, USER).status == "paused"

    h.press(SubAction(action="resume", sub_id=sub_id))
    assert h.store.subscription(sub_id, USER).status == "active"
    assert h.store.watches(USER) == {}  # indexed again: nothing from the pause is sent
    assert h.worker.wakes == 1

    h.press(SubAction(action="delete", sub_id=sub_id))
    assert h.store.subscriptions(USER) == []


def test_someone_elses_subscription_cannot_be_touched(h):
    other = h.store.add_subscription(999, Filter("europe"))
    assert answers(h.press(SubAction(action="delete", sub_id=other))) == ["Subscription not found"]
    assert h.store.subscription(other, 999) is not None


STALE = "⌛ This button has expired. Start again with /new."


def test_buttons_of_an_older_dialog_message_are_stale(h):
    h.text("/new")
    assert answers(h.press(Pick(step="cont", value="europe"), message_id=99)) == [STALE]


def test_double_tap_on_save_creates_one_subscription(h):
    h.text("/new")
    h.press(Pick(step="cont", value="asia"))
    h.press(Pick(step="ctry", value="*"))
    h.press(Pick(step="dates", value="*"))
    h.press(Pick(step="dur", value="*"))
    h.press(Pick(step="pets", value="*"))
    h.press(Pick(step="save"))
    assert answers(h.press(Pick(step="save"))) == [STALE]
    assert len(h.store.subscriptions(USER)) == 1


def test_cancel_while_countries_load_is_respected(h):
    h.text("/new")

    async def cancel_meanwhile():
        h.source.during_countries = None
        msg = Message(message_id=7, date=datetime.now(), chat=CHAT, from_user=FROM, text="/cancel")
        await h.dp.feed_update(h.bot, Update(update_id=999, message=msg))

    h.source.during_countries = cancel_meanwhile
    calls = h.press(Pick(step="cont", value="europe"))
    assert not any("Step 2" in t for t in texts(calls))


def test_forged_page_value_is_rejected_not_crashed(h):
    h.text("/new")
    h.press(Pick(step="cont", value="europe"))
    assert answers(h.press(Pick(step="cpage", value="abc"))) == ["Unknown page"]
    assert answers(h.press(Pick(step="dur", value="5"))) == [STALE]  # not at that step yet


def test_resume_on_a_running_subscription_does_not_rebaseline(h):
    sub_id = h.store.add_subscription(USER, Filter("europe"))
    h.store.finish_baseline(h.store.subscription(sub_id, USER), "ths", [])
    assert answers(h.press(SubAction(action="resume", sub_id=sub_id))) == ["Already running"]
    assert h.store.subscription(sub_id, USER).status == "active"
    assert set(h.store.watches(USER)) == {(sub_id, "ths")}  # its baseline survives
    assert h.worker.wakes == 0


def test_buttons_on_a_message_older_than_48h_still_work(h):
    sub_id = h.store.add_subscription(USER, Filter("europe"))
    calls = h.press(SubAction(action="pause", sub_id=sub_id), inaccessible=True)
    assert h.store.subscription(sub_id, USER).status == "paused"
    assert any(isinstance(c, SendMessage) for c in calls)  # a fresh message instead of an edit


def test_cancel_while_the_continent_tap_is_being_answered(h):
    h.text("/new")

    async def cancel():
        msg = Message(message_id=8, date=datetime.now(), chat=CHAT, from_user=FROM, text="/cancel")
        await h.dp.feed_update(h.bot, Update(update_id=998, message=msg))

    h.session.on_answer = cancel
    h.press(Pick(step="cont", value="europe"))  # must not raise KeyError
    assert h.store.subscriptions(USER) == []


def test_expired_callback_answer_does_not_abort_the_action(h):
    sub_id = h.store.add_subscription(USER, Filter("europe"))
    h.session.answers_expired = True  # pressed while the bot was down
    calls = h.press(SubAction(action="pause", sub_id=sub_id))
    assert h.store.subscription(sub_id, USER).status == "paused"
    assert any(isinstance(c, EditMessageText) and "paused" in c.text for c in calls)


def table(h, count: int, chat: int = USER) -> int:
    items = [
        Item(
            Listing(
                source="ths", id=str(i), title=f"Sit {i}", url=f"https://x/l/{i}/", continent="europe",
                country="portugal", place=f"Town{i}, Faro, Portugal",
                photo_url=f"https://img/{i}.jpg" if i % 2 else None, pets=(("cat", 1),), country_code="pt",
            ),
            [Slot(f"s{i}", date(2030, 1, 1 + i % 28), date(2030, 2, 1), True, applicants=0)],
            [1],
        )
        for i in range(count)
    ]
    return h.store.add_alert_list(chat, pack(items))


def edits(calls) -> list[EditMessageText]:
    return [c for c in calls if isinstance(c, EditMessageText)]


def test_table_number_opens_the_card_in_place_with_the_photo(h):
    list_id = table(h, 3)
    (card,) = edits(h.press(Browse(list_id=list_id, view="card", n=1)))
    assert card.text.startswith("🏡 <b>Sit 1</b>")
    assert card.link_preview_options.url == "https://img/1.jpg"
    assert card.link_preview_options.show_above_text and card.link_preview_options.prefer_large_media
    buttons = [[b.text for b in row] for row in card.reply_markup.inline_keyboard]
    assert buttons == [["◀", "2/3", "▶"], ["📋 Back to the list"], ["🔗 Open on TrustedHousesitters"]]
    assert card.reply_markup.inline_keyboard[2][0].url == "https://x/l/1/"

    # ◀ ▶ wrap around; a listing without a photo gets no preview at all
    (prev,) = edits(h.press(Browse.unpack(card.reply_markup.inline_keyboard[0][0].callback_data)))
    assert prev.text.startswith("🏡 <b>Sit 0</b>") and prev.link_preview_options.is_disabled
    (wrapped,) = edits(h.press(Browse(list_id=list_id, view="card", n=2)))
    nxt = Browse.unpack(wrapped.reply_markup.inline_keyboard[0][2].callback_data)
    assert nxt.n == 0

    (back,) = edits(h.press(Browse.unpack(card.reply_markup.inline_keyboard[1][0].callback_data)))
    assert "<pre>" in back.text and back.link_preview_options.is_disabled


def test_table_pages(h):
    list_id = table(h, 13)
    (page2,) = edits(h.press(Browse(list_id=list_id, view="page", n=1)))
    rows = page2.reply_markup.inline_keyboard
    assert [b.text for b in rows[0]] == ["11", "12", "13"]
    assert [b.text for b in rows[-1]] == ["◀", "2/2", "▶"]
    # Back from card 12 lands on page 2
    (card,) = edits(h.press(Browse.unpack(rows[0][1].callback_data)))
    assert Browse.unpack(card.reply_markup.inline_keyboard[1][0].callback_data).n == 1
    # the page counter button does nothing
    calls = h.press(Browse(list_id=list_id, view="noop"))
    assert edits(calls) == [] and answers(calls) == [None]


def test_table_buttons_are_checked_against_the_chat_and_the_list(h):
    foreign = table(h, 2, chat=999)
    assert answers(h.press(Browse(list_id=foreign, view="card", n=0))) == ["⌛ This list has expired."]
    mine = table(h, 2)
    assert answers(h.press(Browse(list_id=mine, view="card", n=2))) == ["Unknown button"]
    assert answers(h.press(Browse(list_id=mine, view="frame", n=0))) == ["Unknown button"]


class RawData:
    """Callback data exactly as a (forged) client sends it."""

    def __init__(self, data: str):
        self.data = data

    def pack(self) -> str:
        return self.data


@pytest.mark.parametrize(
    "data", ["b:" + "9" * 40 + ":card:0", "b:1:page:-1", "b:0:card:0", "b:x:card:0", "s:pause:" + "9" * 40]
)
def test_forged_out_of_range_numbers_are_stale_buttons_not_crashes(h, data):
    table(h, 2)
    assert answers(h.press(RawData(data))) == [STALE]


def test_table_button_on_an_inaccessible_message_sends_the_card_anew(h):
    list_id = table(h, 2)
    calls = h.press(Browse(list_id=list_id, view="card", n=1), inaccessible=True)
    (sent,) = [c for c in calls if isinstance(c, SendMessage)]
    assert sent.text.startswith("🏡 <b>Sit 1</b>") and sent.link_preview_options.url == "https://img/1.jpg"


class OtherSource(FakeSource):
    key = "mmh"
    title = "MindMyHouse"

    async def countries(self, continent):
        return [Country("france", "France", 27), Country("spain", "Spain", 5)]


@pytest.fixture
def two(tmp_path):
    h = Harness(Store(tmp_path / "two.sqlite3"))
    h.other = OtherSource()
    h.dp = build_dispatcher(h.store, {"ths": h.source, "mmh": h.other}, h.worker, frozenset({USER}))
    yield h
    h.loop.close()
    h.store.close()


def test_with_two_sites_one_subscription_covers_both_and_countries_merge(two):
    assert "Step 1 · Continent" in texts(two.text("/new"))[0]
    (country_step,) = [c for c in two.press(Pick(step="cont", value="europe")) if isinstance(c, EditMessageText)]
    buttons = [b.text for row in country_step.reply_markup.inline_keyboard for b in row]
    countries = [b for b in buttons if " · " in b]
    assert countries == ["Portugal · 30", "France · 27", "Spain · 25"]  # Spain: 20 + 5
    two.press(Pick(step="ctry", value="2"))
    two.press(Pick(step="dates", value="*"))
    two.press(Pick(step="dur", value="*"))
    confirm = texts(two.press(Pick(step="pets", value="*")))[0]
    assert "🌐 Sites: TrustedHousesitters, MindMyHouse (change in /sites)" in confirm
    two.press(Pick(step="save"))
    (sub,) = two.store.subscriptions(USER)
    assert sub.filter == Filter("europe", country="spain")


def test_countries_still_load_when_one_site_fails(two):
    async def broken(continent):
        raise RuntimeError("down")

    two.other.countries = broken
    two.text("/new")
    (country_step,) = [c for c in two.press(Pick(step="cont", value="europe")) if isinstance(c, EditMessageText)]
    assert "Portugal · 30" in str(country_step.reply_markup)


def test_sites_toggle(two):
    (msg,) = texts(two.text("/sites"))
    assert msg.startswith("🌐 <b>Sites</b>")
    (keyboard,) = [c.reply_markup for c in two.text("/sites") if isinstance(c, SendMessage)]
    off = SiteToggle.unpack(keyboard.inline_keyboard[1][0].callback_data)
    assert (off.source, off.on) == ("mmh", False)
    calls = two.press(off)
    assert answers(calls) == ["MindMyHouse off"]
    (edited,) = edits(calls)
    assert [row[0].text for row in edited.reply_markup.inline_keyboard] == [
        "✅ TrustedHousesitters", "⬜ MindMyHouse"
    ]
    assert two.store.enabled_sources(USER, ["ths", "mmh"]) == ["ths"]
    assert two.worker.wakes == 0

    # A double tap (or an old /sites message) asks for the state it already has.
    assert answers(two.press(off)) == ["MindMyHouse is already off"]
    assert two.store.enabled_sources(USER, ["ths", "mmh"]) == ["ths"]
    assert answers(two.press(SiteToggle(source="mmh", on=True))) == ["MindMyHouse on — indexing current listings"]
    assert two.worker.wakes == 1  # indexed now, not in half an hour
    assert answers(two.press(SiteToggle(source="nope", on=True))) == ["Unknown site"]


def test_sites_does_not_end_an_open_new_dialog(two):
    two.text("/new")
    two.press(Pick(step="cont", value="asia"))
    two.press(Pick(step="ctry", value="*"))
    two.press(Pick(step="dates", value="*"))
    two.press(Pick(step="dur", value="*"))
    two.press(Pick(step="pets", value="*"))
    two.text("/sites")
    two.press(SiteToggle(source="mmh", on=False))
    assert "saved" in texts(two.press(Pick(step="save")))[0]
    assert len(two.store.subscriptions(USER)) == 1


def test_list_shows_each_sites_state(two):
    sub = two.store.subscription(two.store.add_subscription(USER, Filter("europe")), USER)
    two.store.finish_baseline(sub, "ths", [])
    (text,) = texts(two.text("/list"))
    assert "\n⏳ indexing MindMyHouse\n" in text and text.endswith("🌐 Sites: TrustedHousesitters, MindMyHouse")
    two.store.set_source(USER, "mmh", False)
    (text,) = texts(two.text("/list"))
    assert "\n🟢 active\n" in text and text.endswith("🌐 Sites: TrustedHousesitters")
    two.store.set_source(USER, "ths", False)
    (text,) = texts(two.text("/list"))
    assert "💤 all sites are off" in text


def test_a_site_turned_off_is_left_out_of_new(two):
    two.store.set_source(USER, "mmh", False)
    two.text("/new")
    (country_step,) = [c for c in two.press(Pick(step="cont", value="europe")) if isinstance(c, EditMessageText)]
    assert "France" not in str(country_step.reply_markup)  # only the other site has France


def test_every_site_off_in_the_middle_of_new(two):
    two.text("/new")
    two.store.set_source(USER, "ths", False)
    two.store.set_source(USER, "mmh", False)
    (edited,) = edits(two.press(Pick(step="cont", value="europe")))
    assert edited.text == "💤 All sites are off for you — turn one on in /sites first."


def test_new_with_every_site_off_points_to_sites(two):
    two.store.set_source(USER, "ths", False)
    two.store.set_source(USER, "mmh", False)
    assert texts(two.text("/new")) == ["💤 All sites are off for you — turn one on in /sites first."]
