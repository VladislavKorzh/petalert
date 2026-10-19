"""MindMyHouse: parsing the search page's map markers, local filtering,
caching and errors over mocked HTTP. The live site is in test_live_mmh.py."""

from __future__ import annotations

import asyncio
import pathlib
import re
from datetime import date

import httpx
import pytest

from petalert.http import Http
from petalert.models import PETS, Filter, first_open_start
from petalert.sources.base import SourceError
from petalert.sources.mmh import (
    CACHE_SECONDS,
    COUNTRIES,
    RETRY_SECONDS,
    SEARCH_PAGE,
    MindMyHouse,
    parse_page,
    parse_popup,
    slugify,
)

PAGE = (pathlib.Path(__file__).parent / "fixtures" / "mmh_search.html").read_text()
TODAY = date(2026, 10, 18)


def by_id(listings, listing_id):
    return next(l for l in listings if l.id == listing_id)


def test_every_marker_parses():
    listings, total = parse_page(PAGE)
    assert total == len(listings) == 34
    assert len({l.id for l in listings}) == 34
    assert all(l.continent and l.slots for l in listings)
    assert all(name in PETS or name == "other animal" for l in listings for name, _ in l.pets)
    assert all(count is None for l in listings for _, count in l.pets)


def test_fields():
    listings, _ = parse_page(PAGE)
    condo = by_id(listings, "32697")
    assert condo.title == "Beautiful seaside condo in Meia Praia with three dogs"
    assert condo.url == "https://www.mindmyhouse.com/sitters/assignment/32697"
    assert (condo.continent, condo.country, condo.country_code) == ("europe", "portugal", "pt")
    assert condo.place == "Lagos, Faro, Portugal"  # from "Portugal, Lagos, Faro"
    assert condo.photo_url == (
        "https://www.mindmyhouse.com/assets/owners_photos/large/bee4a3576ee365a725cd03efb4d381b8.jpg"
    )
    assert condo.pets == (("dog", None),)
    (slot,) = condo.slots
    assert (slot.id, slot.start, slot.end, slot.days, slot.is_open) == (
        "32697-20261219-20261231", date(2026, 12, 19), date(2026, 12, 31), 12, True
    )


def test_edge_cases():
    listings, _ = parse_page(PAGE)
    finca = by_id(listings, "25356")  # four date ranges, five kinds of animals
    assert [(s.start, s.end) for s in finca.slots][-1] == (date(2027, 1, 1), date(2027, 1, 14))
    assert len(finca.slots) == 4
    assert [p for p, _ in finca.pets] == ["dog", "cat", "horse", "poultry", "farm animal"]
    assert by_id(listings, "51651").title.startswith("I'm looking")  # \' in the JS string
    assert "&" in by_id(listings, "52245").title and "&amp;" not in by_id(listings, "52245").title
    assert by_id(listings, "49350").place == "Ireland"  # "Ireland, "
    caribbean = by_id(listings, "50110")
    assert (caribbean.continent, caribbean.country, caribbean.country_code) == ("north-america", "caribbean", None)
    assert by_id(listings, "9288").photo_url is None  # the site's placeholder image
    assert by_id(listings, "37817").pets == ()


def test_country_table_covers_the_site_dropdown_names_with_valid_codes():
    assert all(c in {"europe", "asia", "africa", "oceania", "north-america", "south-america"}
               for c, _ in COUNTRIES.values())
    assert all(code is None or (len(code) == 2 and code.isalpha() and code.islower())
               for _, code in COUNTRIES.values())


class Fake:
    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.notices: list[str] = []
        self.now = 1000.0

        def record(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, text=PAGE)

        async def nosleep(_):
            return None

        self.http = Http(
            min_interval=0,
            client_factory=lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(record)),
            sleep=nosleep,
        )

        async def notice(text):
            self.notices.append(text)

        self.mmh = MindMyHouse(self.http, notice=notice, clock=lambda: TODAY, monotonic=lambda: self.now)

    def run(self, coro_factory):
        async def go():
            try:
                return await coro_factory()
            finally:
                await self.http.aclose()

        return asyncio.run(go())


def test_search_filters_locally_and_one_fetch_serves_a_while():
    fake = Fake()

    async def go():
        europe = await fake.mmh.search(Filter("europe", exclude_countries=("united-kingdom",)), max_pages=1)
        cats = await fake.mmh.search(
            Filter("europe", country="spain", pets=("cat",), date_from=date(2026, 12, 1)), max_pages=None
        )
        fake.now += CACHE_SECONDS + 1
        await fake.mmh.search(Filter("asia"), max_pages=1)
        return europe, cats

    europe, cats = fake.run(go)
    assert europe and all(l.continent == "europe" and l.country != "united-kingdom" for l in europe)
    # Spain, has a cat, a slot from December on ("52240" only has a dog)
    assert {l.id for l in cats} == {"10759", "25356", "51651", "51797", "52007"}
    assert len(fake.requests) == 2  # the second search came from the cache
    assert str(fake.requests[0].url) == SEARCH_PAGE


def test_countries_of_a_continent_largest_first():
    fake = Fake()
    countries = fake.run(lambda: fake.mmh.countries("europe"))
    assert [(c.slug, c.name, c.count) for c in countries[:2]] == [("spain", "Spain", 6), ("france", "France", 3)]
    assert "united-states" not in {c.slug for c in countries}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="maintenance"),
        httpx.Response(200, text="<html>a new design</html>"),
        httpx.Response(200, text='Found 3 assignments<script>m.bindPopup(\'<div>broken</div>\')</script>'),
    ],
)
def test_broken_page_is_a_source_error(response):
    fake = Fake(lambda request: response)
    with pytest.raises(SourceError):
        fake.run(lambda: fake.mmh.search(Filter("europe"), max_pages=1))


