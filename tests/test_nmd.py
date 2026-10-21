"""Nomador: parsing stay cards, the crawl by stay length, keeping the index
up to date from the map, local filtering and errors over mocked HTTP. The
live site is in test_live_nmd.py."""

from __future__ import annotations

import asyncio
import pathlib
import re
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pytest

from petalert.http import Http
from petalert.models import Filter, first_open_start
from petalert.sources import nmd
from petalert.sources.base import SourceError
from petalert.sources.nmd import (
    BASE,
    CACHE_SECONDS,
    PER_PAGE,
    RETRY_SECONDS,
    Nomador,
    parse_dates,
    parse_page,
    stay_url,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "nmd_page.html").read_text()  # real cards: 8 open (one without dates), 2 taken
STAY = (FIXTURES / "nmd_stay.html").read_text()  # Dos Hermanas' own page, trimmed
TODAY = date(2026, 10, 21)
DOS_HERMANAS = "01a15ddc-1049-74c7-8586-2bcd91ef3137"
BASTELICACCIA = "01a0ebf1-35f5-7b6d-83f2-bda98fb3a429"
_STARTS = re.compile(r'<div\s+class="c-housing\b')


def cards_of(html: str) -> list[str]:
    starts = [m.start() for m in _STARTS.finditer(html)] + [len(html)]
    return [html[a:b] for a, b in zip(starts, starts[1:])]


def by_id(listings, listing_id):
    return next(l for l in listings if l.id == listing_id)


# --- parsing ---------------------------------------------------------------


def test_every_card_parses():
    listings, cards = parse_page(PAGE)
    assert cards == len(listings) == 10
    assert all(l.source == "nmd" and l.continent and l.country_code for l in listings)
    assert [all(s.is_open for s in l.slots) for l in listings] == [True] * 8 + [False] * 2


def test_fields():
    listings, _ = parse_page(PAGE)
    stay = by_id(listings, DOS_HERMANAS)
    assert stay.title == "House-sitting in Dos Hermanas · flexible dates"
    assert stay.url == "https://www.nomador.com/house-sitting/1CgFAt1zPtNezQbBKyURBU"
    assert (stay.continent, stay.country, stay.country_code) == ("europe", "spain", "es")
    assert stay.place == "Dos Hermanas, Andalusia, Spain"
    assert stay.photo_url.startswith("https://media.nomador.com/") and stay.photo_url.endswith(".jpg")
    assert stay.pets == (("dog", 2),)
    assert stay.published is None
    (slot,) = stay.slots
    assert (slot.id, slot.start, slot.end, slot.days, slot.is_open, slot.seq) == (
        DOS_HERMANAS, date(2026, 12, 18), date(2027, 1, 3), 16, True, None
    )


def test_edge_cases():
    listings, _ = parse_page(PAGE)
    places = {l.title.removeprefix("House-sitting in ").split(" · ")[0]: l for l in listings}
    corsica = places["Bastelicaccia"]  # "From the 7 to the 12 Nov 26", fixed dates
    assert corsica.title == "House-sitting in Bastelicaccia"
    assert (corsica.slots[0].start, corsica.slots[0].end) == (date(2026, 11, 7), date(2026, 11, 12))
    assert corsica.pets == (("cat", 2), ("dog", 2))
    assert places["Maastricht"].pets == (("dog", 2), ("horse", 11))
    assert places["Xagħra"].place == "Xagħra, Malta"  # no region
    gosier = places["Le Gosier"]  # "Grande-Terre, Guadeloupe": the Caribbean, not France
    assert (gosier.continent, gosier.country, gosier.country_code) == ("north-america", "guadeloupe", "gp")
    assert (places["Saint Pierre"].continent, places["Saint Pierre"].country_code) == ("africa", "re")
    assert "Val d'Oingt" in places  # &#039; in the card
    undated = places["Chabanière"]  # a flexible stay without dates: nothing to match
    assert (undated.slots, undated.pets) == ((), ())
    assert [l.slots[0].is_open for l in (places["Rotterdam"], places["Meudon"])] == [False, False]


@pytest.mark.parametrize(
    "text, dates",
    [
        ("From the 7 to the 12 Nov 26", (date(2026, 11, 7), date(2026, 11, 12))),
        ("From the 18 Dec to the 3 Jan 27", (date(2026, 12, 18), date(2027, 1, 3))),
        ("From the 5 Aug to the 6 Oct 27", (date(2027, 8, 5), date(2027, 10, 6))),
        ("From the 1 Nov 26 to the 3 Jan 28", (date(2026, 11, 1), date(2028, 1, 3))),
        ("", None),
    ],
)
def test_dates(text, dates):
    assert parse_dates(text) == dates


