"""TrustedHousesitters: внутрішній JSON API сторінки пошуку.

`GET /api/v3/search/listings/?query=<JSON>` працює анонімно, але лише з
`Referer` сторінки пошуку — без нього 401 «Authentication credentials were not
provided». Формат query знято з JS-бандла сайту (білдер запиту в модулі
пошуку) і перевірено живими запитами 2026-10-17.

`api-version` сайт бере з `window.__ENVIRONMENT__` у HTML. Неправильне
значення дає 406, тому при 406 версія перечитується зі сторінки, а якщо й це
не допомогло — запит іде без заголовка (без нього API теж відповідає).

Дебаг: `python -m petalert.sources search --continent europe --country portugal`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from ..http import Http
from ..models import Country, Filter, Listing, Slot
from .base import SourceError

log = logging.getLogger(__name__)

BASE = "https://www.trustedhousesitters.com"
SEARCH_PAGE = f"{BASE}/house-and-pet-sitting-assignments/"
API = f"{BASE}/api/v3/search/listings/"
# Сайт сам віддає такий розмір прев'ю для соцмереж; довільні трансформації
# Cloudinary в цього акаунта заборонені (404).
PHOTO = "https://res.cloudinary.com/trustedhousesitters/image/upload/t_social_media_preview"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PER_PAGE = 100
# API віддає лише перші 1000 результатів пошуку: далі 400 «Search results are
# limited to the first 1000 results» (перевірено 2026-10-17). Тож «повний обхід»
# широкого фільтра (уся Європа ~2650) — це 1000 найновіших, а не все.
RESULT_LIMIT = 1000
MAX_PAGES = RESULT_LIMIT // PER_PAGE
_VERSION_RE = re.compile(r'"apiVersion":"([0-9]{4}-[0-9]{2}-[0-9]{2})"')

Notice = Callable[[str], Awaitable[None]]


def build_query(
    flt: Filter, today: date, page: int = 1, *, facets: tuple[str, ...] = ()
) -> dict[str, Any]:
    assignments: dict[str, Any] = {
        "confirmed": False,
        "reviewing": False,
        "dateFrom": max(today, flt.date_from or today).isoformat(),
    }
    if flt.date_to:
        assignments["dateTo"] = flt.date_to.isoformat()
    duration = {}
    if flt.min_days is not None:
        duration["minimum"] = flt.min_days
    if flt.max_days is not None:
        duration["maximum"] = flt.max_days
    if duration:
        assignments["durationInDays"] = duration
    filters: dict[str, Any] = {
        "activeMembership": True,
        "assignments": assignments,
        # countryCode API мовчки ігнорує — лише slug.
        "geoHierarchy": (
            {"countrySlug": flt.country} if flt.country else {"continentSlug": flt.continent}
        ),
    }
    if flt.pets:
        filters["pets"] = [{"type": pet} for pet in sorted(flt.pets)]
    return {
        "filters": filters,
        "facets": list(facets),
        "sort": [{"published": "desc"}],
        "page": page,
        "resultsPerPage": PER_PAGE,
        "debug": False,
        "stats": [],
    }


def parse_listing(raw: dict[str, Any]) -> Listing:
    """Обов'язкове для алерту читається суворо (KeyError), решта — через .get."""
    listing_id = str(raw["id"])
    loc = raw["location"]
    slots = tuple(
        Slot(
            id=str(a["id"]),
            start=date.fromisoformat(a["startDate"]),
            end=date.fromisoformat(a["endDate"]),
            is_open=not a["isConfirmed"] and not a["isReviewing"],
            applicants=a.get("numberOfApplicants"),
        )
        for a in raw["assignments"]
    )
    place_parts: list[str] = []
    for part in (loc.get("name"), loc.get("admin1Name"), loc.get("countryName")):
        if part and part not in place_parts:
            place_parts.append(part)
    photos = raw.get("photos") or []
    cover = next((p for p in photos if p.get("coverPhoto")), photos[0] if photos else None)
    return Listing(
        source="ths",
        id=listing_id,
        title=" ".join((raw.get("title") or "Untitled").split()),
        url=f"{SEARCH_PAGE}l/{listing_id}/",
        continent=loc["continentSlug"],
        country=loc["countrySlug"],
        place=", ".join(place_parts),
        photo_url=f"{PHOTO}/{cover['publicId']}" if cover and cover.get("publicId") else None,
        pets=tuple(
            (a["name"], int(a.get("count") or 1)) for a in raw.get("animals") or [] if a.get("name")
        ),
        slots=slots,
    )


