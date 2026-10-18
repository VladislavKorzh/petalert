"""/search in the bot: the query (petalert/search.py), then a table of results
that works like an alert table (bot/browse.py) — a number opens the card.

`/search <query>` searches right away; a bare `/search` explains the format and
takes the next message as the query. Plain text with a date in it, outside any
dialog, is a search too.
"""

from __future__ import annotations

import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import geo
from ..db import Store
from ..models import CONTINENTS
from ..search import MAX_RESULTS, Found, Search, has_date, parse, run
from .browse import Item, list_message, pack
from .dialog import ALL_OFF
from .format import date_range
from .ui import NO_PREVIEW

log = logging.getLogger(__name__)

SEARCH_HELP = (
    "🔎 <b>Search open sits by start date</b>\n"
    "Send places, dates and an optional ± margin in days:\n"
    "• <code>eu(-uk) ±3 30.11.2026</code> — Europe without the UK, starting 27 Nov – 3 Dec\n"
    "• <code>eu(-uk) ±3 30.11.2026 -</code> — the same, starting 27 Nov or later\n"
    "• <code>pt, es, 1.12.2026 - 20.12.2026</code> — Portugal or Spain, starting in that range\n\n"
    "🌍 Places: <code>eu</code>, <code>asia</code>, <code>africa</code>, <code>oceania</code>, "
    "<code>north america</code>, <code>south america</code>; countries by code "
    "(<code>pt</code>, <code>uk</code>) or name; <code>-xx</code> leaves one out. None = anywhere.\n"
    f"ℹ️ Searches the sites on in /sites, nearest start first, up to {MAX_RESULTS} sits."
)
BUSY = "⏳ A search is already running — wait for it to finish."


class SearchStates(StatesGroup):
    query = State()


def describe(search: Search) -> tuple[str, str]:
    """(where, when) as plain text: "Europe except United Kingdom", "27 Nov – 3 Dec 2026"."""
    if set(search.continents) == set(CONTINENTS) and not search.countries:
        where = "Anywhere"
    else:
        where = ", ".join([CONTINENTS[c] for c in search.continents] + [geo.name(c) for c in search.countries])
    if search.exclude:
        where += " except " + ", ".join(geo.name(c) for c in search.exclude)
    if search.start_to is None:
        when = f"from {search.start_from:%-d %b %Y}"
    elif search.start_to == search.start_from:
        when = f"{search.start_from:%-d %b %Y}"
    else:
        when = date_range(search.start_from, search.start_to)
    return where, when


def header(search: Search, found: Found) -> str:
    where, when = describe(search)
    n = len(found.items)
    lines = [
        f"🔎 <b>{n} open sit{'' if n == 1 else 's'}</b> · start {when}" if n else f"🔎 <b>Nothing found</b> · start {when}",
        f"🌍 {escape(where)}",
    ]
    if found.more:
        lines.append(f"ℹ️ The nearest {n} — narrow the search for later ones.")
    lines += [f"⚠️ {escape(problem)}" for problem in found.problems]
    return "\n".join(lines)


async def parsed(message: Message, text: str) -> Search | None:
    """The query, or None after telling the user what is wrong with it."""
    try:
        return parse(text, date.today())
    except ValueError as exc:
        await message.answer(f"❌ {escape(str(exc), quote=False)}\n\nSend /search for the format.")
        return None


async def run_search(message: Message, query: Search, store: Store, sources, searching: set[int]) -> None:
    """Search the chat's sites and answer with the table."""
    chat = message.chat.id
    on = [sources[k] for k in store.enabled_sources(chat, sources)]
    if not on:
        await message.answer(ALL_OFF)
        return
    if chat in searching:  # each one reads the sites; one at a time is plenty
        await message.answer(BUSY)
        return
    searching.add(chat)
    try:
        where, when = describe(query)
        progress = await message.answer(f"🔎 Searching… {escape(where)} · start {when}")
        try:
            found = await run(query, on)
        except Exception:
            log.exception("search %r failed", query)
            await progress.edit_text("⚠️ The search failed. Try again in a few minutes.")
            return
        head = header(query, found)
        if not found.items:
            await progress.edit_text(head)
            return
        items = [Item(listing, slots, []) for listing, slots in found.items]
        list_id = store.add_alert_list(chat, pack(items, head))
        text, markup = list_message(items, list_id, 0, head)
        await progress.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    finally:
        searching.discard(chat)


async def cmd_search(
    message: Message, command: CommandObject, state: FSMContext, store: Store, sources, searching: set[int]
) -> None:
    await state.clear()
    if not command.args:
        await state.set_state(SearchStates.query)
        await message.answer(SEARCH_HELP)
        return
    if query := await parsed(message, command.args):
        await run_search(message, query, store, sources, searching)


async def on_query(message: Message, state: FSMContext, store: Store, sources, searching: set[int]) -> None:
    # A query that doesn't parse keeps the state: the next message is a retry.
    if query := await parsed(message, message.text):
        # Before searching, not after: a /new started meanwhile must keep its state.
        await state.clear()
        await run_search(message, query, store, sources, searching)


async def on_plain_text(message: Message, store: Store, sources, searching: set[int]) -> None:
    if query := await parsed(message, message.text):
        await run_search(message, query, store, sources, searching)


def build_router() -> Router:
    router = Router(name="search")
    router.message.register(cmd_search, Command("search"))
    router.message.register(on_query, SearchStates.query, F.text, ~F.text.startswith("/"))
    router.message.register(
        on_plain_text, StateFilter(None), F.text, ~F.text.startswith("/"), F.text.func(has_date)
    )
    return router