@pytest.mark.parametrize(
    "text", ["Hosting a traveller for a night", "From the 7 to the 12 Foo 26", "From the 30 Feb to the 3 Mar 27"]
)
def test_other_dates_are_errors(text):
    with pytest.raises(ValueError):
        parse_dates(text)


def test_stay_url_is_the_id_in_base58():
    assert stay_url(DOS_HERMANAS) == f"{BASE}/house-sitting/1CgFAt1zPtNezQbBKyURBU"
    assert stay_url("f0886109-c6ac-4cac-88b3-01828f9e6fa9") == f"{BASE}/house-sitting/WhiYzm3ERC4Ev4zDkATmse"


def test_changed_markup_fails_loudly_instead_of_emptying_the_site():
    with pytest.raises(SourceError, match="parsed 1 of 10"):  # only the stay without dates
        parse_page(PAGE.replace("From the ", "Du "))


# --- the source over a mocked site ----------------------------------------

_TEMPLATE = next(c for c in cards_of(PAGE) if BASTELICACCIA in c)


def uid(n: int) -> str:
    """A time-based (v7) stay id; a larger n is a newer stay."""
    return f"01a1{n:04x}-0000-7000-8000-{n:012x}"


def site_dates(start: date, end: date) -> str:
    return f"From the {start.day} {start:%b} to the {end.day} {end:%b %y}"


def card(stay_id: str, days: int, *, taken=False, where="Corsica, France", pets="2 Cats - 2 Dogs") -> str:
    """A real card (Bastelicaccia's) with another stay's id, dates and place."""
    start = date(2026, 12, 1)
    html = (
        _TEMPLATE.replace(BASTELICACCIA, stay_id)
        .replace(stay_url(BASTELICACCIA).removeprefix(BASE), stay_url(stay_id).removeprefix(BASE))
        .replace("From the 7 to the 12 Nov 26", site_dates(start, start + timedelta(days=days)))
        .replace("Corsica, France", where)
    )
    html = re.sub(r'(<p class="c-housing__spec">).*?(</p>)', rf"\g<1>{pets}\g<2>", html, flags=re.S)
    return html.replace('js-housing"', 'js-housing occupied"', 1) if taken else html


@dataclass
class Stay:
    id: str
    days: int | None  # None: no dates (the site's length filters never show it)
    taken: bool = False
    html: str = ""

    def __post_init__(self):
        self.html = self.html or card(self.id, self.days or 0, taken=self.taken)


class Site:
    """Nomador as the source sees it: the map; result pages filtered by
    length, the open stays first, 10 a page; a stay's own page."""

    def __init__(self, stays: list[Stay], on_map: list[str] | None = None):
        self.stays = {s.id: s for s in stays}
        self.on_map = on_map if on_map is not None else [s.id for s in stays if not s.taken]
        self.pages: dict[str, str] = {}  # a stay's own page, if not just its card

    def add(self, stay: Stay) -> None:
        self.stays[stay.id] = stay
        self.on_map.append(stay.id)

    def respond(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.params
        if path == "/api/search/geolocation":
            assert dict(query) == {"fixedDates": "1", "flexibleDates": "1"}
            return httpx.Response(200, json=[
                {"id": i, "stayType": "house-sitting", "lat": 45.7, "lng": 4.8} for i in self.on_map
            ])
        if path == "/house-sitting/":
            assert request.headers["X-Requested-With"] == "XMLHttpRequest"
            low, high = int(query.get("minDuration", 0)), int(query.get("maxDuration", 9999))
            band = [s for s in self.stays.values() if s.days is not None and low <= s.days <= high]
            band.sort(key=lambda s: s.taken)
            page = int(query["page"])
            items = band[(page - 1) * PER_PAGE : page * PER_PAGE]
            return httpx.Response(200, text='<div class="v-panel__items">' + "".join(s.html for s in items) + "</div>")
        stay = next((s for s in self.stays.values() if stay_url(s.id).endswith(path)), None)
        if stay is None:
            return httpx.Response(200, text="<html>Oops, this stay doesn't exist</html>")  # as the site does
        return httpx.Response(200, text=self.pages.get(stay.id, f"<html>{stay.html}</html>"))


def kind(request: httpx.Request) -> str:
    if request.url.path == "/api/search/geolocation":
        return "map"
    if request.url.path == "/house-sitting/":
        q = request.url.params
        return f"list {q.get('minDuration', '')}-{q.get('maxDuration', '')} p{q['page']}"
    return "stay"


class Fake:
    def __init__(self, site: Site | None = None, handler=None, *, refresh=timedelta(hours=3)):
        self.site = site
        self.requests: list[httpx.Request] = []
        self.notices: list[str] = []
        self.now = 1000.0

        async def record(request):
            self.requests.append(request)
            response = (handler or site.respond)(request)
            return await response if asyncio.iscoroutine(response) else response

        async def nosleep(_):
            return None

        def plain(proxy):
            raise AssertionError("Nomador requests must go as a browser's")

        self.http = Http(
            min_interval=0,
            client_factory=plain,
            browser_factory=lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(record)),
            sleep=nosleep,
        )

        async def notice(text):
            self.notices.append(text)

        self.nmd = Nomador(
            self.http, notice=notice, refresh=refresh, clock=lambda: TODAY, monotonic=lambda: self.now
        )

    def kinds(self) -> list[str]:
        kinds = [kind(r) for r in self.requests]
        self.requests.clear()
        return kinds

    def run(self, coro_factory):
        async def go():
            try:
                return await coro_factory()
            finally:
                await self.http.aclose()

        return asyncio.run(go())


