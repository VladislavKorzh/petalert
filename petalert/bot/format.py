"""Telegram texts: alert, digest, filter description. Pure functions.

Parse mode is HTML, so every string from the site goes through `html.escape`:
otherwise a listing titled "Cats & Dogs <3" would break sending the whole
message.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape, unescape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..models import CONTINENTS, Filter, Listing, Slot

CAPTION_LIMIT = 1024  # photo caption, in Telegram's units (see tg_len)
TEXT_LIMIT = 4096
TITLE_LIMIT = 200

_PLURAL = {"fish": "fish", "poultry": "poultry"}
PET_EMOJI = {
    "dog": "🐕", "cat": "🐈", "horse": "🐎", "bird": "🐦", "fish": "🐟",
    "reptile": "🦎", "poultry": "🐔", "farm animal": "🐄", "small pet": "🐹",
}
_SMALL_WORDS = {"and", "of", "the"}


_TAG = re.compile(r"<[^>]+>")


def tg_len(html_text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units of the text after the
    HTML entities are parsed (an emoji is 2, a tag is 0)."""
    return len(unescape(_TAG.sub("", html_text)).encode("utf-16-le")) // 2


def pet_label(name: str, count: int) -> str:
    plural = _PLURAL.get(name, f"{name}s")
    return f"{PET_EMOJI.get(name, '🐾')} {count} {name if count == 1 else plural}"


def pets_line(pets: tuple[tuple[str, int], ...]) -> str:
    return " · ".join(pet_label(name, count) for name, count in pets)


def country_name(slug: str) -> str:
    """`bosnia-and-herzegovina` → `Bosnia and Herzegovina`. Names from the API
    aren't stored: the slug in the filter is enough, and this reads fine."""
    words = slug.split("-")
    return " ".join(
        w if i and w in _SMALL_WORDS else w.capitalize() for i, w in enumerate(words)
    )


def date_range(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%-d %b} – {end:%-d %b %Y}"
    return f"{start:%-d %b %Y} – {end:%-d %b %Y}"


def applicants_label(n: int) -> str:
    return "🟢 no applicants yet" if n == 0 else f"👥 {n} applicant{'' if n == 1 else 's'}"


def slot_lines(slot: Slot) -> str:
    """Two lines per slot: dates in bold, then length and competition below."""
    details = [f"⏳ {slot.days} days"]
    if slot.applicants is not None:
        details.append(applicants_label(slot.applicants))
    return f"📅 <b>{date_range(slot.start, slot.end)}</b>\n      └ " + " · ".join(details)


def _title(listing: Listing, limit: int = TITLE_LIMIT) -> str:
    title = listing.title
    if len(title) > limit:
        title = title[: limit - 1] + "…"
    return escape(title)


def render_alert(
    listing: Listing, slots: list[Slot], sub_ids: list[int], source_title: str
) -> tuple[str, InlineKeyboardMarkup]:
    head = [f"🏡 <b>{_title(listing)}</b>", "", f"📍 {escape(listing.place)}"]
    if listing.pets:
        head.append(escape(pets_line(listing.pets)))
    head += ["", "🆕 <b>New dates:</b>" if len(slots) > 1 else "🆕 <b>New date:</b>"]
    subs = ", ".join(f"#{i}" for i in sub_ids)
    tail = ["", f"🔔 Subscription {subs} · {escape(source_title)}"]
    blocks = [slot_lines(s) for s in slots]

    def build(shown: int) -> str:
        body = blocks[:shown]
        if shown < len(blocks):
            body.append(f"➕ <i>… and {len(blocks) - shown} more dates</i>")
        return "\n".join(head + body + tail)

    shown = len(blocks)
    text = build(shown)
    while tg_len(text) > CAPTION_LIMIT and shown > 1:
        shown -= 1
        text = build(shown)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"🔗 Open on {source_title}", url=listing.url)]]
    )
    return text, markup


def render_digest(items: list[tuple[Listing, list[Slot]]]) -> tuple[str, int]:
    """The text and how many items fit into it: the worker won't mark the rest
    and will send them next tick."""
    blocks: list[str] = []

    def text(n: int) -> str:
        return "\n\n".join([f"📋 <b>{n} more new listings</b>", *blocks])

    for listing, slots in items:
        dates = ", ".join(date_range(s.start, s.end) for s in slots[:2])
        if len(slots) > 2:
            dates += f" (+{len(slots) - 2})"
        block = (
            f'{len(blocks) + 1}. 🏡 <a href="{escape(listing.url)}">{_title(listing, 80)}</a>\n'
            f"     📍 {escape(listing.place)}\n"
            f"     📅 {dates}"
        )
        blocks.append(block)
        if tg_len(text(999)) > TEXT_LIMIT:
            blocks.pop()
            break
    return text(len(blocks)), len(blocks)


def describe_filter(flt: Filter) -> str:
    continent = CONTINENTS.get(flt.continent, flt.continent)
    if flt.country:
        where = f"{country_name(flt.country)} ({continent})"
    elif flt.exclude_countries:
        where = f"{continent}, except " + ", ".join(country_name(c) for c in sorted(flt.exclude_countries))
    else:
        where = f"{continent}, any country"
    if flt.date_from and flt.date_to:
        dates = date_range(flt.date_from, flt.date_to)
    elif flt.date_from:
        dates = f"from {flt.date_from:%-d %b %Y}"
    else:
        dates = "any"
    if flt.min_days and flt.max_days:
        length = f"{flt.min_days}–{flt.max_days} days"
    elif flt.min_days:
        length = f"{flt.min_days}+ days"
    elif flt.max_days:
        length = f"up to {flt.max_days} days"
    else:
        length = "any"
    pets = " or ".join(f"{PET_EMOJI.get(p, '')} {p}" for p in flt.pets) if flt.pets else "any"
    return "\n".join(
        [
            f"🌍 Where: {escape(where)}",
            f"📅 Dates: {dates}",
            f"⏳ Length: {length}",
            f"🐾 Pets: {escape(pets)}",
        ]
    )
