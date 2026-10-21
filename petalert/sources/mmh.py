"""MindMyHouse: the assignment search page, parsed.

The site has no API. Its search page lists 20 assignments per page, but the
map script on every page carries ALL assignments of the search, each with the
full title (the marker's `title` option; the popup cuts long ones), photo,
place, every date range and the pets (verified 2026-10-18: 207 markers for
"Found 207 assignments", ~45 KB gzipped). So one GET of the default search —
all countries, newest first — is the whole site: filters are applied locally,
and one fetch serves every subscription for a few minutes.

Compared with THS there are no applicant counts, no pet counts, no publication
time and no date-slot ids. A slot is identified by its listing and dates (so
changed dates count as new), and with neither ids nor publication time every
slot unseen since the baseline is news (see models.appeared_after) — which is
right here, because the baseline sees the whole site. The exception is a
change on our side that makes old listings matchable for the first time (a
country added to COUNTRIES): deploy it with `rebaseline mmh` (see README).

A page the parser no longer understands must fail loudly (SourceError → the
admin hears after a few ticks), never quietly return listings without dates.

Debug: `python -m petalert.sources --source mmh search --continent europe`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from datetime import date
from html import unescape

from ..http import Http
from ..models import Country, Filter, Listing, Slot, first_open_start, in_area, listing_matches, matching_slots
from .base import Notice, SourceError

log = logging.getLogger(__name__)

BASE = "https://www.mindmyhouse.com"
SEARCH_PAGE = f"{BASE}/sitters/search_assignments"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# The site doesn't block datacenter IPs, so it goes direct even with
# PROXY_MODE=always; the proxy is only for retries. (2026-10-18: for hours the
# proxy pool answered 522 for this site while a direct GET from the VPS worked.)
PROXY_MODE = "fallback"
CACHE_SECONDS = 300  # one fetch serves all filters of a tick and the /new dialog
RETRY_SECONDS = 120  # after a failure: the rest of the tick fails fast instead of refetching

# The site's own country list (the search form's dropdown, 2026-10-18), with a
# continent and an ISO code for each. Some entries are regions ("Caribbean",
# "South East Asia"): no code, so no flag. A name missing here is reported to
# the admin, and its listings match no continent until it is added.
COUNTRIES: dict[str, tuple[str, str | None]] = {
    # Europe
    "Albania": ("europe", "al"), "Austria": ("europe", "at"), "Belgium": ("europe", "be"),
    "Bulgaria": ("europe", "bg"), "Croatia": ("europe", "hr"), "Cyprus": ("europe", "cy"),
    "Czech Republic": ("europe", "cz"), "Denmark": ("europe", "dk"), "Estonia": ("europe", "ee"),
    "Finland": ("europe", "fi"), "France": ("europe", "fr"), "Germany": ("europe", "de"),
    "Gibraltar": ("europe", "gi"), "Greece": ("europe", "gr"), "Hungary": ("europe", "hu"),
    "Iceland": ("europe", "is"), "Ireland": ("europe", "ie"), "Italy": ("europe", "it"),
    "Latvia": ("europe", "lv"), "Liechtenstein": ("europe", "li"), "Lithuania": ("europe", "lt"),
    "Luxembourg": ("europe", "lu"), "Macedonia": ("europe", "mk"), "Malta": ("europe", "mt"),
    "Monaco": ("europe", "mc"), "Netherlands": ("europe", "nl"), "Norway": ("europe", "no"),
    "Poland": ("europe", "pl"), "Portugal": ("europe", "pt"), "Romania": ("europe", "ro"),
    "Slovakia": ("europe", "sk"), "Slovenia": ("europe", "si"), "Spain": ("europe", "es"),
    "Sweden": ("europe", "se"), "Switzerland": ("europe", "ch"), "United Kingdom": ("europe", "gb"),
    # Asia
    "Brunei": ("asia", "bn"), "Cambodia": ("asia", "kh"), "Hong Kong": ("asia", "hk"),
    "India": ("asia", "in"), "Indonesia": ("asia", "id"), "Israel": ("asia", "il"),
    "Japan": ("asia", "jp"), "Laos": ("asia", "la"), "Malaysia": ("asia", "my"),
    "Middle East": ("asia", None), "Nepal": ("asia", "np"), "North East Asia": ("asia", None),
    "Philippines": ("asia", "ph"), "Singapore": ("asia", "sg"), "South East Asia": ("asia", None),
    "South Korea": ("asia", "kr"), "Sri Lanka": ("asia", "lk"), "Taiwan": ("asia", "tw"),
    "Thailand": ("asia", "th"), "United Arab Emirates": ("asia", "ae"), "Vietnam": ("asia", "vn"),
    # Africa
    "Africa": ("africa", None), "Mauritius": ("africa", "mu"), "Morocco": ("africa", "ma"),
    "South Africa": ("africa", "za"),
    # Oceania ("the Pacific" on the site)
    "Australia": ("oceania", "au"), "Cook Islands": ("oceania", "ck"), "Fiji": ("oceania", "fj"),
    "New Zealand": ("oceania", "nz"), "Samoa": ("oceania", "ws"), "Solomon Islands": ("oceania", "sb"),
    "South Pacific": ("oceania", None), "Tonga": ("oceania", "to"), "Vanuatu": ("oceania", "vu"),
    # North and Central America, the Caribbean
    "Belize": ("north-america", "bz"), "Canada": ("north-america", "ca"),
    "Caribbean": ("north-america", None), "Costa Rica": ("north-america", "cr"),
    "Mexico": ("north-america", "mx"), "Panama": ("north-america", "pa"),
    "United States": ("north-america", "us"),
    # South America
    "Argentina": ("south-america", "ar"), "Brazil": ("south-america", "br"),
    "Chile": ("south-america", "cl"), "Peru": ("south-america", "pe"),
    "Suriname": ("south-america", "sr"), "Uruguay": ("south-america", "uy"),
}

# The site's animal names → models.PETS (what filters match on).
PETS = {
    "Dog(s)": "dog", "Cat(s)": "cat", "Horse/Donkey(s)": "horse", "Chicken(s)": "poultry",
    "Bird(s)": "bird", "Fish": "fish", "Rabbit(s)": "small pet", "Guinea pig(s)": "small pet",
    "Rodent(s)": "small pet", "Tortoise/Turtle(s)": "reptile", "Reptile(s)": "reptile",
    "Pig(s)": "farm animal", "Goat(s)": "farm animal", "Farm animal(s)": "farm animal",
    "Other animal(s)": "other animal",  # no filter for it; shown as 🐾
}
KNOWN_PETS = set(PETS.values())
# Region names that say nothing ("France, Saint-Cloud, …, Metropolitan France").
GENERIC_REGIONS = {"Metropolitan France"}

MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

_FOUND = re.compile(r"Found (\d+) assignments?")
# A marker: `L.marker(…, { title: '<full title>' }); markerN.bindPopup('<html>'`.
# Both are single-quoted JS strings (\' and friends escaped). The title option
# is optional here, so a marker without it still parses (with the popup title).
_MARKER = re.compile(
    r"(?:\{ title: '((?:[^'\\]|\\.)*)' \}\);\s*)?marker(\d+)\.bindPopup\('((?:[^'\\]|\\.)*)'"
)
# Where it is: `const markerN = L.marker(new L.LatLng(37.0164626,  -7.9351983), …`.
_POINT = re.compile(r"\bmarker(\d+) = L\.marker\(new L\.LatLng\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)")
_ID = re.compile(r'href="/sitters/assignment/(\d+)"')
_TITLE = re.compile(r'<h2 class="title">(.*?)</h2>', re.S)
_IMG = re.compile(r'<img src="([^"]+)"')
_PLACE = re.compile(r'<div class="summary">(.*?)</div>', re.S)
_DATES_LI = re.compile(r'<li class="date-duration">(.*?)</li>', re.S)
_RANGE = re.compile(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) - (\d{1,2}) ([A-Z][a-z]{2}) (\d{4})")
_PETS_LI = re.compile(r'<li class="pets">(.*?)</li>', re.S)
_TAG = re.compile(r"<[^>]+>")


def slugify(name: str) -> str:
    """`United Kingdom` → `united-kingdom`, the same slugs THS uses."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _text(html: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", html)).split())