def everything(fake: Fake):
    return fake.nmd.search(Filter("europe"), max_pages=1)


def test_first_fetch_crawls_each_length_until_its_open_stays_end():
    site = Site(
        [Stay(uid(i), 3) for i in range(20)] + [Stay(uid(100 + i), 3, taken=True) for i in range(15)]
        + [Stay(uid(200 + i), 10) for i in range(12)]
        + [Stay(uid(300 + i), 20) for i in range(4)] + [Stay(uid(400), 20, taken=True)]
    )
    fake = Fake(site)
    listings = fake.run(lambda: everything(fake))
    assert fake.kinds() == [
        "map",
        "list -7 p1", "list -7 p2", "list -7 p3",  # p3 is all taken: the open ones are over
        "list 8-14 p1", "list 8-14 p2",  # a short page
        "list 15- p1",
    ]
    assert len(listings) == 36 and all(l.slots[0].is_open for l in listings)
    assert [l.id for l in listings] == sorted((l.id for l in listings), reverse=True)  # newest first


def test_between_crawls_the_map_tells_what_is_new_and_what_is_gone():
    site = Site([Stay(uid(i), 3) for i in range(5)])
    fake = Fake(site)

    async def go():
        await everything(fake)
        fake.kinds()
        await everything(fake)  # within the cache
        cached = fake.kinds()
        fake.now += CACHE_SECONDS + 1
        site.add(Stay(uid(50), 9))
        site.on_map.remove(uid(0))
        listings = await everything(fake)
        update = fake.kinds()
        fake.now += timedelta(hours=3).total_seconds()
        await everything(fake)
        return cached, update, listings, fake.kinds()

    cached, update, listings, later = fake.run(go)
    assert cached == []
    assert update == ["map", "stay"]  # the new stay's own page, no result pages
    assert [l.id for l in listings] == [uid(50), uid(4), uid(3), uid(2), uid(1)]  # uid(0) left the map
    assert later == ["map", "list -7 p1", "list 8-14 p1", "list 15- p1"]  # the crawl is due again


def test_a_new_stay_comes_with_the_large_photo_of_its_page():
    site = Site([Stay(uid(i), 3) for i in range(3)])
    fake = Fake(site)

    async def go():
        await everything(fake)
        fake.now += CACHE_SECONDS + 1
        site.add(Stay(DOS_HERMANAS, 16, html=next(c for c in cards_of(PAGE) if DOS_HERMANAS in c)))
        site.pages[DOS_HERMANAS] = STAY  # its own card among two others
        return await everything(fake)

    stay = by_id(fake.run(go), DOS_HERMANAS)
    assert stay.photo_url.startswith("https://media.nomador.com/") and "/rs:fill:750:520:1/" in stay.photo_url
    assert stay.place == "Dos Hermanas, Andalusia, Spain"
    assert len(fake.run(lambda: everything(fake))) == 4  # not Borås or Bastelicaccia from that page


