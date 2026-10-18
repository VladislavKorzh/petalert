"""TrustedHousesitters over mocked HTTP: headers, 406 self-healing, paging,
errors. The live THS is never hit here — that's what test_live_ths.py is for."""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import date, datetime, timedelta

import httpx
import pytest

from petalert.http import Http
from petalert.models import Filter
from petalert.sources.base import SourceError, TooBroad
from petalert.sources.ths import API, SEARCH_PAGE, TrustedHousesitters

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "ths_search_portugal.json").read_text())
COUNTRIES = json.loads((FIXTURES / "ths_countries_europe.json").read_text())
PAGE_HTML = (FIXTURES / "ths_page_snippet.html").read_text()


def page_of(n: int, total: int, start: int = 0) -> dict:
    """n distinct listings based on one from the fixture."""
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


class Feed:
    """A fake THS feed sorted by `published` desc that honours the keyset
    filter `published.before` (inclusive), like the real API."""

    def __init__(self, n: int, *, same_published: bool = False):
        base = SEARCH["results"][1]
        start = datetime(2026, 10, 17, 18, 0, 0)
        self.items = [
            dict(base, id=str(i), published=(start - timedelta(minutes=0 if same_published else i)).isoformat())
            for i in range(n)
        ]
        self.served = 0
        self.on_serve = None  # hook: called after each page with the page number

    def __call__(self, request):
        q = json.loads(request.url.params["query"])
        before = q["filters"].get("published", {}).get("before")
        rows = [
            it for it in self.items
            if before is None or (it["published"] is not None and it["published"] <= before)
        ]
        # Plain page numbers only for the null-published tail (the API caps them at 1000).
        assert q["page"] == 1 or before is None
        self.served += 1
        start = (q["page"] - 1) * 100
        page = rows[start:start + 100]
        if self.on_serve:
            self.on_serve(self.served)
        return httpx.Response(200, json={"results": page, "total": len(rows)})


def ids(listings) -> set[str]:
    return {l.id for l in listings}


def test_keyset_crawl_goes_past_the_1000_cap_and_dedups_the_inclusive_bound():
    feed = Feed(2650)
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert ids(listings) == {str(i) for i in range(2650)}
    befores = [q["filters"].get("published", {}).get("before") for q in fake.queries()]
    assert befores[0] is None and befores[-1] is None  # first page, and the final re-check of the top
    assert all(befores[1:-1])


def test_listing_dropping_out_mid_crawl_does_not_hide_another():
    feed = Feed(250)
    # After page 1 is served, a sit on it gets confirmed and leaves the feed.
    feed.on_serve = lambda n: n == 1 and feed.items.pop(10)
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert ids(listings) >= {str(i) for i in range(11, 250)}


def test_listing_bumped_mid_crawl_is_caught_by_the_final_look_at_the_top():
    feed = Feed(350)

    def bump(n):
        if n == 1:  # listing 300 gets new dates and jumps to the top
            item = next(it for it in feed.items if it["id"] == "300")
            feed.items.remove(item)
            feed.items.insert(0, dict(item, published="2026-10-17T19:00:00"))

    feed.on_serve = bump
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert "300" in ids(listings)


def test_small_filter_is_fetched_completely_even_on_a_page_one_tick():
    feed = Feed(250)  # ≤ SMALL_TOTAL
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert len(listings) == 250
    assert all(q["filters"].get("published", {}).get("before") for q in fake.queries()[1:])


def test_max_pages_one_on_a_big_filter_fetches_only_the_first_page():
    fake = Fake(Feed(500))
    fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert len(fake.requests) == 1


def test_a_stuck_cursor_fails_loudly_instead_of_returning_a_partial_crawl():
    fake = Fake(Feed(500, same_published=True))
    with pytest.raises(SourceError, match="stuck"):
        fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))


def test_an_api_ignoring_the_cursor_is_an_error_not_a_partial_crawl():
    feed = Feed(2650)

    def ignore_cursor(request):
        q = json.loads(request.url.params["query"])
        q["filters"].pop("published", None)
        stripped = request.url.copy_set_param("query", json.dumps(q))
        return feed(httpx.Request("GET", stripped))

    fake = Fake(ignore_cursor)
    with pytest.raises(SourceError, match="ignores filters.published"):
        fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))


def test_undated_listings_starting_on_a_full_first_page_are_fetched():
    feed = Feed(250)
    for item in feed.items[95:]:  # undated listings sort last; page 1 ends among them
        item["published"] = None
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert len(listings) == 250


def test_null_published_listings_are_fetched_from_the_tail_of_the_plain_feed():
    feed = Feed(450)
    feed.items.append(dict(feed.items.pop(5), published=None))  # sorts after everything
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert "5" in ids(listings) and len(listings) == 450
    assert fake.queries()[-1]["page"] == 5


def test_a_failing_admin_notice_does_not_fail_the_search():
    def handler(r):
        if str(r.url).startswith(API):
            ok = r.headers.get("api-version") == "2026-01-01"
            return httpx.Response(200, json=SEARCH) if ok else httpx.Response(406)
        return httpx.Response(200, text=PAGE_HTML.replace("2025-08-14", "2026-01-01"))

    fake = Fake(handler)

    async def broken_notice(text):
        raise RuntimeError("Telegram down")

    fake.ths._notice = broken_notice
    fake.ths.api_version = "2020-01-01"
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert len(listings) == len(SEARCH["results"])


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