class TrustedHousesitters:
    key = "ths"
    title = "TrustedHousesitters"

    def __init__(
        self,
        http: Http,
        api_version: str | None,
        *,
        notice: Notice | None = None,
        clock: Callable[[], date] = date.today,
    ):
        self.http = http
        self.api_version = api_version
        self._notice = notice
        self._clock = clock

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": SEARCH_PAGE,
            "THS-Platform-Detail": "web",
        }
        if self.api_version:
            headers["api-version"] = self.api_version
        return headers

    async def _tell(self, text: str) -> None:
        log.warning(text)
        if self._notice:
            await self._notice(text)

    async def _site_version(self) -> str | None:
        resp = await self.http.get(SEARCH_PAGE, headers={"User-Agent": USER_AGENT})
        match = _VERSION_RE.search(resp.text) if resp.status_code == 200 else None
        return match.group(1) if match else None

    async def _query(self, query: dict[str, Any]) -> dict[str, Any]:
        params = {"query": json.dumps(query, separators=(",", ":"))}
        resp = await self.http.get(API, params=params, headers=self._headers())
        if resp.status_code == 406:
            old = self.api_version
            new = await self._site_version()
            if new and new != old:
                self.api_version = new
                resp = await self.http.get(API, params=params, headers=self._headers())
                if resp.status_code != 406:
                    await self._tell(
                        f"THS api-version changed {old} → {new}. Update THS_API_VERSION in .env."
                    )
            if resp.status_code == 406:
                self.api_version = None
                resp = await self.http.get(API, params=params, headers=self._headers())
                await self._tell(
                    f"THS rejects api-version {old!r} (site says {new!r}); "
                    f"now querying without the header → HTTP {resp.status_code}."
                )
        if resp.status_code == 401:
            raise SourceError("THS HTTP 401: Referer/auth requirement changed")
        if resp.status_code != 200:
            raise SourceError(f"THS HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"THS returned non-JSON: {resp.text[:200]}") from exc

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        today = self._clock()
        seen: dict[str, Listing] = {}
        page = 1
        while True:
            data = await self._query(build_query(flt, today, page))
            try:
                results, total = data["results"], int(data["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError(f"THS response shape changed: {str(data)[:200]}") from exc
            parsed = 0
            for raw in results:
                try:
                    listing = parse_listing(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("THS: не розібрав оголошення %s: %r", raw.get("id"), exc)
                    continue
                parsed += 1
                # Між сторінками стрічка зсувається на щойно опубліковані —
                # той самий listing може прийти двічі.
                seen.setdefault(listing.id, listing)
            if results and not parsed:
                raise SourceError(f"THS listing shape changed: {str(results[0])[:200]}")
            if len(results) < PER_PAGE or page * PER_PAGE >= total:
                break
            if max_pages is not None and page >= max_pages:
                break
            if page >= MAX_PAGES:
                log.info("THS: ліміт API %s з %s результатів для %s", RESULT_LIMIT, total, flt.to_json())
                break
            page += 1
        return list(seen.values())

    async def countries(self, continent: str) -> list[Country]:
        query = build_query(Filter(continent=continent), self._clock(), facets=("country",))
        query["resultsPerPage"] = 1
        data = await self._query(query)
        try:
            raw = data["facets"]["country"]
            countries = [Country(slug=c["slug"], name=c["name"], count=int(c["count"])) for c in raw]
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"THS country facet shape changed: {str(data)[:200]}") from exc
        return sorted(countries, key=lambda c: (-c.count, c.name))