def _day(d: str, mon: str, y: str) -> date:
    return date(int(y), MONTHS[mon], int(d))


def _js(raw: str) -> str:
    """The content of a single-quoted JS string literal."""
    return re.sub(r"\\(.)", r"\1", raw)


def parse_popup(popup: str, full_title: str = "", point: tuple[float, float] | None = None) -> Listing:
    """One map marker's popup (already unescaped from JS) → Listing, at the
    marker's `point` (lat, lon). The id, the place and the dates are required
    (ValueError/KeyError/AttributeError)."""
    listing_id = _ID.search(popup).group(1)  # type: ignore[union-attr]
    parts = [p.strip() for p in _text(_PLACE.search(popup).group(1)).split(",")]  # type: ignore[union-attr]
    parts = [p for p in parts if p]
    country = parts[0]  # "France, Fustérouau, Mirande, Occitania": country, town, area, region
    continent, code = COUNTRIES.get(country, ("", None))
    place: list[str] = []
    for part in (parts[1] if len(parts) > 1 else None, parts[-1] if len(parts) > 2 else None):
        if part and part not in place and part != country and part not in GENERIC_REGIONS:
            place.append(part)
    place.append(country)  # always last: the table and countries() rely on it
    dates = _DATES_LI.search(popup)
    if dates is None:
        raise ValueError("no dates")
    ranges = _RANGE.findall(dates.group(1))
    if not ranges or len(ranges) != dates.group(1).count("icon-calendar"):
        # Unparsed dates must not pass as "no dates": the format has changed.
        raise ValueError(f"unrecognised dates: {_text(dates.group(1))[:100]}")
    slots = []
    for d1, m1, y1, d2, m2, y2 in ranges:
        start, end = _day(d1, m1, y1), _day(d2, m2, y2)
        slots.append(Slot(f"{listing_id}-{start:%Y%m%d}-{end:%Y%m%d}", start, end, True))
    pets: list[tuple[str, int | None]] = []
    found = _PETS_LI.search(popup)
    for label in _text(found.group(1)).split(", ") if found else ():
        name = PETS.get(label, label.removesuffix("(s)").lower())
        if name and name not in [p for p, _ in pets]:
            pets.append((name, None))  # the site doesn't say how many
    img = _IMG.search(popup)
    photo = img.group(1) if img and "/owners_photos/" in img.group(1) else None  # not the placeholder
    if photo:
        # "small" is 360×277; "large" is the same photo big enough for a card.
        photo = (BASE + photo if photo.startswith("/") else photo).replace("/owners_photos/small/", "/owners_photos/large/")
    short = _TITLE.search(popup)
    title = _text(full_title) or (_text(short.group(1)) if short else "") or "Untitled"
    return Listing(
        source="mmh",
        id=listing_id,
        title=title[:1].upper() + title[1:],
        url=f"{BASE}/sitters/assignment/{listing_id}",
        continent=continent,
        country=slugify(country),
        place=", ".join(place),
        photo_url=photo,
        pets=tuple(pets),
        slots=tuple(slots),
        country_code=code,
        lat=point[0] if point else None,
        lon=point[1] if point else None,
    )


