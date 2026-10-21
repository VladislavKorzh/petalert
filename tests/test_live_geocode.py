"""Live Nominatim. Only with PETALERT_LIVE=1 — catches changes of its answer.

    PETALERT_LIVE=1 uv run pytest tests/test_live_geocode.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from petalert.geocode import Geocoder
from petalert.http import Http
from petalert.models import distance_km

pytestmark = pytest.mark.skipif(os.environ.get("PETALERT_LIVE") != "1", reason="PETALERT_LIVE=1")


def test_towns_are_found_with_their_country_and_typos_are_not():
    async def go():
        http = Http(min_interval=1.0)  # the public server's limit
        try:
            geocoder = Geocoder(http)
            return [await geocoder.town("malaga", "es"), await geocoder.town("valencia"), await geocoder.town("malagaa")]
        finally:
            await http.aclose()

    malaga, valencia, typo = asyncio.run(go())
    assert malaga.label == "Málaga, Spain" and distance_km(malaga.lat, malaga.lon, 36.7213, -4.4214) < 10
    assert valencia.country_code == "es"  # the best known one
    assert typo is None
