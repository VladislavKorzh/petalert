"""/search: parsing the query, matching, and reading the sites only as far as needed."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from petalert import geo
from petalert.models import CONTINENTS, Filter, Listing, Slot
from petalert.search import MAX_RESULTS, Search, has_date, parse, run
from petalert.sources.base import SourceError, TooBroad
from petalert.sources.mmh import COUNTRIES as MMH_COUNTRIES

TODAY = date(2026, 10, 18)


def d(day: int, month: int, year: int = 2026) -> date:
    return date(year, month, day)


# --- parsing ------------------------------------------------------------------


def test_the_users_example():
    s = parse("eu(-uk), +-3, 30.11.2026 -", TODAY)
    assert s == Search(start_from=d(27, 11), start_to=None, continents=("europe",), exclude=("gb",))


@pytest.mark.parametrize(
    "text, start_from, start_to",
    [
        ("eu 30.11.2026", d(30, 11), d(30, 11)),  # no margin: that day
        ("eu ±3 30.11.2026", d(27, 11), d(3, 12)),
        ("eu(-uk), +5, 30.11.2026", d(30, 11), d(5, 12)),  # only after
        ("eu -2 30.11.2026", d(28, 11), d(30, 11)),  # only before
        ("eu +5 -2 30.11.2026", d(28, 11), d(5, 12)),
        ("eu +5 days 30.11.2026 -", d(30, 11), None),
        ("eu 1.12 - 20.12 +5", d(1, 12), d(25, 12)),
        ("eu 1.12 -20.12", d(1, 12), d(20, 12)),  # a range, not a margin of 20
        ("eu 2026-11-30 +2", d(30, 11), d(2, 12)),  # the ISO date's dashes stay dates
        ("eu +/-3 2026-11-30", d(27, 11), d(3, 12)),
        ("eu 30/11/2026 +- 3 days", d(27, 11), d(3, 12)),
        ("eu, -+2, 30.11.2026 –", d(28, 11), None),
        ("eu 30.11.2026-", d(30, 11), None),
        ("eu 30.11 - 20.12, ±3", d(27, 11), d(23, 12)),
        ("eu 1.12.2026 20.12.2026", d(1, 12), d(20, 12)),
        ("eu 20.12 - 5.01", d(20, 12), d(5, 1, 2027)),  # no year: the next one to come
        ("eu 15.10", d(15, 10, 2027), d(15, 10, 2027)),  # just passed, no margin: next year's
        ("eu ±3 17.10", d(19, 10), d(20, 10)),  # just passed, the margin reaches tomorrow
        ("eu 17.10 -", d(19, 10), None),
        ("eu 1.10 - 31.10", d(19, 10), d(31, 10)),  # a range already under way
        ("eu 1.05 - 1.06", d(1, 5, 2027), d(1, 6, 2027)),  # over this year: next year's
        ("eu 1.12 to 20.12", d(1, 12), d(20, 12)),
        ("eu 30.11. -", d(30, 11), None),  # a trailing dot
        ("eu 1.12. - 20.12.", d(1, 12), d(20, 12)),
        ("eu(-uk), 30.11.2026, -", d(30, 11), None),
        ("eu", d(19, 10), None),  # no dates: from tomorrow on
        ("eu ±5 20.10.2026", d(19, 10), d(25, 10)),  # never before tomorrow
    ],
)
def test_dates_are_a_range_of_starts(text, start_from, start_to):
    s = parse(text, TODAY)
    assert (s.start_from, s.start_to) == (start_from, start_to)


@pytest.mark.parametrize(
    "text, continents, countries, exclude",
    [
        ("pt, es, 30.11.2026", (), ("pt", "es"), ()),
        ("PT ES 30.11.2026", (), ("pt", "es"), ()),
        ("united kingdom, costa rica 30.11.2026", (), ("gb", "cr"), ()),
        ("eu -uk -ie 30.11.2026", ("europe",), (), ("gb", "ie")),
        ("eu(-uk, -ireland), north america 30.11.2026", ("europe", "north-america"), (), ("gb", "ie")),
        ("30.11.2026 -, eu(-uk)", ("europe",), (), ("gb",)),  # dash, then a place
        ("Europe, Curaçao 30.11.2026", ("europe",), ("cw",), ()),
        ("30.11.2026", tuple(CONTINENTS), (), ()),  # anywhere
        # after a range: a place, not an exclusion
        ("1.12.2026 - 20.12.2026 pt", (), ("pt",), ()),
        ("1.12 – 20.12 united kingdom", (), ("gb",), ()),
        ("1.12 - 20.12 eu", ("europe",), (), ()),
        ("pt 1.12 to 20.12", (), ("pt",), ()),  # "to" is not Tonga
        ("eu 30.11.-uk", ("europe",), (), ("gb",)),
        # several words without commas
        ("eu north america 30.11.2026", ("europe", "north-america"), (), ()),
        ("pt united kingdom 30.11.2026", (), ("pt", "gb"), ()),
        ("north america -us 30.11.2026", ("north-america",), (), ("us",)),
    ],
)
def test_places(text, continents, countries, exclude):
    s = parse(text, TODAY)
    assert (s.continents, s.countries, s.exclude) == (continents, countries, exclude)


def test_open_end_dash_is_not_an_exclusion():
    s = parse("eu 30.11.2026 -uk", TODAY)
    assert s.start_to == d(30, 11) and s.exclude == ("gb",)


@pytest.mark.parametrize(
    "text, error",
    [
        ("narnia 30.11.2026", "Unknown place 'narnia'"),
        ("eu(-asia) 30.11.2026", "Only countries can be left out"),
        ("eu 30.11.26", "Can't read '30.11.26'"),
        ("eu 31.11.2026", "doesn't exist"),
        ("eu 20.12.2026 - 1.12.2026", "must not be before"),
        ("eu 1.12.2026 - 20.12.2026 - 1.01.2027", "At most two dates"),
        ("eu 1.10.2026 - 10.10.2026", "have passed"),
        ("eu 18.10.2026", "have passed"),  # today
        ("eu ±31 30.11.2026", "at most 30"),
        ("eu +31 30.11.2026", "at most 30"),
        ("eu ±3 ±4 30.11.2026", "One margin per side"),
        ("eu ±3 +2 30.11.2026", "One margin per side"),
        ("- 30.11.2026 pt", "lone '-'"),  # not "everywhere except Portugal"
        ("eu - uk 30.11.2026", "lone '-'"),
        ("eu 30.11.2026 -uk -", "lone '-'"),
        ("eu ±3 1.1.0001 -", "Check the year"),
        ("eu 31.12.9999", "Check the year"),
        ("eu 32.13", "doesn't exist"),
    ],
)
def test_errors_are_for_the_user(text, error):
    with pytest.raises(ValueError, match=error):
        parse(text, TODAY)


def test_plain_text_counts_as_a_search_only_with_a_date():
    assert has_date("eu(-uk) ±3 30.11.2026") and has_date("pt 30.11")
    assert not has_date("hello") and not has_date("pt es")


# --- matching -----------------------------------------------------------------


def listing(lid: str, code: str | None, *starts: date, source="ths", continent="europe", is_open=True) -> Listing:
    return Listing(
        source=source, id=lid, title=f"Sit {lid}", url=f"https://x/{lid}", continent=continent,
        country=code or "region", place="Somewhere", photo_url=None, pets=(),
        slots=tuple(Slot(f"{lid}-{i}", s, date(s.year + 1, 1, 1), is_open) for i, s in enumerate(starts)),
        country_code=code,
    )


def test_matching_goes_by_country_code_and_exclusions_win():
    s = parse("eu(-uk), us 30.11.2026", TODAY)
    assert s.matches(listing("1", "pt", d(30, 11)))
    assert not s.matches(listing("2", "gb", d(30, 11)))
    assert s.matches(listing("3", "us", d(30, 11), continent="north-america"))
    assert not s.matches(listing("4", "ca", d(30, 11), continent="north-america"))
    assert s.matches(listing("5", None, d(30, 11), source="mmh"))  # a region: only its continent finds it


def test_slots_are_the_open_ones_starting_in_range():
    s = parse("eu ±3 30.11.2026", TODAY)
    sit = listing("1", "pt", d(5, 12), d(27, 11), d(3, 12), d(26, 11))
    assert [x.start for x in s.slots(sit)] == [d(27, 11), d(3, 12)]
    assert s.slots(listing("2", "pt", d(30, 11), is_open=False)) == []


def test_scopes_ask_for_continents_and_the_countries_outside_them():
    s = parse("eu, pt, us, -gb 30.11.2026", TODAY)
    assert s.scopes() == [
        Filter("europe", date_from=d(30, 11)),
        Filter("north-america", country="united-states", date_from=d(30, 11)),
    ]


# --- reading the sites ----------------------------------------------------------


class Site:
    """A source whose by_start serves fixed pages and counts what was read."""

    def __init__(self, key: str, pages: list[list[Listing]], *, error: Exception | None = None):
        self.key = key
        self.title = key.upper()
        self.pages = pages
        self.error = error
        self.read = 0
        self.scopes: list[Filter] = []

    async def by_start(self, flt):
        self.scopes.append(flt)
        for page in self.pages:
            self.read += 1
            yield page
        if self.error:
            raise self.error


def go(search: Search, *sites):
    return asyncio.run(run(search, list(sites)))


def test_reading_stops_once_past_the_range():
    pages = [
        [listing("1", "pt", d(27, 11)), listing("2", "gb", d(28, 11))],
        [listing("3", "fr", d(2, 12)), listing("4", "fr", d(5, 12))],  # 5 Dec: past 3 Dec
        [listing("5", "fr", d(6, 12))],
    ]
    site = Site("ths", pages)
    found = go(parse("eu(-uk) ±3 30.11.2026", TODAY), site)
    assert site.read == 2
    assert [l.id for l, _ in found.items] == ["1", "3"] and not found.more and not found.problems


def test_results_stop_at_the_cap_nearest_first():
    def day(i: int) -> date:
        return date.fromordinal(d(1, 12).toordinal() + i // 10)

    # Two sites, 80 sits each, interleaved by start: the 100 nearest of both are kept.
    ths = Site("ths", [[listing(f"t{i}", "pt", day(2 * i)) for i in range(80)]])
    mmh = Site("mmh", [[listing(f"m{i}", "pt", day(2 * i + 1), source="mmh") for i in range(80)]])
    found = go(parse("eu 30.11.2026 -", TODAY), ths, mmh)
    starts = [slots[0].start for _, slots in found.items]
    assert len(found.items) == MAX_RESULTS and found.more and starts == sorted(starts)
    assert starts[-1] == day(MAX_RESULTS - 1)  # nothing nearer was dropped


def test_reading_a_site_stops_at_the_cap():
    page = [listing(str(i), "pt", d(1, 12)) for i in range(MAX_RESULTS + 20)]
    site = Site("ths", [page, [listing("late", "pt", d(2, 12))]])
    found = go(parse("eu 30.11.2026 -", TODAY), site)
    assert len(found.items) == MAX_RESULTS and found.more and site.read == 1


def test_sites_merge_by_start_and_a_listing_counts_once():
    ths = Site("ths", [[listing("1", "pt", d(29, 11)), listing("2", "pt", d(2, 12))]])
    mmh = Site("mmh", [[listing("9", "es", d(1, 12), source="mmh"), listing("9", "es", d(1, 12), source="mmh")]])
    found = go(parse("pt, es ±3 30.11.2026", TODAY), ths, mmh)
    assert [(l.source, l.id) for l, _ in found.items] == [("ths", "1"), ("mmh", "9"), ("ths", "2")]
    assert len(ths.scopes) == 2  # a request per country


def test_a_failing_site_leaves_the_others_and_says_so():
    ok = Site("ths", [[listing("1", "pt", d(30, 11))]])
    broken = Site("mmh", [], error=SourceError("HTTP 502"))
    found = go(parse("eu 30.11.2026", TODAY), ok, broken)
    assert [l.id for l, _ in found.items] == ["1"]
    assert found.problems == ["MMH failed — results may be incomplete, try again later"]


def test_too_broad_keeps_what_was_read_and_tells_why():
    site = Site("ths", [[listing("1", "pt", d(30, 11))]], error=TooBroad("only the nearest 1000 listings could be checked"))
    found = go(parse("pt 30.11.2026 -", TODAY), site)
    assert [l.id for l, _ in found.items] == ["1"]
    assert found.problems == ["THS: only the nearest 1000 listings could be checked"]


# --- the country table ----------------------------------------------------------


def test_geo_agrees_with_mindmyhouse_on_codes_and_continents():
    for name, (continent, code) in MMH_COUNTRIES.items():
        if code:
            assert geo.continent(code) == continent, name


def test_geo_slugs_are_unique_and_aliases_resolve():
    assert len({geo.slug(c) for c in geo.COUNTRIES}) == len(geo.COUNTRIES)
    assert all(code in geo.COUNTRIES for code in geo.ALIASES.values())
    assert all(continent in CONTINENTS for _, continent in geo.COUNTRIES.values())
    assert geo.lookup_place("UK") == ("country", "gb")
    assert geo.lookup_place("eu") == ("continent", "europe")
    assert geo.slug("cz") == "czech-republic"  # THS's slug