def test_undated_listings_beyond_1000_are_found_per_country():
    # 1500 dated listings plus one undated in Portugal: plain pages can't reach
    # it, so the country facet (with vs without a cursor) points at Portugal.
    feed = Feed(1501)
    for it in feed.items[:-1]:
        it["location"] = dict(it["location"], countrySlug="spain")
    undated = feed.items[-1]
    undated.update(published=None, location=dict(undated["location"], countrySlug="portugal"))

    def handler(request):
        q = json.loads(request.url.params["query"])
        geo = q["filters"]["geoHierarchy"]
        before = q["filters"].get("published", {}).get("before")
        if q["facets"] == ["country"]:
            rows = [it for it in feed.items if before is None or it["published"]]
            counts = {}
            for it in rows:
                counts[it["location"]["countrySlug"]] = counts.get(it["location"]["countrySlug"], 0) + 1
            return httpx.Response(200, json={"results": [], "total": len(rows),
                "facets": {"country": [{"slug": k, "name": k, "count": v} for k, v in counts.items()]}})
        if geo.get("countrySlug") == "portugal":
            rows = [it for it in feed.items if it["location"]["countrySlug"] == "portugal"]
            start = (q["page"] - 1) * 100
            return httpx.Response(200, json={"results": rows[start:start + 100], "total": len(rows)})
        return feed(request)

    fake = Fake(handler)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert undated["id"] in ids(listings) and len(listings) == 1501


def test_page_one_tick_survives_a_broken_cursor():
    feed = Feed(250)  # small filter: a tick would normally fetch all of it

    def ignore_cursor(request):
        q = json.loads(request.url.params["query"])
        q["filters"].pop("published", None)
        return feed(httpx.Request("GET", request.url.copy_set_param("query", json.dumps(q))))

    fake = Fake(ignore_cursor)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=1))
    assert len(listings) == 100  # page 1 still delivers alerts


def test_listings_added_by_the_final_refresh_come_first():
    feed = Feed(350)

    def publish(n):
        if n == 2:
            feed.items.insert(0, dict(feed.items[0], id="new", published="2026-10-17T19:00:00"))

    feed.on_serve = publish
    fake = Fake(feed)
    listings = fake.run(lambda ths: ths.search(Filter("europe"), max_pages=None))
    assert listings[0].id == "new"


# --- by_start (/search) -------------------------------------------------------


def starting(i: int, start: date) -> dict:
    """A listing from the fixture with one open slot starting on `start`."""
    base = SEARCH["results"][1]
    slot = dict(base["assignments"][0], id=str(9000 + i), startDate=start.isoformat(),
                endDate=(start + timedelta(days=7)).isoformat())
    return dict(base, id=str(i), assignments=[slot])


class StartFeed:
    """THS sorted by start date, served by page number."""

    def __init__(self, starts: list[date]):
        self.items = [starting(i, s) for i, s in enumerate(starts)]

    def __call__(self, request):
        query = json.loads(request.url.params["query"])
        first = (query["page"] - 1) * query["resultsPerPage"]
        return httpx.Response(
            200, json={"results": self.items[first : first + query["resultsPerPage"]], "total": len(self.items)}
        )


def read(fake: Fake, flt: Filter, pages: int | None = None) -> list[list[str]]:
    """Ids per page, reading at most `pages` pages, like a caller that stops early."""

    async def go(ths):
        out = []
        async for page in ths.by_start(flt):
            out.append([l.id for l in page])
            if pages and len(out) == pages:
                break
        return out

    return fake.run(go)


def test_by_start_asks_for_the_start_date_sort_and_reads_only_what_the_caller_takes():
    starts = [date(2026, 11, 1) + timedelta(days=i // 50) for i in range(250)]
    fake = Fake(StartFeed(starts))
    got = read(fake, Filter("europe", country="portugal", date_from=date(2026, 11, 1)), pages=2)
    assert [len(p) for p in got] == [100, 100] and len(fake.requests) == 2
    query = fake.queries()[0]
    assert query["sort"] == [{"function": {"name": "SIT_START_DATE", "order": "asc"}}]
    assert query["filters"]["assignments"]["dateFrom"] == "2026-11-01"
    assert query["filters"]["geoHierarchy"] == {"countrySlug": "portugal"}
    assert [q["page"] for q in fake.queries()] == [1, 2]


def test_by_start_ends_with_the_feed():
    fake = Fake(StartFeed([date(2026, 11, 1)] * 130))
    assert [len(p) for p in read(fake, Filter("europe"))] == [100, 30]
    # From tomorrow (the clock says 17 Oct): THS mixes today's starts into tomorrow's.
    assert fake.queries()[0]["filters"]["assignments"]["dateFrom"] == "2026-10-18"


def test_by_start_fails_loudly_when_the_sort_is_ignored():
    starts = [date(2026, 12, 1)] * 100 + [date(2026, 11, 2)] * 10  # page 2 starts earlier
    fake = Fake(StartFeed(starts))
    with pytest.raises(SourceError, match="start-date sort"):
        read(fake, Filter("europe"))

    async def again(ths):
        with pytest.raises(SourceError):
            async for _ in ths.by_start(Filter("asia")):
                pass

    fake.run(again)
    assert len([n for n in fake.notices if "no longer sorts by start date" in n]) == 1  # once, not per search


def test_by_start_past_the_page_number_limit_is_too_broad():
    fake = Fake(StartFeed([date(2026, 11, 1)] * 1050))
    with pytest.raises(TooBroad, match="nearest 1000"):
        read(fake, Filter("europe"))
    assert len(fake.requests) == 10