def test_missing_markers_and_unknown_countries_are_told_once():
    page = PAGE.replace("Found 34 assignments", "Found 36 assignments").replace(
        "summary\">Portugal, Lagos, Faro", "summary\">Atlantis, Lagos, Faro"
    )
    fake = Fake(lambda request: httpx.Response(200, text=page))

    async def go():
        first = await fake.mmh.search(Filter("europe", country="portugal"), max_pages=1)
        fake.now += CACHE_SECONDS + 1
        await fake.mmh.search(Filter("europe"), max_pages=1)
        return first

    portugal = fake.run(go)
    assert "32697" not in {l.id for l in portugal}  # its country is unknown now
    assert len(fake.notices) == 2  # once each, not on every fetch
    assert any("found 36 assignments, the map has 34" in n for n in fake.notices)
    assert any("'Atlantis'" in n for n in fake.notices)


def test_full_title_comes_from_the_marker_not_the_cut_popup():
    listings, _ = parse_page(PAGE)
    hillside = by_id(listings, "10759")
    assert hillside.title == (
        "Private hillside 2 acre house 20 minutes from the sea and Tejeda National Park in southern Spain"
    )
    assert not any(l.title.endswith("…") for l in listings)


def test_changed_date_format_fails_loudly_instead_of_emptying_the_site():
    page = re.sub(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})", r"\1/\2/\3", PAGE)
    with pytest.raises(SourceError, match="parsed 0 of 34"):
        parse_page(page)


def test_city_state_place_keeps_the_country_last():
    popup = (
        '<h2 class="title"><a href="/sitters/assignment/7">Flat</a></h2>'
        '<div class="summary">Singapore, Singapore, Central Region</div>'
        '<ul class="summary"><li class="date-duration"><span class="icon-calendar"></span>'
        "1 Dec 2026 - 9 Dec 2026</li></ul>"
    )
    listing = parse_popup(popup)
    assert listing.place == "Central Region, Singapore" and listing.country == "singapore"
    generic = parse_popup(popup.replace("Singapore, Singapore, Central Region", "France, Laurac, Carcassonne, Metropolitan France"))
    assert generic.place == "Laurac, France"


def test_a_failure_is_not_refetched_for_the_rest_of_the_tick():
    fake = Fake(lambda request: httpx.Response(502, text="bad gateway"))

    async def go():
        errors = []
        for _ in range(3):
            try:
                await fake.mmh.search(Filter("europe"), max_pages=1)
            except SourceError as exc:
                errors.append(str(exc))
        one_fetch = len(fake.requests)  # Http's own retries of the 502
        fake.now += RETRY_SECONDS + 1
        with pytest.raises(SourceError):
            await fake.mmh.search(Filter("europe"), max_pages=1)
        return errors, one_fetch

    errors, one_fetch = fake.run(go)
    assert len(errors) == 3 and "HTTP 502" in errors[0] and "failed 0 s ago" in errors[1]
    assert len(fake.requests) == 2 * one_fetch  # one fetch per retry window, not one per call


def test_unknown_animals_are_told():
    page = PAGE.replace("Dog(s)", "Alpaca(s)")
    fake = Fake(lambda request: httpx.Response(200, text=page))
    fake.run(lambda: fake.mmh.search(Filter("europe"), max_pages=1))
    assert any("unknown animal 'alpaca'" in n for n in fake.notices)


def test_countries_include_those_without_sits_right_now():
    fake = Fake()
    countries = fake.run(lambda: fake.mmh.countries("europe"))
    names = [c.name for c in countries]
    assert len(countries) == sum(1 for cont, _ in COUNTRIES.values() if cont == "europe")
    assert "Albania" in names and next(c for c in countries if c.name == "Albania").count == 0
    assert all(c.slug == slugify(c.name) for c in countries)


def test_by_start_is_the_continent_nearest_start_first():
    fake = Fake()

    async def go():
        return [page async for page in fake.mmh.by_start(Filter("europe", country="spain", date_from=date(2026, 12, 1)))]

    (page,) = fake.run(go)
    keys = [first_open_start(l, date(2026, 12, 1)) for l in page]
    assert keys == sorted(keys) and None not in keys
    assert {l.continent for l in page} == {"europe"} and len({l.country for l in page}) > 1  # country left to the caller


def test_by_start_with_a_minimum_length_orders_by_the_first_long_slot():
    fake = Fake()
    since = date(2026, 12, 1)

    async def go():
        long = [page async for page in fake.mmh.by_start(Filter("europe", date_from=since, min_days=21))]
        every = [page async for page in fake.mmh.by_start(Filter("europe", date_from=since))]
        return long, every

    (page,), (everything,) = fake.run(go)
    keys = [first_open_start(l, since, 21) for l in page]
    assert page and keys == sorted(keys) and None not in keys
    assert len(page) < len(everything)  # some only have shorter sits


def test_goes_direct_even_when_the_proxy_is_always_on():
    used: list[bool] = []

    def factory(proxy):
        def handler(request):
            used.append(proxy is not None)
            return httpx.Response(200, text=PAGE)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    http = Http("http://proxy:1", "always", min_interval=0, client_factory=factory)
    mmh = MindMyHouse(http, clock=lambda: TODAY)

    async def go():
        try:
            return await mmh.search(Filter("europe"), max_pages=1)
        finally:
            await http.aclose()

    assert asyncio.run(go()) and used == [False]
