"""Live Nomador. Only with PETALERT_LIVE=1 — catches site changes before users do.

    PETALERT_LIVE=1 uv run pytest tests/test_live_nmd.py
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from petalert.http import Http
from petalert.sources.nmd import (
    BANDS,
    HOUSE_SITS,
    LAST_PAGE,
    MAP,
    PER_PAGE,
    SEARCH,
    parse_page,
    stay_url,
)

pytestmark = pytest.mark.skipif(os.environ.get("PETALERT_LIVE") != "1", reason="PETALERT_LIVE=1")


def get(url: str, params: dict[str, str] | None = None, *, xhr: bool = True) -> httpx.Response:
    headers = {"Accept-Language": "en"}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"

    async def go():
        http = Http(min_interval=1.0)
        try:
            return await http.get(url, params=params, headers=headers, browser=True)
        finally:
            await http.aclose()

    return asyncio.run(go())


def test_the_map_has_the_house_sits():
    stays = get(MAP, HOUSE_SITS).json()
    assert len(stays) > 300
    assert {s["stayType"] for s in stays} == {"house-sitting"}


def test_each_length_band_is_well_under_the_page_cap():
    # The map takes the same filters as the result pages, so it counts each band.
    for low, high in BANDS:
        params = dict(HOUSE_SITS)
        if low is not None:
            params["minDuration"] = str(low)
        if high is not None:
            params["maxDuration"] = str(high)
        n = len(get(MAP, params).json())
        assert 0 < n < 0.8 * LAST_PAGE * PER_PAGE, f"{n} stays of {low}–{high} days: time to split BANDS"


def test_the_first_result_page_parses_and_every_country_is_known():
    listings, cards = parse_page(get(SEARCH, {**HOUSE_SITS, "page": "1"}).text)
    assert cards == len(listings) == PER_PAGE
    assert [l.place for l in listings if not l.continent] == []
    assert sum(bool(l.slots) for l in listings) >= PER_PAGE - 2  # a few flexible stays have no dates
    assert any(l.pets for l in listings)
    assert all(l.lat is not None and l.lon is not None for l in listings)  # /search around a town


def test_a_stay_page_has_its_card_and_a_photo():
    stay_id = get(MAP, HOUSE_SITS).json()[0]["id"]
    resp = get(stay_url(stay_id), xhr=False)
    assert resp.status_code == 200 and 'property="og:image"' in resp.text
    listings, _ = parse_page(resp.text)
    listing = next(l for l in listings if l.id == stay_id)
    photo = get(listing.photo_url, xhr=False)
    assert photo.status_code == 200 and photo.headers["content-type"].startswith("image/")
