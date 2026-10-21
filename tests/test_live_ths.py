"""Live THS. Only with PETALERT_LIVE=1 — catches API drift before users do.

    PETALERT_LIVE=1 uv run pytest tests/test_live_ths.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import httpx
import pytest

from petalert import geo
from petalert.config import DEFAULT_THS_API_VERSION
from petalert.http import Http
from petalert.models import CONTINENTS, Circle, Filter, distance_km, first_open_start, matching_slots
from petalert.sources.ths import USER_AGENT, TrustedHousesitters

pytestmark = pytest.mark.skipif(os.environ.get("PETALERT_LIVE") != "1", reason="PETALERT_LIVE=1")


def run(fn):
    async def go():
        http = Http(min_interval=1.0)
        try:
            return await fn(TrustedHousesitters(http, DEFAULT_THS_API_VERSION))
        finally:
            await http.aclose()

    return asyncio.run(go())


def test_europe_first_page_parses_and_has_open_dates():
    flt = Filter("europe")
    listings = run(lambda ths: ths.search(flt, max_pages=1))
    assert len(listings) >= 50
    assert all(l.continent == "europe" for l in listings)
    assert sum(len(matching_slots(l, flt, date.today())) for l in listings) > 0


def test_country_facet_works():
    countries = run(lambda ths: ths.countries("europe"))
    assert any(c.slug == "united-kingdom" for c in countries)


def test_site_api_version_matches_the_default():
    version = run(lambda ths: ths._site_version())
    assert version == DEFAULT_THS_API_VERSION, f"THS is now on {version}: update THS_API_VERSION"


def test_short_listing_url_redirects():
    listing = run(lambda ths: ths.search(Filter("europe"), max_pages=1))[0]
    resp = httpx.get(listing.url, headers={"User-Agent": USER_AGENT})
    assert resp.status_code == 302
    assert f"/l/{listing.id}/" in resp.headers["location"]


def test_every_ths_country_is_in_geo_on_the_same_continent():
    """/search queries a country by geo's slug and scopes it by geo's continent."""
    by_slug = {geo.slug(code): code for code in geo.COUNTRIES}

    async def facets(ths):
        return {c: await ths.countries(c) for c in CONTINENTS}

    for continent, countries in run(facets).items():
        for country in countries:
            code = by_slug.get(country.slug)
            assert code and geo.continent(code) == continent, (continent, country.slug)


@pytest.mark.parametrize("min_days", [None, 14])
def test_by_start_is_ordered_by_the_first_open_start(min_days):
    since = date.today() + timedelta(days=1)  # by_start's earliest

    async def two_pages(ths):
        pages = []
        async for page in ths.by_start(Filter("europe", date_from=since, min_days=min_days)):
            pages.append(page)
            if len(pages) == 2:
                break
        return pages

    pages = run(two_pages)  # by_start itself raises if the order breaks
    keys = [first_open_start(l, since, min_days) for page in pages for l in page]
    assert len(keys) > 150 and keys == sorted(keys)


def test_by_start_in_a_circle_keeps_to_it_across_borders():
    """/search around a town: THS's geoPoint, ordered as by_start needs."""
    since = date.today() + timedelta(days=1)
    geneva = Circle(46.2044, 6.1432, 100)  # Switzerland and France

    async def nearest(ths):
        listings = []
        async for page in ths.by_start(Filter("", near=geneva, date_from=since)):
            listings += page
            if len(listings) >= 200:  # two pages at most
                break
        return listings

    listings = run(nearest)
    keys = [first_open_start(l, since) for l in listings]
    assert len(listings) > 5 and keys == sorted(keys)  # 18 on 2026-10-21
    assert {"ch", "fr"} <= {l.country_code for l in listings}
    # The same distance as ours, give or take the Earth's radius THS takes.
    assert all(distance_km(geneva.lat, geneva.lon, l.lat, l.lon) <= geneva.km + 1 for l in listings)
