"""Towns → coordinates for /search around a town: OpenStreetMap's Nominatim.

The public server is free under its policy
(https://operations.osmfoundation.org/policies/nominatim/): an identifying
User-Agent, at most a request a second (Http spaces all requests further
apart) and results cached — here each town once per process, a miss too.
A bot of a few people asks a few times a day.

The best match wins, as on openstreetmap.org: a town, but a region
("Algarve") or a lake ("Lake Como") works as well. A country with it
(`countrycodes`) says which of several towns of that name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from . import geo
from .http import Http

log = logging.getLogger(__name__)

SEARCH = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "petalert (Telegram bot for house-sitting alerts; github.com/VladislavKorzh/petalert)"
CACHE_SIZE = 1000  # towns; the oldest goes first


class GeocodeError(Exception):
    """The lookup failed (not: the town wasn't found)."""


@dataclass(frozen=True)
class Town:
    name: str  # as the map has it in English: "Málaga"
    country_code: str | None  # ISO 3166 alpha-2, lower case
    lat: float
    lon: float

    @property
    def label(self) -> str:
        """`Málaga, Spain`; `San Marino` (a country of its own name)."""
        country = geo.name(self.country_code) if self.country_code in geo.COUNTRIES else None
        return f"{self.name}, {country}" if country and country != self.name else self.name


class Geocoder:
    def __init__(self, http: Http):
        self.http = http
        self._cache: dict[tuple[str, str | None], Town | None] = {}

    async def town(self, query: str, country: str | None = None) -> Town | None:
        """The best match for `query` (in `country`, an ISO code, if given);
        None if the map has nothing of that name."""
        key = (" ".join(query.lower().split()), country)
        if key in self._cache:
            return self._cache[key]
        params = {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "1", "accept-language": "en"}
        if country:
            params["countrycodes"] = country
        try:
            resp = await self.http.get(SEARCH, params=params, headers={"User-Agent": USER_AGENT}, proxy_mode="fallback")
        except httpx.TransportError as exc:
            raise GeocodeError(f"Nominatim: {exc!r}") from exc
        if resp.status_code != 200:
            raise GeocodeError(f"Nominatim HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            found = resp.json()
            town = None
            if found:
                best = found[0]
                code = (best.get("address") or {}).get("country_code")
                town = Town(
                    name=best.get("name") or best["display_name"].split(",")[0],
                    country_code=code.lower() if code else None,
                    lat=float(best["lat"]),
                    lon=float(best["lon"]),
                )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeocodeError(f"Nominatim answer changed: {resp.text[:200]}") from exc
        log.info("geocode %r (%s) → %s", query, country, town)
        if len(self._cache) >= CACHE_SIZE:
            del self._cache[next(iter(self._cache))]
        self._cache[key] = town
        return town