def parse_page(html: str) -> tuple[list[Listing], int]:
    """All listings from the map script of a search page, and how many the
    page says it found. Raises SourceError if the page no longer looks like one."""
    found = _FOUND.search(html)
    markers = _MARKER.findall(html)
    if found is None:
        raise SourceError(f"MindMyHouse page changed: no results summary ({len(markers)} markers)")
    total = int(found.group(1))
    points = {n: (float(lat), float(lon)) for n, lat, lon in _POINT.findall(html)}
    listings: dict[str, Listing] = {}
    for title, n, raw in markers:
        popup = _js(raw)
        try:
            listing = parse_popup(popup, _js(title), points.get(n))
        except (AttributeError, IndexError, KeyError, ValueError) as exc:
            log.warning("MindMyHouse: could not parse a marker: %r %s", exc, popup[:200])
            continue
        listings.setdefault(listing.id, listing)
    # Most markers unparsed means the format changed, not a few odd listings:
    # alerts would quietly stop, so fail and let the admin know.
    if total and len(listings) * 2 < max(total, len(markers)):
        raise SourceError(
            f"MindMyHouse map changed: found {total}, parsed {len(listings)} of {len(markers)} markers"
        )
    return list(listings.values()), total


class MindMyHouse:
    key = "mmh"
    title = "MindMyHouse"
    # search() always returns every match, whatever max_pages: each tick is a
    # full crawl, so the worker needn't schedule extra ones.
    always_complete = True

    def __init__(
        self,
        http: Http,
        *,
        notice: Notice | None = None,
        clock: Callable[[], date] = date.today,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.http = http
        self._notice = notice
        self._clock = clock
        self._monotonic = monotonic
        self._lock = asyncio.Lock()  # the worker and the dialog share one fetch
        self._cache: tuple[float, list[Listing]] | None = None
        self._failure: tuple[float, Exception] | None = None
        self._told: set[str] = set()  # problems already reported to the admin

    async def _tell_once(self, key: str, text: str) -> None:
        log.warning(text)
        if key in self._told or not self._notice:
            return
        self._told.add(key)
        try:
            await self._notice(text)
        except Exception:
            # A failed notification must never fail the request it reports on.
            log.exception("MindMyHouse: could not deliver the notice")

    async def _all(self) -> list[Listing]:
        async with self._lock:
            now = self._monotonic()
            if self._cache and now - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            if self._failure and now - self._failure[0] < RETRY_SECONDS:
                raise SourceError(f"MindMyHouse failed {now - self._failure[0]:.0f} s ago: {self._failure[1]}")
            try:
                listings = await self._fetch()
            except Exception as exc:
                self._failure = (now, exc)
                raise
            self._failure = None
            self._cache = (now, listings)
            return listings

    async def _fetch(self) -> list[Listing]:
        resp = await self.http.get(
            SEARCH_PAGE, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, proxy_mode=PROXY_MODE
        )
        if resp.status_code != 200:
            raise SourceError(f"MindMyHouse HTTP {resp.status_code}: {resp.text[:200]}")
        listings, total = parse_page(resp.text)
        if len(listings) < total:
            # E.g. listings without coordinates, if the map ever skips them.
            await self._tell_once(
                f"gap:{total - len(listings)}",
                f"MindMyHouse: the page found {total} assignments, the map has {len(listings)} — "
                "the missing ones are not watched.",
            )
        for name in sorted({l.place.rsplit(", ", 1)[-1] for l in listings if not l.continent}):
            await self._tell_once(
                f"country:{name}",
                f"MindMyHouse: unknown country {name!r}: its listings match no continent. Add it to "
                "COUNTRIES in sources/mmh.py and deploy with `rebaseline mmh` between stop and start "
                "(README, \"Re-indexing a site\"), so its current listings aren't alerted as new.",
            )
        for name in sorted({p for l in listings for p, _ in l.pets} - KNOWN_PETS):
            await self._tell_once(
                f"pet:{name}",
                f"MindMyHouse: unknown animal {name!r} — map it in PETS in sources/mmh.py.",
            )
        if len(listings) >= 20 and not any(l.pets for l in listings):
            await self._tell_once("pets", "MindMyHouse: no listing has pets — the pets markup changed?")
        if len(listings) >= 20 and not any(l.lat is not None for l in listings):
            await self._tell_once(
                "points", "MindMyHouse: no marker has coordinates — the map script changed? /search around a "
                "town finds nothing here until fixed.",
            )
        return listings

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        """The whole site is one request, so `max_pages` doesn't matter; the
        filter is applied here, as an API would."""
        today = self._clock()
        return [l for l in await self._all() if listing_matches(l, flt) and matching_slots(l, flt, today)]

    async def by_start(self, flt: Filter) -> AsyncIterator[list[Listing]]:
        """The whole site is one page: the continent's listings (or those in
        the filter's circle) with an open slot from `flt.date_from` on (at
        least `flt.min_days` long), nearest first. The country is left to the
        caller (it matches on ISO codes)."""
        today = self._clock()
        since = max(today, flt.date_from or today)
        keyed = [(first_open_start(l, since, flt.min_days), l) for l in await self._all() if in_area(l, flt)]
        yield [l for k, l in sorted((kl for kl in keyed if kl[0]), key=lambda kl: (kl[0], kl[1].id))]

    async def countries(self, continent: str) -> list[Country]:
        """All of the site's countries of the continent, even those without a
        sit right now: listings are few here, and a subscription is for later.
        Regions ("Caribbean", "Middle East") are left out: no other site has
        them, and the continent covers them."""
        today = self._clock()
        counts = Counter(
            l.country for l in await self._all()
            if l.continent == continent and any(s.start >= today for s in l.slots)
        )
        countries = [
            Country(slugify(name), name, counts[slugify(name)])
            for name, (cont, code) in COUNTRIES.items()
            if cont == continent and code
        ]
        return sorted(countries, key=lambda c: (-c.count, c.name))
