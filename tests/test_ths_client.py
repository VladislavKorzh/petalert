"""TrustedHousesitters поверх мокнутого HTTP: заголовки, 406-самолікування,
пагінація, помилки. Живий THS тут не смикається — для цього test_live_ths.py."""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import date

import httpx
import pytest

from petalert.http import Http
from petalert.models import Filter
from petalert.sources.base import SourceError
from petalert.sources.ths import API, SEARCH_PAGE, TrustedHousesitters

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "ths_search_portugal.json").read_text())
COUNTRIES = json.loads((FIXTURES / "ths_countries_europe.json").read_text())
PAGE_HTML = (FIXTURES / "ths_page_snippet.html").read_text()


def page_of(n: int, total: int, start: int = 0) -> dict:
    """n різних оголошень на основі першого з фікстури."""
    base = SEARCH["results"][1]
    return {
        "results": [dict(base, id=str(start + i)) for i in range(n)],
        "total": total,
    }


class Fake:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self.notices: list[str] = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        async def nosleep(_):
            return None

        self.http = Http(
            min_interval=0,
            client_factory=lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(record)),
            sleep=nosleep,
        )

        async def notice(text):
            self.notices.append(text)

        self.ths = TrustedHousesitters(
            self.http, "2025-08-14", notice=notice, clock=lambda: date(2026, 10, 17)
        )

    def run(self, coro_factory):
        async def go():
            try:
                return await coro_factory(self.ths)
            finally:
                await self.http.aclose()

        return asyncio.run(go())

    def queries(self) -> list[dict]:
        return [
            json.loads(r.url.params["query"]) for r in self.requests if str(r.url).startswith(API)
        ]


def test_request_carries_referer_version_and_json_query():
    fake = Fake(lambda r: httpx.Response(200, json=SEARCH))
    listings = fake.run(lambda ths: ths.search(Filter("europe", country="portugal"), max_pages=1))
    assert len(listings) == len(SEARCH["results"])
    (req,) = fake.requests
    assert req.headers["Referer"] == SEARCH_PAGE
    assert req.headers["api-version"] == "2025-08-14"
    assert req.headers["THS-Platform-Detail"] == "web"
    assert fake.queries()[0]["filters"]["geoHierarchy"] == {"countrySlug": "portugal"}


def test_406_refreshes_version_from_page_and_tells_admin():
    def handler(r):
        if str(r.url).startswith(API):
            ok = r.headers.get("api-version") == "2026-01-01"
            return httpx.Response(200, json=SEARCH) if ok else httpx.Response(406)
        return httpx.Response(200, text=PAGE_HTML.replace("2025-08-14", "2026-01-01"))

    fake = Fake(handler)
    fake.ths.api_version = "2020-01-01"
    fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert fake.ths.api_version == "2026-01-01"
    assert len(fake.notices) == 1 and "2020-01-01 → 2026-01-01" in fake.notices[0]


def test_406_twice_falls_back_to_no_version_header():
    def handler(r):
        if str(r.url).startswith(API):
            if "api-version" in r.headers:
                return httpx.Response(406)
            return httpx.Response(200, json=SEARCH)
        return httpx.Response(200, text=PAGE_HTML.replace("2025-08-14", "2026-01-01"))

    fake = Fake(handler)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert listings and fake.ths.api_version is None
    assert "api-version" not in fake.requests[-1].headers
    assert any("without the header" in n for n in fake.notices)


def test_401_means_auth_requirement_changed():
    fake = Fake(lambda r: httpx.Response(401, json={"detail": "no"}))
    with pytest.raises(SourceError, match="401"):
        fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))


def test_other_errors_carry_status_and_body():
    fake = Fake(lambda r: httpx.Response(500, text="Looks like something went wrong"))
    with pytest.raises(SourceError, match="500.*something went wrong"):
        fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))


def test_paging_until_total_and_dedup_across_pages():
    pages = {1: page_of(100, 230), 2: page_of(100, 230, start=95), 3: page_of(30, 230, start=200)}
    fake = Fake(lambda r: httpx.Response(200, json=pages[json.loads(r.url.params["query"])["page"]]))
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert [q["page"] for q in fake.queries()] == [1, 2, 3]
    # Сторінка 2 зсунулась на 5 оголошень назад — дублікати відкинуті.
    assert len(listings) == 225


def test_full_crawl_stops_at_the_api_result_limit():
    # THS: сторінка 11 по 100 → 400 «limited to the first 1000 results».
    def handler(r):
        page = json.loads(r.url.params["query"])["page"]
        if page > 10:
            return httpx.Response(400, json={"page": ["Search results are limited to the first 1000 results."]})
        return httpx.Response(200, json=page_of(100, 2650, start=page * 100))

    fake = Fake(handler)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert len(fake.requests) == 10 and len(listings) == 1000


def test_max_pages_one_fetches_only_the_first_page():
    fake = Fake(lambda r: httpx.Response(200, json=page_of(100, 500)))
    fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert len(fake.requests) == 1


def test_one_broken_listing_is_skipped_but_a_fully_broken_page_raises():
    broken = {"id": "x", "title": "no location"}
    good = SEARCH["results"][0]
    fake = Fake(lambda r: httpx.Response(200, json={"results": [broken, good], "total": 2}))
    assert len(fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))) == 1

    fake = Fake(lambda r: httpx.Response(200, json={"results": [broken], "total": 1}))
    with pytest.raises(SourceError, match="shape changed"):
        fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))


def test_countries_from_facet_sorted_by_count():
    fake = Fake(lambda r: httpx.Response(200, json=COUNTRIES))
    countries = fake.run(lambda ths: ths.countries("europe"))
    (q,) = fake.queries()
    assert q["facets"] == ["country"] and q["resultsPerPage"] == 1
    assert q["filters"]["geoHierarchy"] == {"continentSlug": "europe"}
    assert countries[0].slug == "united-kingdom"
    assert [c.count for c in countries] == sorted((c.count for c in countries), reverse=True)
