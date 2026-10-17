"""TrustedHousesitters: the search page's internal JSON API.

`GET /api/v3/search/listings/?query=<JSON>` works anonymously, but only with
the search page as `Referer` — without it the API returns 401 "Authentication
credentials were not provided". The query format was taken from the site's JS
bundle (the query builder in the search module) and verified with live
requests on 2026-10-17.

The site takes `api-version` from `window.__ENVIRONMENT__` in the HTML. A wrong
value gets a 406, so on a 406 the version is re-read from the page, and if
that doesn't help the request goes without the header (the API answers
without it too).

Debug: `python -m petalert.sources search --continent europe --country portugal`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from typing import Any

from ..http import Http
from ..models import Country, Filter, Listing, Slot
from .base import SourceError

log = logging.getLogger(__name__)

BASE = "https://www.trustedhousesitters.com"
SEARCH_PAGE = f"{BASE}/house-and-pet-sitting-assignments/"
API = f"{BASE}/api/v3/search/listings/"
# The site itself serves this social-preview size; arbitrary Cloudinary
# transformations are disabled for this account (404).
PHOTO = "https://res.cloudinary.com/trustedhousesitters/image/upload/t_social_media_preview"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PER_PAGE = 100
# The API refuses to page past the first 1000 results ("Search results are
# limited to the first 1000 results", verified 2026-10-17). So instead of page
# numbers the crawl pages with a cursor: every request is page 1 of "published
# before the last listing seen". The bound is inclusive, so consecutive pages
# overlap by a listing and are deduped by id. There is no cap that way, and a
# listing dropping out of the feed mid-crawl can't shift an unread one onto a
# page that was already fetched.
MAX_PAGES = 200  # runaway guard; all of Europe is ~27 pages
RESULT_LIMIT = 1000  # how deep plain page numbers can go
FAR_FUTURE = "2100-01-01T00:00:00"  # a cursor that keeps every dated listing
# A filter this small is fetched completely on every tick, whatever max_pages
# says: it costs at most a few requests and catches dates added to old listings
# without a `published` bump (rare, but it happens).
SMALL_TOTAL = 300
_VERSION_RE = re.compile(r'"apiVersion":"([0-9]{4}-[0-9]{2}-[0-9]{2})"')

Notice = Callable[[str], Awaitable[None]]


def build_query(
    flt: Filter,
    today: date,
    *,
    published_before: str | None = None,
    page: int = 1,
    facets: tuple[str, ...] = (),
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
        # The API silently ignores countryCode — slug only.
        "geoHierarchy": (
            {"countrySlug": flt.country} if flt.country else {"continentSlug": flt.continent}
        ),
    }
    if flt.pets:
        filters["pets"] = [{"type": pet} for pet in sorted(flt.pets)]
    if published_before:
        filters["published"] = {"before": published_before}  # inclusive
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
    """Fields an alert depends on are read strictly (KeyError); the rest via .get."""
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
            try:
                await self._notice(text)
            except Exception:
                # A failed notification must never fail the request it reports on.
                log.exception("THS: could not deliver the notice")

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

    async def _page(
        self,
        flt: Filter,
        today: date,
        before: str | None,
        seen: dict[str, Listing],
        *,
        refresh: bool = False,
        page: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch one page into `seen`; returns the raw results and the total."""
        data = await self._query(build_query(flt, today, published_before=before, page=page))
        try:
            results, total = data["results"], int(data["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"THS response shape changed: {str(data)[:200]}") from exc
        parsed = 0
        for raw in results:
            try:
                listing = parse_listing(raw)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("THS: could not parse listing %s: %r", raw.get("id"), exc)
                continue
            parsed += 1
            if refresh:
                seen[listing.id] = listing
            else:
                seen.setdefault(listing.id, listing)
        if results and not parsed:
            raise SourceError(f"THS listing shape changed: {str(results[0])[:200]}")
        return results, total

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        today = self._clock()
        seen: dict[str, Listing] = {}
        dated: set[str] = set()  # ids seen with a `published` date
        before: str | None = None
        limit = max_pages
        pages = 0
        while True:
            results, total = await self._page(flt, today, before, seen)
            pages += 1
            problem = None
            if before is not None and any(
                not r.get("published") or r["published"] > before for r in results
            ):
                # A cursor page may only hold listings at or below the cursor.
                # Anything else means THS stopped honouring filters.published.
                problem = "THS ignores filters.published; the cursor crawl can't be trusted"
            dated.update(str(r["id"]) for r in results if r.get("published") and "id" in r)
            if pages == 1:
                first_total = total
                if total <= SMALL_TOTAL:
                    limit = None
            cursor = results[-1].get("published") if results else None
            if problem is None and len(results) == PER_PAGE and (limit is None or pages < limit):
                if cursor and (cursor == before or pages >= MAX_PAGES):
                    # 100+ listings in one second, or a runaway.
                    problem = f"THS cursor crawl stuck at {cursor!r} after {pages} pages"
            if problem:
                if max_pages is None:
                    # Never pass a partial crawl off as complete: a later one
                    # would then alert everything this one missed.
                    raise SourceError(problem)
                log.warning("%s — keeping the first page for %s", problem, flt.to_json())
                return list(seen.values())  # a page-1 tick is fine with that
            if len(results) < PER_PAGE or (limit is not None and pages >= limit) or not cursor:
                break  # (no cursor: the undated listings, which sort last, have begun)
            before = cursor
        if max_pages is None and pages > 1:
            # A listing that got new dates mid-crawl jumped above the cursor; one
            # more look at the top catches it. Whatever it adds is the newest, so
            # it goes first (callers rely on newest-first order).
            known = set(seen)
            await self._page(flt, today, None, seen, refresh=True)
            top = {i: l for i, l in seen.items() if i not in known}
            seen = {**top, **{i: l for i, l in seen.items() if i not in top}}
        if limit is None and first_total > PER_PAGE and first_total > len(dated):
            # (≤ 100 results all came on page 1, undated ones included)
            await self._undated(flt, today, seen, first_total, first_total - len(dated))
        return list(seen.values())

    async def _country_counts(self, flt: Filter, today: date, before: str | None) -> dict[str, int]:
        query = build_query(flt, today, published_before=before, facets=("country",))
        query["resultsPerPage"] = 1
        data = await self._query(query)
        try:
            return {c["slug"]: int(c["count"]) for c in data["facets"]["country"]}
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"THS country facet shape changed: {str(data)[:200]}") from exc

    async def _undated(
        self, flt: Filter, today: date, seen: dict[str, Listing], total: int, missing: int
    ) -> None:
        """Listings without a `published` date never match a cursor; in the
        plain feed they sort last. Plain page numbers only reach 1000 results,
        so above that, find the countries holding them (country facet with vs
        without a cursor) and read the tail of each of those, scoped to it."""
        if total <= RESULT_LIMIT:
            scopes = [(flt, total, missing)]
        else:
            plain = await self._country_counts(flt, today, None)
            with_date = await self._country_counts(flt, today, FAR_FUTURE)
            scopes = []
            for slug, n in plain.items():
                gap = n - with_date.get(slug, 0)
                if gap <= 0 or slug in flt.exclude_countries:
                    continue
                if n > RESULT_LIMIT:
                    log.info("THS: %d undated listings in %s are out of reach (%d results)", gap, slug, n)
                    continue
                scopes.append((replace(flt, country=slug, exclude_countries=()), n, gap))
        for scope, n, gap in scopes:
            # Start a little before where they should be (listings may have left
            # the feed meanwhile) and go on while pages are full (or new ones arrived).
            page = max(1, (n - gap - 10) // PER_PAGE + 1)
            while True:
                results, now_total = await self._page(scope, today, None, seen, page=page)
                if len(results) < PER_PAGE or page * PER_PAGE >= min(max(n, now_total), RESULT_LIMIT):
                    break
                page += 1

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
