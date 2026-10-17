"""Діалог /new: джерело → континент → країна → дати → тривалість → тварини → Save.

Крок країни має два режими: одна країна (або будь-яка) чи «увесь континент,
крім позначених» — той самий список із пагінацією, але кнопки перемикають
виключення, а «Done» веде далі.

Одне повідомлення бота редагується від кроку до кроку; нове з'являється лише
після текстового вводу (дати, тривалість). Стан — у FSM (MemoryStorage):
рестарт посеред діалогу його губить, і це прийнятно — /new займає хвилину.

Країну в кнопці передаємо індексом у списку з FSM, а не slug: callback data
обмежена 64 байтами, а slug на кшталт `bonaire-saint-eustatius-and-saba` разом
із префіксом підходить до межі впритул.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Store
from ..models import CONTINENTS, PETS, Filter
from ..scheduler import Worker
from .format import PET_EMOJI, describe_filter

log = logging.getLogger(__name__)

COUNTRIES_PER_PAGE = 10
DURATION_PRESETS = (3, 7, 14, 30)
MAX_DAYS = 365
ANY = "*"
CONTINENT_EMOJI = {
    "europe": "🇪🇺", "asia": "🌏", "africa": "🌍",
    "oceania": "🏝", "north-america": "🌎", "south-america": "🌎",
}


class NewSub(StatesGroup):
    source = State()
    continent = State()
    country = State()
    exclude = State()
    dates = State()
    duration = State()
    pets = State()
    confirm = State()


class Pick(CallbackData, prefix="n"):
    step: str
    value: str = ""


# --- розбір текстового вводу (чисті функції) ---------------------------------

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{4}")


def _parse_date(token: str) -> date:
    if "-" in token:
        y, m, d = token.split("-")
    else:
        d, m, y = re.split(r"[./]", token)
    return date(int(y), int(m), int(d))


def parse_date_range(text: str, today: date) -> tuple[date, date]:
    """`2026-11-01 2027-01-31` або `01.11.2026 - 31.01.2027`.

    ValueError несе текст для користувача. Минулий початок підтягується до
    сьогодні — сайт так само не показує вже розпочаті сити.
    """
    tokens = _DATE_RE.findall(text)
    if len(tokens) != 2:
        raise ValueError(
            "Send two dates, e.g. 2026-11-01 2027-01-31 or 01.11.2026 - 31.01.2027."
        )
    try:
        start, end = (_parse_date(t) for t in tokens)
    except ValueError:
        raise ValueError("One of the dates doesn't exist. Check day and month.") from None
    if end <= start:
        raise ValueError("The end date must be after the start date.")
    if end <= today:
        raise ValueError("This range is already in the past.")
    return max(start, today), end


_DURATION_RE = re.compile(r"^\s*(\d{1,3})\s*(?:\+|(?:-|–|—|to)\s*(\d{1,3}))?\s*$")


def parse_duration(text: str) -> tuple[int, int | None]:
    """`7` / `7+` — від 7 днів; `7-21` — від 7 до 21."""
    match = _DURATION_RE.match(text)
    if not match:
        raise ValueError("Send a number of days like 7, or a range like 7-21.")
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else None
    if not 1 <= low <= MAX_DAYS or (high is not None and not low <= high <= MAX_DAYS):
        raise ValueError(f"Days must be between 1 and {MAX_DAYS}, min ≤ max.")
    return low, high


# --- клавіатури --------------------------------------------------------------


def _cancel(kb: InlineKeyboardBuilder) -> None:
    kb.button(text="✖ Cancel", callback_data=Pick(step="cancel"))


def sources_keyboard(sources) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, source in sources.items():
        kb.button(text=source.title, callback_data=Pick(step="src", value=key))
    _cancel(kb)
    kb.adjust(1)
    return kb.as_markup()


def continents_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for slug, name in CONTINENTS.items():
        kb.button(text=f"{CONTINENT_EMOJI[slug]} {name}", callback_data=Pick(step="cont", value=slug))
    _cancel(kb)
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def countries_keyboard(
    countries: list[list], page: int, excluded: list[str] | None = None
) -> InlineKeyboardMarkup:
    """Вибір однієї країни; з `excluded` — перемикання виключень."""
    excluding = excluded is not None
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    if not excluding:
        kb.button(text="🌍 Any country", callback_data=Pick(step="ctry", value=ANY))
        kb.button(text="🚫 All except…", callback_data=Pick(step="cmode", value="exclude"))
        rows += [1, 1]
    start = page * COUNTRIES_PER_PAGE
    chunk = countries[start : start + COUNTRIES_PER_PAGE]
    for i, (slug, name, count) in enumerate(chunk, start=start):
        if excluding:
            mark = "🚫 " if slug in excluded else ""
            kb.button(text=f"{mark}{name} · {count}", callback_data=Pick(step="excl", value=str(i)))
        else:
            kb.button(text=f"{name} · {count}", callback_data=Pick(step="ctry", value=str(i)))
    rows += [2] * (len(chunk) // 2) + [1] * (len(chunk) % 2)
    page_step = "epage" if excluding else "cpage"
    nav = 0
    if page > 0:
        kb.button(text="◀ Prev", callback_data=Pick(step=page_step, value=str(page - 1)))
        nav += 1
    if start + COUNTRIES_PER_PAGE < len(countries):
        kb.button(text="Next ▶", callback_data=Pick(step=page_step, value=str(page + 1)))
        nav += 1
    if nav:
        rows.append(nav)
    if excluding:
        kb.button(text=f"Done ✔ ({len(excluded)} excluded)", callback_data=Pick(step="edone"))
        rows.append(1)
    _cancel(kb)
    kb.adjust(*rows, 1)
    return kb.as_markup()


def dates_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Any dates", callback_data=Pick(step="dates", value=ANY))
    _cancel(kb)
    kb.adjust(1)
    return kb.as_markup()


def duration_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="♾ Any length", callback_data=Pick(step="dur", value=ANY))
    for days in DURATION_PRESETS:
        kb.button(text=f"{days}+ days", callback_data=Pick(step="dur", value=str(days)))
    _cancel(kb)
    kb.adjust(1, len(DURATION_PRESETS), 1)
    return kb.as_markup()


def pets_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pet in PETS:
        mark = "✅ " if pet in selected else ""
        kb.button(text=f"{mark}{PET_EMOJI[pet]} {pet.capitalize()}", callback_data=Pick(step="pet", value=pet))
    kb.button(text="🐾 Any pet", callback_data=Pick(step="pets", value=ANY))
    kb.button(text="Done ✔", callback_data=Pick(step="pets", value="done"))
    _cancel(kb)
    kb.adjust(3, 3, 3, 2, 1)
    return kb.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💾 Save", callback_data=Pick(step="save"))
    _cancel(kb)
    kb.adjust(2)
    return kb.as_markup()


# --- кроки -------------------------------------------------------------------


def _filter(data: dict) -> Filter:
    return Filter(
        continent=data["continent"],
        country=data.get("country"),
        date_from=date.fromisoformat(data["date_from"]) if data.get("date_from") else None,
        date_to=date.fromisoformat(data["date_to"]) if data.get("date_to") else None,
        min_days=data.get("min_days"),
        max_days=data.get("max_days"),
        pets=tuple(data.get("pets") or ()),
        exclude_countries=tuple(data.get("exclude") or ()),
    )


def _country_at(data: dict, value: str) -> str | None:
    """Slug за індексом із кнопки; None — якщо індекс не з цього списку."""
    countries = data.get("countries") or []
    if not value.isdigit() or int(value) >= len(countries):
        return None
    return countries[int(value)][0]


async def _show(target: Message, text: str, markup: InlineKeyboardMarkup, *, edit: bool) -> None:
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def ask_continent(target: Message, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(NewSub.continent)
    await _show(target, "🌍 <b>Step 1 · Continent</b>\nWhere do you want to sit?", continents_keyboard(), edit=edit)


async def ask_country(target: Message, state: FSMContext, page: int) -> None:
    await state.set_state(NewSub.country)
    data = await state.get_data()
    countries = data["countries"]
    text = (
        f"🗺 <b>Step 2 · Country in {CONTINENTS[data['continent']]}</b>\n"
        "Pick one, any, or 🚫 exclude some.\n"
        "<i>The number = listings with open dates right now.</i>"
    )
    if not countries:
        text = (
            f"🗺 <b>Step 2 · Country</b>\nNo open sits in {CONTINENTS[data['continent']]} "
            "right now — you can still subscribe."
        )
    await _show(target, text, countries_keyboard(countries, page), edit=True)


async def ask_exclude(target: Message, state: FSMContext, page: int) -> None:
    await state.set_state(NewSub.exclude)
    data = await state.get_data()
    text = (
        f"🚫 <b>Step 2 · Exclude countries</b>\n"
        f"All of {CONTINENTS[data['continent']]} except the countries you tap.\n"
        "Tap again to undo, then <b>Done</b>."
    )
    excluded = data.get("exclude") or []
    await _show(target, text, countries_keyboard(data["countries"], page, excluded), edit=True)


async def ask_dates(target: Message, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(NewSub.dates)
    text = (
        "📅 <b>Step 3 · Dates</b>\n"
        "Send a date range — the whole sit must fit inside it:\n"
        "• <code>2026-11-01 2027-01-31</code>\n"
        "• <code>01.11.2026 - 31.01.2027</code>\n"
        "or tap <b>Any dates</b>."
    )
    await _show(target, text, dates_keyboard(), edit=edit)


async def ask_duration(target: Message, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(NewSub.duration)
    text = (
        "⏳ <b>Step 4 · Length</b>\n"
        "Tap a preset, or send <code>7</code> (minimum) or <code>7-21</code> (min–max days)."
    )
    await _show(target, text, duration_keyboard(), edit=edit)


async def ask_pets(target: Message, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(NewSub.pets)
    selected = (await state.get_data()).get("pets") or []
    text = (
        "🐾 <b>Step 5 · Pets</b>\n"
        "A listing matches if it has <b>at least one</b> of the selected.\n"
        "Leave empty for any, then <b>Done</b>."
    )
    await _show(target, text, pets_keyboard(selected), edit=edit)


async def ask_confirm(target: Message, state: FSMContext, sources, *, edit: bool) -> None:
    await state.set_state(NewSub.confirm)
    data = await state.get_data()
    title = sources[data["source"]].title
    text = f"📝 <b>Check your subscription</b> · {title}\n\n{describe_filter(_filter(data))}"
    await _show(target, text, confirm_keyboard(), edit=edit)


async def cmd_new(message: Message, state: FSMContext, sources) -> None:
    await state.clear()
    if len(sources) == 1:
        # Один сайт — питати нема про що; крок з'явиться з другим джерелом.
        await state.update_data(source=next(iter(sources)))
        await ask_continent(message, state, edit=False)
        return
    await state.set_state(NewSub.source)
    await message.answer("Which site?", reply_markup=sources_keyboard(sources))


async def on_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text("✖️ Cancelled.")
    await query.answer()


async def on_source(query: CallbackQuery, callback_data: Pick, state: FSMContext, sources) -> None:
    if callback_data.value not in sources:
        await query.answer("Unknown site", show_alert=True)
        return
    await state.update_data(source=callback_data.value)
    await ask_continent(query.message, state, edit=True)
    await query.answer()


async def on_continent(query: CallbackQuery, callback_data: Pick, state: FSMContext, sources) -> None:
    if callback_data.value not in CONTINENTS:
        await query.answer("Unknown continent", show_alert=True)
        return
    await query.answer("Loading countries…")
    source = sources[(await state.get_data())["source"]]
    try:
        countries = await source.countries(callback_data.value)
    except Exception:
        log.exception("countries(%s) упав", callback_data.value)
        await state.clear()
        await query.message.edit_text("⚠️ Couldn't load countries right now. Try /new again in a few minutes.")
        return
    await state.update_data(
        continent=callback_data.value,
        countries=[[c.slug, c.name, c.count] for c in countries],
    )
    await ask_country(query.message, state, page=0)


async def on_country_page(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    await ask_country(query.message, state, page=int(callback_data.value))
    await query.answer()


async def on_country(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    country = None
    if callback_data.value != ANY:
        country = _country_at(await state.get_data(), callback_data.value)
        if country is None:
            await query.answer("Unknown country", show_alert=True)
            return
    await state.update_data(country=country, exclude=[], countries=None)
    await ask_dates(query.message, state, edit=True)
    await query.answer()


async def on_exclude_mode(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(exclude=[])
    await ask_exclude(query.message, state, page=0)
    await query.answer()


async def on_exclude_page(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    await ask_exclude(query.message, state, page=int(callback_data.value))
    await query.answer()


async def on_exclude_toggle(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    data = await state.get_data()
    slug = _country_at(data, callback_data.value)
    if slug is None:
        await query.answer("Unknown country", show_alert=True)
        return
    excluded = list(data.get("exclude") or [])
    if slug in excluded:
        excluded.remove(slug)
    else:
        excluded.append(slug)
    await state.update_data(exclude=excluded)
    page = int(callback_data.value) // COUNTRIES_PER_PAGE
    await query.message.edit_reply_markup(
        reply_markup=countries_keyboard(data["countries"], page, excluded)
    )
    await query.answer()


async def on_exclude_done(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(country=None, countries=None)
    await ask_dates(query.message, state, edit=True)
    await query.answer()


async def on_any_dates(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(date_from=None, date_to=None)
    await ask_duration(query.message, state, edit=True)
    await query.answer()


async def on_dates_text(message: Message, state: FSMContext) -> None:
    try:
        start, end = parse_date_range(message.text, date.today())
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(date_from=start.isoformat(), date_to=end.isoformat())
    await ask_duration(message, state, edit=False)


async def on_duration(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    low = None if callback_data.value == ANY else int(callback_data.value)
    await state.update_data(min_days=low, max_days=None)
    await ask_pets(query.message, state, edit=True)
    await query.answer()


async def on_duration_text(message: Message, state: FSMContext) -> None:
    try:
        low, high = parse_duration(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(min_days=low, max_days=high)
    await ask_pets(message, state, edit=False)


async def on_pet_toggle(query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
    if callback_data.value not in PETS:
        await query.answer("Unknown pet", show_alert=True)
        return
    selected = list((await state.get_data()).get("pets") or [])
    if callback_data.value in selected:
        selected.remove(callback_data.value)
    else:
        selected.append(callback_data.value)
    await state.update_data(pets=selected)
    await query.message.edit_reply_markup(reply_markup=pets_keyboard(selected))
    await query.answer()


async def on_pets_done(query: CallbackQuery, callback_data: Pick, state: FSMContext, sources) -> None:
    if callback_data.value == ANY:
        await state.update_data(pets=[])
    await ask_confirm(query.message, state, sources, edit=True)
    await query.answer()


async def on_save(query: CallbackQuery, state: FSMContext, store: Store, worker: Worker) -> None:
    data = await state.get_data()
    sub_id = store.add_subscription(query.message.chat.id, data["source"], _filter(data))
    await state.clear()
    await query.message.edit_text(
        f"💾 <b>Subscription #{sub_id} saved</b>\n\n{describe_filter(_filter(data))}\n\n"
        "⏳ Indexing current listings — I'll tell you when it's live, "
        "then message you only about new dates."
    )
    await query.answer("Saved")
    worker.wake()


def build_router() -> Router:
    """Свіжий роутер на кожен Dispatcher: aiogram дозволяє приєднати роутер лише раз."""
    router = Router(name="dialog")
    router.message.register(cmd_new, Command("new"))
    router.callback_query.register(on_cancel, Pick.filter(F.step == "cancel"))
    router.callback_query.register(on_source, NewSub.source, Pick.filter(F.step == "src"))
    router.callback_query.register(on_continent, NewSub.continent, Pick.filter(F.step == "cont"))
    router.callback_query.register(on_country_page, NewSub.country, Pick.filter(F.step == "cpage"))
    router.callback_query.register(on_country, NewSub.country, Pick.filter(F.step == "ctry"))
    router.callback_query.register(on_exclude_mode, NewSub.country, Pick.filter(F.step == "cmode"))
    router.callback_query.register(on_exclude_page, NewSub.exclude, Pick.filter(F.step == "epage"))
    router.callback_query.register(on_exclude_toggle, NewSub.exclude, Pick.filter(F.step == "excl"))
    router.callback_query.register(on_exclude_done, NewSub.exclude, Pick.filter(F.step == "edone"))
    router.callback_query.register(on_any_dates, NewSub.dates, Pick.filter(F.step == "dates"))
    router.message.register(on_dates_text, NewSub.dates, F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_duration, NewSub.duration, Pick.filter(F.step == "dur"))
    router.message.register(on_duration_text, NewSub.duration, F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_pet_toggle, NewSub.pets, Pick.filter(F.step == "pet"))
    router.callback_query.register(on_pets_done, NewSub.pets, Pick.filter(F.step == "pets"))
    router.callback_query.register(on_save, NewSub.confirm, Pick.filter(F.step == "save"))
    return router
