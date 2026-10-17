"""Live THS. Only with PETALERT_LIVE=1 — catches API drift before users do.

    PETALERT_LIVE=1 uv run pytest tests/test_live_ths.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import httpx
import pytest

from petalert.config import DEFAULT_THS_API_VERSION
from petalert.http import Http
from petalert.models import Filter, matching_slots
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