def test_stays_without_a_card_are_not_read_again_until_the_next_crawl():
    site = Site([Stay(uid(i), 3) for i in range(5)], on_map=[uid(i) for i in range(5)] + [uid(99)])
    fake = Fake(site)

    async def go():
        await everything(fake)
        first = fake.kinds()
        fake.now += CACHE_SECONDS + 1
        await everything(fake)
        second = fake.kinds()
        fake.now += timedelta(hours=3).total_seconds()
        await everything(fake)
        return first, second, fake.kinds()

    first, second, third = fake.run(go)
    assert first[-1] == "stay" and second == ["map"] and third[-1] == "stay"


def test_taken_stays_still_on_the_map_keep_what_their_page_said():
    site = Site(
        [Stay(uid(i), 3) for i in range(12)] + [Stay(uid(90), 3, taken=True)]
        + [Stay(uid(80 + i), 3, taken=True) for i in range(8)] + [Stay(uid(91), 3, taken=True)]
    )
    # The map shows some taken stays too: one the crawl reads on page 2, where
    # the taken ones begin, and one on page 3, which it doesn't read.
    site.on_map += [uid(90), uid(91)]
    fake = Fake(site)

    async def go():
        listings = await everything(fake)
        first = fake.kinds()
        fake.now += timedelta(hours=3).total_seconds()
        await everything(fake)
        return listings, first, fake.kinds()

    listings, first, recrawl = fake.run(go)
    assert first == ["map", "list -7 p1", "list -7 p2", "list 8-14 p1", "list 15- p1", "stay"]
    assert {uid(90), uid(91)}.isdisjoint(l.id for l in listings)  # taken: never a match
    assert "stay" not in recrawl  # uid(91) carried over, not read again


def test_a_length_band_at_the_page_cap_is_told_and_the_rest_read_one_by_one(monkeypatch):
    monkeypatch.setattr(nmd, "LAST_PAGE", 2)
    site = Site([Stay(uid(i), 3) for i in range(25)])
    fake = Fake(site)
    listings = fake.run(lambda: everything(fake))
    kinds = fake.kinds()
    assert len(listings) == 25
    assert kinds.count("list -7 p1") == kinds.count("list -7 p2") == 1 and kinds.count("stay") == 5
    assert len(fake.notices) == 1 and "0–7 days fill all 2 result pages" in fake.notices[0]


def test_the_real_page_answers_filters_countries_and_searches_by_start():
    stays = []
    for html in cards_of(PAGE):
        (listing,), _ = parse_page(html)
        slot = listing.slots[0] if listing.slots else None
        stays.append(Stay(listing.id, slot.days if slot else None, slot is not None and not slot.is_open, html))
    rotterdam = next(s.id for s in stays if "Rotterdam" in s.html)
    site = Site(stays, on_map=[s.id for s in stays if not s.taken] + [rotterdam])
    fake = Fake(site)

    async def go():
        horses = await fake.nmd.search(Filter("europe", country="france", pets=("horse",)), max_pages=1)
        europe = await fake.nmd.countries("europe")
        caribbean = await fake.nmd.countries("north-america")
        (page,) = [p async for p in fake.nmd.by_start(Filter("europe", country="spain", date_from=date(2026, 11, 1)))]
        return horses, europe, caribbean, page

    horses, europe, caribbean, page = fake.run(go)
    assert [l.place for l in horses] == ["Val d'Oingt, Auvergne-Rhône-Alpes, France"]
    assert [(c.slug, c.name, c.count) for c in europe] == [
        ("france", "France", 2), ("malta", "Malta", 1), ("netherlands", "Netherlands", 1), ("spain", "Spain", 1)
    ]  # not the stay without dates, nor Rotterdam (taken)
    assert [(c.slug, c.count) for c in caribbean] == [("guadeloupe", 1)]
    starts = [first_open_start(l, date(2026, 11, 1)) for l in page]
    assert starts == [date(2026, 11, 7), date(2026, 12, 18), date(2026, 12, 21), date(2026, 12, 23)]
    assert len({l.country for l in page}) > 1  # the country is left to the caller


def test_unknown_countries_and_animals_are_told_once():
    site = Site([Stay(uid(i), 3) for i in range(3)])
    site.stays[uid(1)].html = card(uid(1), 3, where="Lagos, Atlantis", pets="1 Alpaca - 2 Dogs")
    fake = Fake(site)

    async def go():
        first = await everything(fake)
        fake.now += CACHE_SECONDS + 1
        await everything(fake)
        return first

    assert uid(1) not in {l.id for l in fake.run(go)}  # no continent
    assert len(fake.notices) == 2
    assert any("unknown country 'Atlantis'" in n for n in fake.notices)
    assert any("unknown animal 'alpaca'" in n for n in fake.notices)


