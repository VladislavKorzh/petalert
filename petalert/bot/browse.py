"""Several sits as one table message, and its buttons.

A tick that finds more than one new listing for a chat sends them as one table
(nearest dates first) instead of a stream of photo cards; /search answers with
the same kind of table. A number opens that listing's card in the same
message, ◀ ▶ flip through the cards, and "Back to the list" returns to the
table. What a table shows is stored with it (`alert_list`), so the buttons work
without asking the site again: a card is the sit as it was when alerted (or
found), and "Open on …" has the live page.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, NamedTuple

from aiogram import Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from pydantic import Field

from ..db import Store
from ..models import Listing, Slot
from .format import PAGE_SIZE, TEXT_LIMIT, render_alert, render_table
from .ui import NO_PREVIEW, answer, chat_id, edit

EXPIRED = "⌛ This list has expired."
NUMBERS_PER_ROW = 5


class Item(NamedTuple):
    listing: Listing
    slots: list[Slot]  # the new ones (a search: the ones in its range), nearest first
    sub_ids: list[int]  # empty for a search result


# Bounded: a forged huge number would otherwise reach SQLite and crash the
# handler (OverflowError); out of range, it fails the filter like any stale button.
Id = Annotated[int, Field(ge=1, lt=2**63)]


class Browse(CallbackData, prefix="b"):
    list_id: Id
    view: str  # page | card | noop
    n: Annotated[int, Field(ge=0, lt=10_000)] = 0  # page or card index, from 0


def pages(count: int) -> int:
    return max(1, -(-count // PAGE_SIZE))


def pack(items: list[Item], header: str | None = None) -> str:
    """An alert table is stored as a list of items; one with its own header
    (a search) as {"header", "items"}."""
    packed = [
        {
            "listing": {
                "source": l.source, "id": l.id, "title": l.title, "url": l.url,
                "continent": l.continent, "country": l.country,
                "country_code": l.country_code, "place": l.place,
                "photo_url": l.photo_url, "pets": [list(p) for p in l.pets],
            },
            "slots": [
                {"id": s.id, "start": s.start.isoformat(), "end": s.end.isoformat(),
                 "applicants": s.applicants}
                for s in slots
            ],
            "subs": sub_ids,
        }
        for l, slots, sub_ids in items
    ]
    return json.dumps(packed if header is None else {"header": header, "items": packed}, ensure_ascii=False)


def unpack(raw: str) -> tuple[list[Item], str | None]:
    data = json.loads(raw)
    header = None
    if isinstance(data, dict):
        header, data = data["header"], data["items"]
    items = []
    for entry in data:
        d: dict[str, Any] = entry["listing"]
        listing = Listing(**{**d, "pets": tuple((name, count) for name, count in d["pets"])})
        slots = [
            Slot(s["id"], date.fromisoformat(s["start"]), date.fromisoformat(s["end"]), True,
                 applicants=s["applicants"])
            for s in entry["slots"]
        ]
        items.append(Item(listing, slots, entry["subs"]))
    return items, header


def _button(text: str, list_id: int, view: str, n: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Browse(list_id=list_id, view=view, n=n).pack())


def _nav(list_id: int, view: str, n: int, total: int) -> list[InlineKeyboardButton]:
    """◀ n/total ▶ — wrapping around, so the buttons never move under the thumb."""
    return [
        _button("◀", list_id, view, (n - 1) % total),
        _button(f"{n + 1}/{total}", list_id, "noop"),
        _button("▶", list_id, view, (n + 1) % total),
    ]


def list_message(
    items: list[Item], list_id: int, page: int = 0, header: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    first = page * PAGE_SIZE
    numbers = [_button(str(n + 1), list_id, "card", n) for n in range(first, min(first + PAGE_SIZE, len(items)))]
    rows = [numbers[i : i + NUMBERS_PER_ROW] for i in range(0, len(numbers), NUMBERS_PER_ROW)]
    if pages(len(items)) > 1:
        rows.append(_nav(list_id, "page", page, pages(len(items))))
    return render_table(items, page, header), InlineKeyboardMarkup(inline_keyboard=rows)


def card_message(
    items: list[Item], list_id: int, n: int, source_titles: dict[str, str]
) -> tuple[str, InlineKeyboardMarkup, LinkPreviewOptions]:
    """The alert card of item `n`. The message stays a text message (an edit
    can't turn it into a photo), so the photo is a large link preview above
    the text."""
    listing, slots, sub_ids = items[n]
    text, open_button = render_alert(
        listing, slots, sub_ids, source_titles.get(listing.source, listing.source), limit=TEXT_LIMIT
    )
    rows = [_nav(list_id, "card", n, len(items))] if len(items) > 1 else []
    rows.append([_button("📋 Back to the list", list_id, "page", n // PAGE_SIZE)])
    rows += open_button.inline_keyboard
    preview = (
        LinkPreviewOptions(url=listing.photo_url, prefer_large_media=True, show_above_text=True)
        if listing.photo_url
        else NO_PREVIEW
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows), preview


async def on_browse(query: CallbackQuery, callback_data: Browse, store: Store, sources) -> None:
    # By chat as well: callback data comes from the client.
    raw = store.alert_list(callback_data.list_id, chat_id(query))
    if raw is None:
        await answer(query, EXPIRED, show_alert=True)
        return
    items, header = unpack(raw)
    view, n = callback_data.view, callback_data.n
    if view == "noop":
        await answer(query)
    elif view == "page" and 0 <= n < pages(len(items)):
        text, markup = list_message(items, callback_data.list_id, n, header)
        await answer(query)
        await edit(query, text, markup, preview=NO_PREVIEW)
    elif view == "card" and 0 <= n < len(items):
        titles = {key: source.title for key, source in sources.items()}
        text, markup, preview = card_message(items, callback_data.list_id, n, titles)
        await answer(query)
        await edit(query, text, markup, preview=preview)
    else:
        await answer(query, "Unknown button", show_alert=True)


def build_router() -> Router:
    router = Router(name="browse")
    router.callback_query.register(on_browse, Browse.filter())
    return router
