"""Nomador: the house-sitting search, read through its map and result pages.

The site has no public API. What it serves anonymously (verified 2026-10-21):

* the map, `GET /api/search/geolocation?<filters>`: JSON, one marker per stay
  (`id`, `stayType`, `lat`, `lng`), every stay of the search in one request
  (~820 house-sits, ~26 KB gzipped), but nothing else about them;
* result pages, `GET /house-sitting/?<filters>&page=N` with X-Requested-With:
  the results only, 10 stay cards a page with the place, dates, pets and a
  photo. The open stays come first, then every stay that has found its sitters
  or has ended (tens of thousands). A page past 50 repeats page 50, so one
  search reaches 500 stays at most;
* a stay's own page, `/house-sitting/<its id in base58>`: its card again
  among others, and a larger photo.

A stay is one date range of a home, with a page of its own: here a Listing
with a single Slot, both known by the stay's id. "Stopovers" (a night for a
traveller) are not house-sits and are left out.

So the source keeps an index of every open stay. A crawl of the result pages
builds it, split by stay length (BANDS, one of the site's filters) into
searches well under 500 each: ~85 pages, every `refresh` (FULL_CRAWL_HOURS).
In between, each fetch (about one a tick) reads the map: stays gone from it are
dropped, and each stay new on it (~40 a day) is read from its own page. The map
is the authority: a stay on it that the crawl didn't reach is read the same way.

As on MindMyHouse the cards have no publication times or applicant counts, so
every slot unseen since the baseline is news (see models.appeared_after). The
ids of newer stays are time-based UUIDs, but the time is the stay's creation,
not its publication (one created on 15 Oct was published on 21 Oct), so they
can't serve as a watermark. A change on our side that makes old stays
matchable for the first time (a country name added to geo.py) needs
`rebaseline nmd` (see README).

Cloudflare guards the site and challenges clients by their TLS handshake,
whatever the IP: Python 3.12 with OpenSSL 3.0 (Ubuntu 24.04's) got a 403, a
newer OpenSSL got through. So every request goes as a browser's (Http's
`browser`, curl_cffi as Chrome). A blocked request is a SourceError that says so.

Debug: `python -m petalert.sources --source nmd search --continent europe`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import replace
from datetime import date, timedelta
from html import unescape

import httpx

from .. import geo
from ..http import Http
from ..models import Country, Filter, Listing, Slot, first_open_start, in_area, listing_matches, matching_slots
from .base import Notice, SourceError

log = logging.getLogger(__name__)

BASE = "https://www.nomador.com"
SEARCH = f"{BASE}/house-sitting/"
MAP = f"{BASE}/api/search/geolocation"
# Direct first, like MindMyHouse: if Cloudflare ever challenges the server's
# IP as well, Http retries the 403 through the proxy.
PROXY_MODE = "fallback"
HOUSE_SITS = {"fixedDates": "1", "flexibleDates": "1"}  # both kinds of house-sit, no stopovers
# Stay lengths in days (the site's minDuration/maxDuration) that split the
# crawl into searches under the 500 cap: 268, 216 and 326 open stays on
# 2026-10-21. A band that reaches the cap is reported to the admin.
BANDS: tuple[tuple[int | None, int | None], ...] = ((None, 7), (8, 14), (15, None))
PER_PAGE = 10
LAST_PAGE = 50  # any later page repeats this one
MAX_STAY_PAGES = 30  # stays read from their own pages per fetch; the rest wait for the next
CACHE_SECONDS = 300  # one fetch serves all filters of a tick and the /new dialog
RETRY_SECONDS = 120  # after a failure: the rest of the tick fails fast instead of refetching

# The site's animal names (singular and plural) → models.PETS. Only cats, dogs
# and horses were on the cards on 2026-10-21; the rest follow the site's pet
# filter. An unknown one is reported to the admin.
PETS = {
    "dog": "dog", "dogs": "dog", "cat": "cat", "cats": "cat", "horse": "horse", "horses": "horse",
    "bird": "bird", "birds": "bird", "fish": "fish", "fishes": "fish",
    "rodent": "small pet", "rodents": "small pet", "reptile": "reptile", "reptiles": "reptile",
    "farm animal": "farm animal", "farm animals": "farm animal",
    "exotic animal": "other animal", "exotic animals": "other animal",  # no filter for it; shown as 🐾
}
KNOWN_PETS = set(PETS.values())

MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_MON = "|".join(MONTHS)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# A card starts with its div: `<div class="c-housing … [occupied]" data-lat=…`.
_CARD = re.compile(r'<div\s+class="(c-housing\b[^"]*)"\s+data-lat=')
_ATTR = re.compile(r'\bdata-(stayid|title|dates|link|picture|lat|lng)="([^"]*)"')
_LOCATION = re.compile(r'<p class="c-housing__description">(.*?)</p>', re.S)
_AVAILABILITY = re.compile(r'<div class="c-housing__availability">(.*?)</div>', re.S)
_SPEC = re.compile(r'<p class="c-housing__spec">(.*?)</p>', re.S)
# "From the 7 to the 12 Nov 26", "From the 18 Dec to the 3 Jan 27": the start's
# month and year are left out when they are the end's.
_DATES = re.compile(rf"From the (\d{{1,2}})(?: ({_MON}))?(?: (\d{{2}}))? to the (\d{{1,2}}) ({_MON}) (\d{{2}})")
_PET = re.compile(r"(\d+) (.+)")
_OG_IMAGE = re.compile(r'<meta property="og:image" content="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", html)).split())


def stay_url(stay_id: str) -> str:
    """A stay's page: its id (a UUID) in base58, 22 characters
    (`01a15ddc-1049-…` → `/house-sitting/1CgFAt1zPtNezQbBKyURBU`)."""
    n = int(stay_id.replace("-", ""), 16)
    digits = ""
    while n:
        n, r = divmod(n, 58)
        digits = _B58[r] + digits
    return SEARCH + digits.rjust(22, _B58[0])


def parse_dates(text: str) -> tuple[date, date] | None:
    """`From the 18 Dec to the 3 Jan 27` → (2026-12-18, 2027-01-03); None for
    a stay without dates. Anything else is a ValueError."""
    if not text:
        return None
    m = _DATES.fullmatch(text)
    if m is None:
        raise ValueError(f"unrecognised dates: {text[:100]}")
    d1, m1, y1, d2, m2, y2 = m.groups()
    end = date(2000 + int(y2), MONTHS[m2], int(d2))
    month = MONTHS[m1 or m2]
    if y1:
        year = 2000 + int(y1)
    else:
        year = end.year if (month, int(d1)) <= (end.month, end.day) else end.year - 1
    return date(year, month, int(d1)), end


def _coordinate(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def parse_card(classes: str, card: str) -> Listing:
    """One stay card → Listing. The id, the place and well-formed dates are
    required (KeyError/AttributeError/ValueError); a stay without dates (a few
    flexible ones) gets no slot, so it matches nothing."""
    attrs: dict[str, str] = {}
    for name, value in _ATTR.findall(card):
        attrs.setdefault(name, unescape(value))  # the card's own; buttons inside repeat the id
    stay_id = attrs["stayid"]
    availability = _AVAILABILITY.search(card)
    location = _LOCATION.search(card, 0, availability.start() if availability else len(card))
    parts = [p.strip() for p in _text(location.group(1)).split(",") if p.strip()]  # type: ignore[union-attr]
    country = parts[-1]  # "Andalusia, Spain", "Grande-Terre, Guadeloupe", "Malta"
    found = geo.lookup_place(country)
    code = found[1] if found and found[0] == "country" else None
    city = attrs["title"].strip()
    place: list[str] = []
    for part in (city, *parts[:-1]):
        if part and part not in place and part != country:
            place.append(part)
    place.append(country)  # always last: the table and countries() rely on it
    dates = parse_dates(attrs["dates"].strip())
    slots = ()
    if dates:
        # Taken stays (sitters found, offer ended) follow the open ones on a page.
        slots = (Slot(stay_id, *dates, is_open="occupied" not in classes.split()),)
    pets: dict[str, int | None] = {}
    for spec in _SPEC.findall(card):  # "4 Cats - 2 Dogs - 3 Horses"
        for item in _text(spec).split(" - "):
            m = _PET.fullmatch(item)
            label, count = (m.group(2), int(m.group(1))) if m else (item, None)
            name = PETS.get(label.lower(), label.lower())
            if count is not None and pets.get(name) is not None:
                count += pets[name]  # (rodents and the like are one kind here)
            pets[name] = count
    flexible = availability is not None and "Flexible dates" in _text(availability.group(1))
    return Listing(
        source="nmd",
        id=stay_id,
        # The site gives no title, only the town; its own pages say "House-sitting in …".
        title=f"House-sitting in {city or country}" + (" · flexible dates" if flexible else ""),
        url=BASE + attrs["link"],
        continent=geo.continent(code) if code else "",
        country=geo.slug(code) if code else geo.key(country),
        place=", ".join(place),
        photo_url=attrs.get("picture") or None,
        pets=tuple(pets.items()),
        slots=slots,
        country_code=code,
        lat=_coordinate(attrs.get("lat")),
        lon=_coordinate(attrs.get("lng")),
    )


def parse_page(html: str) -> tuple[list[Listing], int]:
    """The stay cards of a result page (or of a stay's page) in order, and
    how many cards there were. Raises SourceError if most of them no longer
    parse: the markup has changed, and alerts would quietly stop."""
    starts = list(_CARD.finditer(html))
    listings = []
    for k, m in enumerate(starts):
        card = html[m.start() : starts[k + 1].start() if k + 1 < len(starts) else len(html)]
        try:
            listings.append(parse_card(m.group(1), card))
        except (AttributeError, IndexError, KeyError, ValueError) as exc:
            log.warning("Nomador: could not parse a card: %r %s", exc, _text(card)[:200])
    if len(listings) * 2 < len(starts):
        raise SourceError(f"Nomador cards changed: parsed {len(listings)} of {len(starts)}")
    return listings, len(starts)


def _taken(listing: Listing) -> bool:
    return any(not s.is_open for s in listing.slots)


def _created(stay_id: str) -> int:
    """When a stay was created, in ms, from a time-based (v7) UUID; 0 for an
    older (v4) one. Orders the index newest first, as search() promises."""
    try:
        return int(stay_id[:8] + stay_id[9:13], 16) if stay_id[14] == "7" else 0
    except (IndexError, ValueError):
        return 0


def _band(low: int | None, high: int | None) -> str:
    return f"{low or 0}–{high} days" if high is not None else f"{low}+ days"


class Nomador:
    key = "nmd"
    title = "Nomador"
    # search() always returns every match, whatever max_pages: it is answered
    # from the index of the whole site, so the worker needn't schedule crawls.
    always_complete = True

    def __init__(
        self,
        http: Http,
        *,
        notice: Notice | None = None,
        refresh: timedelta = timedelta(hours=3),
        clock: Callable[[], date] = date.today,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.http = http
        self._notice = notice
        self._refresh_every = refresh
        self._clock = clock
        self._monotonic = monotonic
        self._lock = asyncio.Lock()  # the worker and the dialog share one fetch
        self._index: dict[str, Listing] | None = None  # every stay on the map, by id
        self._fetched = 0.0  # when the index was last brought up to date
        self._crawled = 0.0  # when the result pages were last crawled
        self._failure: tuple[float, Exception] | None = None
        self._cardless: set[str] = set()  # on the map, but their page has no card: tried again after a crawl
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
            log.exception("Nomador: could not deliver the notice")

    async def _get(self, url: str, params: dict[str, str] | None = None, *, xhr: bool = True) -> httpx.Response:
        headers = {"Accept-Language": "en"}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"  # the results alone, not the whole page
        resp = await self.http.get(url, params=params, headers=headers, proxy_mode=PROXY_MODE, browser=True)
        if resp.status_code != 200:
            if resp.headers.get("cf-mitigated") == "challenge":
                raise SourceError(f"Nomador HTTP {resp.status_code}: blocked by a Cloudflare challenge")
            raise SourceError(f"Nomador HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _all(self) -> list[Listing]:
        if self._lock.locked():
            # A refresh is under way (a crawl takes minutes): the last index
            # will do, and without one the dialog and /search shouldn't wait.
            if self._index is None:
                raise SourceError("Nomador is still indexing the site (a few minutes after a start)")
            return list(self._index.values())
        async with self._lock:
            now = self._monotonic()
            if self._index is not None and now - self._fetched < CACHE_SECONDS:
                return list(self._index.values())
            if self._failure and now - self._failure[0] < RETRY_SECONDS:
                raise SourceError(f"Nomador failed {now - self._failure[0]:.0f} s ago: {self._failure[1]}")
            try:
                self._index = await self._update(now)
            except Exception as exc:
                self._failure = (now, exc)
                raise
            self._failure = None
            self._fetched = now
            return list(self._index.values())

    async def _update(self, now: float) -> dict[str, Listing]:
        """The index brought up to date with the map: a crawl when it's due,
        then each stay new on the map read from its own page."""
        ids = await self._map()
        on_map = set(ids)
        old = self._index or {}
        if self._index is None or now - self._crawled >= self._refresh_every.total_seconds():
            index = {i: l for i, l in (await self._crawl()).items() if i in on_map}
            if len(index) * 2 < len(on_map):
                raise SourceError(f"Nomador crawl found {len(index)} of the {len(on_map)} stays on the map")
            # Map stays the crawl doesn't reach (taken ones the map still shows)
            # keep what their own page said.
            index.update({i: l for i, l in old.items() if i in on_map and i not in index})
            self._crawled = now
            self._cardless.clear()
        else:
            index = {i: l for i, l in old.items() if i in on_map}
        unknown = [i for i in ids if i not in index and i not in self._cardless]
        for stay_id in unknown[:MAX_STAY_PAGES]:
            try:
                listing = await self._stay(stay_id)
            except (SourceError, httpx.TransportError) as exc:
                # The index is still good without it: the next fetch tries again.
                log.warning("Nomador: stay %s: %r", stay_id, exc)
                break
            if listing is None:
                self._cardless.add(stay_id)
            else:
                index[stay_id] = listing
        if len(unknown) > MAX_STAY_PAGES:
            log.info("Nomador: %d new stays on the map, the rest wait for the next fetch", len(unknown))
        if len(self._cardless) >= 5:
            await self._tell_once(
                "cardless",
                f"Nomador: {len(self._cardless)} stays on the map have no card on their own pages — "
                "the stay page changed? Until fixed, new stays arrive only with the crawls.",
            )
        await self._check(index.values())
        return dict(sorted(index.items(), key=lambda kv: _created(kv[0]), reverse=True))

    async def _map(self) -> list[str]:
        resp = await self._get(MAP, HOUSE_SITS)
        try:
            ids = [m["id"] for m in resp.json() if m["stayType"] == "house-sitting"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Nomador map changed: {resp.text[:200]}") from exc
        if not ids:
            raise SourceError("Nomador map has no house-sits")  # ~820 on 2026-10-21
        return ids

    async def _crawl(self) -> dict[str, Listing]:
        """Every open stay from the result pages, band by band. Each band's
        pages are read until its open stays end (a taken one, or a short page)."""
        found: dict[str, Listing] = {}
        for low, high in BANDS:
            params = dict(HOUSE_SITS)
            if low is not None:
                params["minDuration"] = str(low)
            if high is not None:
                params["maxDuration"] = str(high)
            for page in range(1, LAST_PAGE + 1):
                listings, cards = parse_page((await self._get(SEARCH, {**params, "page": str(page)})).text)
                for listing in listings:
                    found.setdefault(listing.id, listing)
                if cards < PER_PAGE or any(_taken(l) for l in listings):
                    break
            else:
                await self._tell_once(
                    f"cap:{low}-{high}",
                    f"Nomador: open stays of {_band(low, high)} fill all {LAST_PAGE} result pages, so the crawl "
                    "can't reach some: they are read from their own pages instead, slowly. "
                    "Split BANDS in sources/nmd.py.",
                )
        return found

    async def _stay(self, stay_id: str) -> Listing | None:
        """A stay from its own page; None if the page has no card of it (a
        stay that doesn't exist gets a page too)."""
        resp = await self._get(stay_url(stay_id), xhr=False)
        listings, _ = parse_page(resp.text)
        listing = next((l for l in listings if l.id == stay_id), None)
        if listing is None:
            log.warning("Nomador: no card of stay %s on its page %s", stay_id, resp.url)
            return None
        image = _OG_IMAGE.search(resp.text)  # 750×520, the card's is 370×278
        return replace(listing, photo_url=unescape(image.group(1))) if image else listing

    async def _check(self, listings: Iterable[Listing]) -> None:
        listings = list(listings)
        for name in sorted({l.place.rsplit(", ", 1)[-1] for l in listings if not l.continent}):
            await self._tell_once(
                f"country:{name}",
                f"Nomador: unknown country {name!r}: its stays match no continent. Add it to geo.py "
                "(or its ALIASES) and deploy with `rebaseline nmd` between stop and start (README, "
                "\"Re-indexing a site\"), so its current stays aren't alerted as new.",
            )
        for name in sorted({p for l in listings for p, _ in l.pets} - KNOWN_PETS):
            await self._tell_once(f"pet:{name}", f"Nomador: unknown animal {name!r} — map it in PETS in sources/nmd.py.")
        if len(listings) >= 20 and not any(l.pets for l in listings):
            await self._tell_once("pets", "Nomador: no stay has pets — the pets markup changed?")
        if len(listings) >= 20 and not any(l.lat is not None for l in listings):
            await self._tell_once(
                "points", "Nomador: no stay card has coordinates — the cards changed? /search around a town "
                "finds nothing here until fixed.",
            )

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        """Answered from the index of the whole site, so `max_pages` doesn't
        matter; the filter is applied here, as an API would."""
        today = self._clock()
        return [l for l in await self._all() if listing_matches(l, flt) and matching_slots(l, flt, today)]

    async def by_start(self, flt: Filter) -> AsyncIterator[list[Listing]]:
        """The whole site is one page: the continent's stays (or those in the
        filter's circle) with an open slot from `flt.date_from` on (at least
        `flt.min_days` long), nearest first. The country is left to the
        caller (it matches on ISO codes)."""
        today = self._clock()
        since = max(today, flt.date_from or today)
        keyed = [(first_open_start(l, since, flt.min_days), l) for l in await self._all() if in_area(l, flt)]
        yield [l for k, l in sorted((kl for kl in keyed if kl[0]), key=lambda kl: (kl[0], kl[1].id))]

    async def countries(self, continent: str) -> list[Country]:
        """Countries of the continent with open stays from today on, largest first."""
        today = self._clock()
        counts = Counter(
            l.country_code for l in await self._all()
            if l.continent == continent and l.country_code and any(s.is_open and s.start >= today for s in l.slots)
        )
        countries = [Country(geo.slug(code), geo.name(code), n) for code, n in counts.items()]
        return sorted(countries, key=lambda c: (-c.count, c.name))
