"""/search: open sits by start date, across the sites a chat has on.

    eu(-uk), ±3, 30.11.2026      Europe without the UK, starting 27 Nov – 3 Dec
    eu(-uk), +5, 30.11.2026      starting 30 Nov – 5 Dec (-2 would be 28 – 30 Nov)
    eu(-uk), ±3, 30.11.2026 -    starting 27 Nov or later
    pt, es, 1.12 - 20.12         Portugal or Spain, starting in that range
    eu, 1.12 - 20.12, min 14     the same in Europe, sits of 14 days or more

Dates are a range of START dates (±N widens it on both sides, +N only after,
-N only before); how long a sit lasts doesn't matter unless `min N` asks for
at least N days (as the sites count: end − start). The range begins tomorrow
at the earliest: a sit starting today is too late to apply for (and THS
doesn't keep today's starts in its start-date order). Places are matched on the listing's ISO country code,
which both sites give (`geo` knows each code's continent).

Sites are read nearest start first (`Source.by_start`) and only until the
range is over or there are enough results, so a search is usually a request
or two per site and place, not a crawl of the continent.

No Telegram here: the bot (bot/search.py) and the CLI (`python -m petalert
search …`) present the result.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import geo
from .models import CONTINENTS, Filter, Listing, Slot, first_open_start
from .sources.base import Source, TooBroad

log = logging.getLogger(__name__)

MAX_RESULTS = 100  # listings in one result: 2 pages of the table, nearest first
MAX_FLEX = 180  # days, on either side
MAX_DAYS = 365  # a minimum length, as in /new

# ±3 / +-3 / +/-3 (both sides), +5 (after), -2 (before). A minus right before
# the number only, not inside a date (2026-11-30) or a range (1.12 -20.12).
_FLEX = re.compile(
    r"(?:(?P<both>±|\+\s*/?\s*[-−]|[-−]\s*/?\s*\+)\s*|(?P<after>\+)\s*|(?<![\w.])(?P<before>[-−]))"
    r"(?P<days>\d{1,3})(?![\d.])(?:\s*d(?:ays?)?\b)?",
    re.IGNORECASE,
)
# min 14 / min14 / min 14d / min 14 days: sits at least that long
_MIN_DAYS = re.compile(
    r"(?<![\w-])min\.?\s*[:=]?\s*(?P<days>\d{1,3})(?![\d.])(?:\s*(?:d|days?|nights?)\b)?", re.IGNORECASE
)
# 2026-11-30, 30.11.2026, 30/11/2026, 30.11 or 30.11. (no year: see _resolve)
_DATE = re.compile(
    r"(?<![\d.])(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?)(?!\d)(?!\.\d)\.?"
)
_RANGE = re.compile(r"\s*(?:[-–—]|to|till|until)?\s*", re.IGNORECASE)  # between two dates
_OPEN_END = re.compile(r"[\s,]*[-–—](?![a-z])", re.IGNORECASE)  # "30.11.2026 -", but not "30.11.2026 -uk"
_DASHES = "-–—"
HELP_FORMAT = "Dates look like 30.11.2026, a margin like ±3, +5 or -2, a minimum length like min 14."
LONE_DASH = (
    "Can't read a lone '-'. A range of starts is 1.12.2026 - 20.12.2026, \"from then on\" is "
    "30.11.2026 -, and -uk leaves a country out."
)
YEARS_AHEAD = 3


@dataclass(frozen=True)
class Search:
    start_from: date
    start_to: date | None = None  # None: no end, nearest first
    continents: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()  # ISO codes, lower case
    exclude: tuple[str, ...] = ()  # ISO codes; wins over the above
    min_days: int | None = None  # a slot at least this long (Slot.days)

    def matches(self, listing: Listing) -> bool:
        code = listing.country_code
        if code in self.exclude:
            return False
        # (MindMyHouse regions such as "Caribbean" have no code: only a continent finds them)
        return listing.continent in self.continents or (code is not None and code in self.countries)

    def slots(self, listing: Listing) -> list[Slot]:
        """Open slots starting within the range (and long enough), nearest first."""
        return sorted(
            (
                s for s in listing.slots
                if s.is_open and s.start >= self.start_from
                and (self.start_to is None or s.start <= self.start_to)
                and (self.min_days is None or s.days >= self.min_days)
            ),
            key=lambda s: (s.start, s.end),
        )

    def scopes(self) -> list[Filter]:
        """What to ask the sites: each continent, and each country outside them."""
        when = {"date_from": self.start_from, "min_days": self.min_days}
        scopes = [Filter(c, **when) for c in self.continents]
        for code in self.countries:
            if geo.continent(code) not in self.continents:
                scopes.append(Filter(geo.continent(code), country=geo.slug(code), **when))
        return scopes


def has_date(text: str) -> bool:
    """Whether plain text is meant as a search (the bot treats it so)."""
    return _DATE.search(text) is not None


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Spaces over parsed parts, so the positions of the rest stay put."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _ymd(match: re.Match, today: date) -> tuple[int, int, int | None]:
    """(day, month, year or None) of a date match; the year within reach."""
    iso_y, iso_m, iso_d, d, m, y = match.groups()
    day, month, year = (int(iso_d), int(iso_m), int(iso_y)) if iso_y else (int(d), int(m), int(y) if y else None)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"The date {match.group(0).rstrip('.')} doesn't exist. Check day and month.")
    if year is not None and not today.year - 1 <= year <= today.year + YEARS_AHEAD:
        raise ValueError(f"Check the year of {match.group(0).rstrip('.')}.")
    return day, month, year


def _date(day: int, month: int, year: int, raw: re.Match) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError(f"The date {raw.group(0).rstrip('.')} doesn't exist. Check day and month.") from None


def _resolve(matches: list[re.Match], today: date, after: timedelta, open_end: bool) -> tuple[date, date | None]:
    """The (first, last) start dates, filling in missing years. A date without
    one is taken in the year that puts it nearest to today, the other date of
    a range in a year that keeps the range in order — and if a range without
    any year is then already over, a year later ("1.05 - 1.06" in October is
    next year's)."""
    parts = [_ymd(m, today) for m in matches]
    years = [y for _, _, y in parts]
    if years[0] is None:
        if len(parts) == 2 and years[1] is not None:
            day, month, _ = parts[0]
            years[0] = years[1] if (month, day) <= parts[1][1::-1] else years[1] - 1
        else:
            day, month, _ = parts[0]
            years[0] = min(
                (today.year + k for k in (-1, 0, 1)),
                key=lambda y: abs((date(y, month, min(day, 28)) - today).days),
            )
    if len(parts) == 2 and years[1] is None:
        years[1] = years[0] if parts[1][1::-1] >= parts[0][1::-1] else years[0] + 1
    dates = [_date(d, m, y, raw) for (d, m, _), y, raw in zip(parts, years, matches)]
    yearless = [y is None for _, _, y in parts]
    end = dates[-1] if not open_end else None
    if end is not None and end + after <= today and all(yearless):
        dates = [_date(d.day, d.month, d.year + 1, raw) for d, raw in zip(dates, matches)]
    first, last = dates[0], (None if open_end else dates[-1])
    if last is not None and last < first:
        raise ValueError("The second date must not be before the first.")
    return first, last


def _dates(text: str, today: date, after: timedelta) -> tuple[date | None, date | None, list[tuple[int, int]]]:
    """(lowest start, highest start, the parsed spans) — before ±; None: open."""
    matches = list(_DATE.finditer(text))
    if len(matches) > 2:
        raise ValueError("At most two dates: a start, or a range of starts like 1.12.2026 - 20.12.2026.")
    spans = [m.span() for m in matches]
    if not matches:
        return None, None, spans
    open_end = False
    if len(matches) == 2:
        between = _RANGE.fullmatch(text, matches[0].end(), matches[1].start())
        if between:  # the dash or "to" of the range, not a dash of an exclusion
            spans.append(between.span())
    elif dash := _OPEN_END.match(text, matches[0].end()):
        spans.append(dash.span())
        open_end = True
    first, last = _resolve(matches, today, after, open_end)
    return first, last, spans


def _places(text: str) -> tuple[list[str], list[str], list[str]]:
    continents: list[str] = []
    countries: list[str] = []
    exclude: list[str] = []

    def add(token: str) -> bool:
        negative = token[0] in _DASHES  # "-uk": the name right after the dash
        name = token.lstrip(_DASHES)
        found = geo.lookup_place(name) if name[:1].isalnum() else None
        if found is None:
            return False
        kind, value = found
        if negative and kind == "continent":
            raise ValueError(f"Only countries can be left out, not {name!r}.")
        target = exclude if negative else continents if kind == "continent" else countries
        if value not in target:
            target.append(value)
        return True

    for part in re.split(r"[,;()\[\]]", text):
        words = part.split()
        if any(not w.strip(_DASHES) for w in words):
            raise ValueError(LONE_DASH)
        i = 0
        while i < len(words):
            # The longest run of words that is a place: "united kingdom", "eu north america".
            j = next((j for j in range(len(words), i, -1) if add(" ".join(words[i:j]))), None)
            if j is not None:
                i = j
                continue
            word = words[i]
            if any(ch.isdigit() for ch in word):
                raise ValueError(f"Can't read {word!r}. {HELP_FORMAT}")
            raise ValueError(
                f"Unknown place {word!r}. Use a continent (eu, asia, africa, oceania, north america, "
                "south america), a country code (pt, uk) or a country name."
            )
    return continents, countries, exclude


def parse(text: str, today: date) -> Search:
    """A query like `eu(-uk), ±3, 30.11.2026 -, min 14`. The ValueError
    message is meant for the user."""
    lengths = list(_MIN_DAYS.finditer(text))
    if len(lengths) > 1:
        raise ValueError("One minimum length, like min 14.")
    min_days = int(lengths[0]["days"]) if lengths else None
    if min_days is not None and not 1 <= min_days <= MAX_DAYS:
        raise ValueError(f"A minimum length is 1 to {MAX_DAYS} days.")
    text = _blank(text, [m.span() for m in lengths])
    margins: dict[str, int] = {}  # days "before" and "after" the dates
    flexes = list(_FLEX.finditer(text))
    for m in flexes:
        sides = ("before", "after") if m["both"] else ("before",) if m["before"] else ("after",)
        if any(side in margins for side in sides):
            raise ValueError("One margin per side: ±3, or +5 and -2.")
        if int(m["days"]) > MAX_FLEX:
            raise ValueError(f"A margin can be at most {MAX_FLEX} days.")
        margins.update(dict.fromkeys(sides, int(m["days"])))
    before, after = (timedelta(days=margins.get(side, 0)) for side in ("before", "after"))
    text = _blank(text, [m.span() for m in flexes])
    first, last, spans = _dates(text, today, after)
    earliest = today + timedelta(days=1)
    start_to = last + after if last else None
    if start_to is not None and start_to < earliest:
        raise ValueError("These start dates have passed (a search starts from tomorrow).")
    continents, countries, exclude = _places(_blank(text, spans))
    if not continents and not countries:
        continents = list(CONTINENTS)  # anywhere
    return Search(
        start_from=max(first - before, earliest) if first else earliest,
        start_to=start_to,
        continents=tuple(continents),
        countries=tuple(countries),
        exclude=tuple(exclude),
        min_days=min_days,
    )


@dataclass
class Found:
    items: list[tuple[Listing, list[Slot]]]  # nearest start first, at most MAX_RESULTS
    more: bool = False  # cut at MAX_RESULTS: later sits exist
    problems: list[str] = field(default_factory=list)  # per site, for the user


async def run(search: Search, sources: Sequence[Source]) -> Found:
    found: dict[tuple[str, str], tuple[Listing, list[Slot]]] = {}
    more = False
    problems: dict[str, str] = {}
    for source in sources:
        for scope in search.scopes():
            matched = pages = 0
            try:
                async with aclosing(source.by_start(scope)) as stream:
                    async for page in stream:
                        pages += 1
                        for listing in page:
                            key = (listing.source, listing.id)
                            if key in found or not search.matches(listing):
                                continue
                            if slots := search.slots(listing):
                                found[key] = (listing, slots)
                                matched += 1
                        if matched >= MAX_RESULTS:
                            more = True  # whatever comes next starts later
                            break
                        last = first_open_start(page[-1], search.start_from, search.min_days) if page else None
                        if search.start_to and last and last > search.start_to:
                            break  # and so does every listing after it
            except TooBroad as exc:
                problems[source.key] = f"{source.title}: {exc}"
            except Exception as exc:
                log.warning("search %s %s: %r", source.key, scope.to_json(), exc)
                problems.setdefault(source.key, f"{source.title} failed — results may be incomplete, try again later")
            log.info("search %s %s: %d pages, %d matched", source.key, scope.to_json(), pages, matched)
    items = sorted(found.values(), key=lambda i: (i[1][0].start, i[1][0].end, i[0].source, i[0].id))
    return Found(items[:MAX_RESULTS], more or len(items) > MAX_RESULTS, list(problems.values()))
