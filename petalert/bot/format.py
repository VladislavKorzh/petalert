"""Telegram texts: alert, table of alerts, filter description. Pure functions.

Parse mode is HTML, so every string from the site goes through `html.escape`:
otherwise a listing titled "Cats & Dogs <3" would break sending the whole
message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from html import escape, unescape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..models import CONTINENTS, Filter, Listing, Slot

CAPTION_LIMIT = 1024  # photo caption, in Telegram's units (see tg_len)
TEXT_LIMIT = 4096
TITLE_LIMIT = 200
PAGE_SIZE = 10  # listings per page of an alert table
MORE_DATES = 2  # extra new dates of one listing shown in the table; its card has all
CITY_LIMIT = 10  # a table row has to fit a phone screen (≤ 35 monospace characters)

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


def subscriptions_label(sub_ids: Sequence[int]) -> str:
    return ("Subscriptions " if len(sub_ids) > 1 else "Subscription ") + ", ".join(f"#{i}" for i in sub_ids)


def render_alert(
    listing: Listing,
    slots: list[Slot],
    sub_ids: list[int],
    source_title: str,
    *,
    limit: int = CAPTION_LIMIT,  # a photo caption; a text message can take TEXT_LIMIT
) -> tuple[str, InlineKeyboardMarkup]:
    head = [f"🏡 <b>{_title(listing)}</b>", "", f"📍 {escape(listing.place)}"]
    if listing.pets:
        head.append(escape(pets_line(listing.pets)))
    head += ["", "🆕 <b>New dates:</b>" if len(slots) > 1 else "🆕 <b>New date:</b>"]
    tail = ["", f"🔔 {subscriptions_label(sub_ids)} · {escape(source_title)}"]
    blocks = [slot_lines(s) for s in slots]

    def build(shown: int) -> str:
        body = blocks[:shown]
        hidden = len(blocks) - shown
        if hidden:
            body.append(f"➕ <i>… and {hidden} more date{'' if hidden == 1 else 's'}</i>")
        return "\n".join(head + body + tail)

    shown = len(blocks)
    text = build(shown)
    while tg_len(text) > limit and shown > 1:
        shown -= 1
        text = build(shown)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"🔗 Open on {source_title}", url=listing.url)]]
    )
    return text, markup


def flag(code: str | None) -> str:
    """`pt` → 🇵🇹 (two regional indicator letters); a pin if the code is unknown."""
    if not code or len(code) != 2 or not code.isascii() or not code.isalpha():
        return "📍"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("a")) for ch in code.lower())


def country_tag(code: str | None) -> str:
    """`pt` → `🇵🇹 PT`: the flag, and the ISO code for flags that look alike."""
    tag = flag(code)
    return f"{tag} {code.upper()}" if code and tag != "📍" else tag


def city(listing: Listing) -> str:
    """The first part of the place ("Espiche" of "Espiche, Faro, Portugal"),
    short enough for a table row on a phone."""
    name = listing.place.split(",")[0].strip() or country_name(listing.country)
    return name if len(name) <= CITY_LIMIT else name[: CITY_LIMIT - 1].rstrip(" -") + "…"


def _row(n: str, slot: Slot, where: str = "") -> str:
    dates = f"{slot.start:%d.%m}–{slot.end:%d.%m}"
    return f"{n:>2} {dates:<11} {slot.days:>3} {where}".rstrip()


def render_table(items: Sequence[tuple[Listing, Sequence[Slot], Sequence[int]]], page: int) -> str:
    """One page of several new listings as a monospace table: number, dates,
    days, place — in the given order (nearest dates first). More new dates of
    the same listing get lines of their own, without a number."""
    sub_ids = sorted({i for _, _, subs in items for i in subs})
    # "Days" is wider than its numbers (≤ 3 digits): it takes the gap before them.
    rows = [f"{'#':>2} {'Dates':<11}Days Where"]
    first = page * PAGE_SIZE
    for n, (listing, slots, _) in enumerate(items[first : first + PAGE_SIZE], start=first + 1):
        rows.append(_row(str(n), slots[0], f"{country_tag(listing.country_code)} {escape(city(listing))}"))
        rows += [_row("", s) for s in slots[1 : 1 + MORE_DATES]]
        if len(slots) > 1 + MORE_DATES:
            rows.append(f"{'':>2} + {len(slots) - 1 - MORE_DATES} more")
    return "\n".join(
        [
            f"🔔 <b>{len(items)} new sits</b> · {subscriptions_label(sub_ids)}",
            "<pre>" + "\n".join(rows) + "</pre>",
            "👇 Tap a number for details",
        ]
    )


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