@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(503, text="maintenance"), "HTTP 503: maintenance"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<html>Just a moment…"), "Cloudflare"),
        (httpx.Response(200, text="<html>a new design</html>"), "map changed"),
        (httpx.Response(200, json=[]), "no house-sits"),
    ],
)
def test_a_broken_map_is_a_source_error(response, error):
    fake = Fake(handler=lambda request: response)
    with pytest.raises(SourceError, match=error):
        fake.run(lambda: everything(fake))


def test_a_crawl_that_misses_most_of_the_map_is_a_source_error():
    site = Site([Stay(uid(i), 3) for i in range(10)])
    for stay in site.stays.values():
        stay.html = stay.html.replace("c-housing__description", "c-card__where")  # cards no longer parse
    fake = Fake(site)
    with pytest.raises(SourceError, match="parsed 0 of 10"):
        fake.run(lambda: everything(fake))
    site = Site([Stay(uid(i), 3) for i in range(4)], on_map=[uid(i) for i in range(10)])
    fake = Fake(site)
    with pytest.raises(SourceError, match="crawl found 4 of the 10 stays on the map"):
        fake.run(lambda: everything(fake))


def test_a_failure_is_not_refetched_for_the_rest_of_the_tick():
    fake = Fake(handler=lambda request: httpx.Response(502, text="bad gateway"))

    async def go():
        errors = []
        for _ in range(3):
            try:
                await everything(fake)
            except SourceError as exc:
                errors.append(str(exc))
        one_fetch = len(fake.requests)  # Http's own retries of the 502
        fake.now += RETRY_SECONDS + 1
        with pytest.raises(SourceError):
            await everything(fake)
        return errors, one_fetch

    errors, one_fetch = fake.run(go)
    assert len(errors) == 3 and "HTTP 502" in errors[0] and "failed 0 s ago" in errors[1]
    assert len(fake.requests) == 2 * one_fetch


def test_a_failing_stay_page_keeps_the_index_and_is_tried_next_time():
    site = Site([Stay(uid(i), 3) for i in range(3)])
    broken = {"on": False}

    def respond(request):
        if broken["on"] and kind(request) == "stay":
            return httpx.Response(500, text="oops")
        return site.respond(request)

    fake = Fake(handler=respond)

    async def go():
        await everything(fake)
        fake.now += CACHE_SECONDS + 1
        site.add(Stay(uid(10), 3))
        broken["on"] = True
        during = await everything(fake)
        fake.now += CACHE_SECONDS + 1
        broken["on"] = False
        return during, await everything(fake)

    during, after = fake.run(go)
    assert len(during) == 3 and len(after) == 4


def test_while_a_crawl_runs_others_get_the_last_index():
    site = Site([Stay(uid(i), 3) for i in range(3)])
    gate = asyncio.Event()

    async def respond(request):
        if kind(request).startswith("list") and gate_on["on"]:
            await gate.wait()
        return site.respond(request)

    gate_on = {"on": False}
    fake = Fake(handler=respond)

    async def crawling():
        gate.clear()
        gate_on["on"] = True
        crawl = asyncio.create_task(everything(fake))
        while not fake.nmd._lock.locked():
            await asyncio.sleep(0)
        return crawl

    async def go():
        crawl = await crawling()  # the first one: nothing to show yet
        with pytest.raises(SourceError, match="still indexing"):
            await fake.nmd.countries("europe")
        gate.set()
        await crawl
        fake.now += timedelta(hours=3).total_seconds()  # the next fetch crawls
        crawl = await crawling()
        meanwhile = await fake.nmd.countries("europe")
        gate.set()
        return meanwhile, await crawl

    meanwhile, crawled = fake.run(go)
    assert [(c.slug, c.count) for c in meanwhile] == [("france", 3)] and len(crawled) == 3


def test_goes_direct_even_when_the_proxy_is_always_on():
    site = Site([Stay(uid(i), 3) for i in range(3)])
    used: list[bool] = []

    def factory(proxy):
        def handler(request):
            used.append(proxy is not None)
            return site.respond(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    http = Http("http://proxy:1", "always", min_interval=0, browser_factory=factory)
    source = Nomador(http, clock=lambda: TODAY)

    async def go():
        try:
            return await source.search(Filter("europe"), max_pages=1)
        finally:
            await http.aclose()

    assert asyncio.run(go()) and used and not any(used)
