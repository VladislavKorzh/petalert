"""Live MindMyHouse. Only with PETALERT_LIVE=1 — catches page changes before users do.

    PETALERT_LIVE=1 uv run pytest tests/test_live_mmh.py
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from petalert.http import Http
from petalert.sources.mmh import SEARCH_PAGE, USER_AGENT, parse_page

pytestmark = pytest.mark.skipif(os.environ.get("PETALERT_LIVE") != "1", reason="PETALERT_LIVE=1")


def get(url: str) -> httpx.Response:
    async def go():
        http = Http(min_interval=1.0)
        try:
            return await http.get(url, headers={"User-Agent": USER_AGENT})
        finally:
            await http.aclose()

    return asyncio.run(go())


def test_the_map_has_every_assignment_and_every_country_is_known():
    resp = get(SEARCH_PAGE)
    assert resp.status_code == 200
    listings, total = parse_page(resp.text)
    assert total > 50 and len(listings) == total
    assert [l.place for l in listings if not l.continent] == []
    assert all(l.slots for l in listings)
    assert all(l.lat is not None and l.lon is not None for l in listings)  # /search around a town


def test_listing_page_and_photo_exist():
    listings, _ = parse_page(get(SEARCH_PAGE).text)
    listing = next(l for l in listings if l.photo_url)
    assert get(listing.url).status_code == 200
    photo = get(listing.photo_url)
    assert photo.status_code == 200 and photo.headers["content-type"].startswith("image/")
