from __future__ import annotations

from datetime import date

from petalert.models import Filter, Listing, Slot, listing_matches, matching_slots

TODAY = date(2026, 10, 17)


def slot(sid: str, start: str, end: str, is_open: bool = True) -> Slot:
    return Slot(sid, date.fromisoformat(start), date.fromisoformat(end), is_open)


def listing(*slots: Slot, continent="europe", country="portugal", pets=(("cat", 1),)) -> Listing:
    return Listing(
        source="ths", id="1", title="t", url="u", continent=continent, country=country,
        place="p", photo_url=None, pets=pets, slots=slots,
    )


def ids(slots) -> list[str]:
    return [s.id for s in slots]


def test_closed_and_already_started_slots_are_dropped():
    lst = listing(
        slot("past", "2026-10-10", "2026-10-20"),
        slot("closed", "2026-11-01", "2026-11-10", is_open=False),
        slot("ok", "2026-11-01", "2026-11-10"),
        slot("today", "2026-10-17", "2026-10-20"),
    )
    assert ids(matching_slots(lst, Filter("europe"), TODAY)) == ["ok", "today"]


def test_slot_must_fit_entirely_inside_the_date_window():
    flt = Filter("europe", date_from=date(2026, 11, 1), date_to=date(2026, 11, 30))
    lst = listing(
        slot("inside", "2026-11-01", "2026-11-30"),
        slot("starts_before", "2026-10-30", "2026-11-05"),
        slot("ends_after", "2026-11-25", "2026-12-02"),
    )
    assert ids(matching_slots(lst, flt, TODAY)) == ["inside"]


def test_duration_is_end_minus_start_with_inclusive_bounds():
    lst = listing(slot("31", "2026-12-09", "2027-01-09"), slot("7", "2026-11-01", "2026-11-08"))
    assert lst.slots[0].days == 31
    assert ids(matching_slots(lst, Filter("europe", min_days=31), TODAY)) == ["31"]
    assert ids(matching_slots(lst, Filter("europe", min_days=32), TODAY)) == []
    assert ids(matching_slots(lst, Filter("europe", max_days=7), TODAY)) == ["7"]


def test_geo_and_pets_are_checked_locally():
    lst = listing(pets=(("dog", 2), ("horse", 1)))
    assert listing_matches(lst, Filter("europe"))
    assert not listing_matches(lst, Filter("asia"))
    assert not listing_matches(lst, Filter("europe", country="spain"))
    # OR: one pet from the list is enough.
    assert listing_matches(lst, Filter("europe", pets=("cat", "dog")))
    assert not listing_matches(lst, Filter("europe", pets=("cat",)))


def test_filter_json_is_canonical():
    a = Filter("europe", pets=("dog", "cat"), date_from=date(2026, 11, 1))
    b = Filter("europe", pets=("cat", "dog"), date_from=date(2026, 11, 1))
    assert a.to_json() == b.to_json()
    assert Filter.from_json(a.to_json()) == Filter("europe", pets=("cat", "dog"), date_from=date(2026, 11, 1))


def test_excluded_countries_are_dropped():
    flt = Filter("europe", exclude_countries=("united-kingdom",))
    assert not listing_matches(listing(country="united-kingdom"), flt)
    assert listing_matches(listing(country="portugal"), flt)


def test_exclusions_keep_old_filter_json_stable():
    # Subscriptions saved before exclusions existed keep the same group key.
    assert "exclude_countries" not in Filter("europe").to_json()
    flt = Filter("europe", exclude_countries=("ireland", "united-kingdom"))
    assert Filter.from_json(flt.to_json()) == flt
    assert flt.to_json() == Filter("europe", exclude_countries=("united-kingdom", "ireland")).to_json()


def test_news_means_published_or_created_after_the_baseline():
    from datetime import UTC, datetime

    from petalert.models import appeared_after

    since = datetime(2026, 10, 17, 16, 27, tzinfo=UTC)
    old_pub = datetime(2026, 10, 8, 8, 23, tzinfo=UTC)
    new_pub = datetime(2026, 10, 17, 19, 0, tzinfo=UTC)

    def lst(published):
        return Listing(source="ths", id="1", title="t", url="u", continent="europe", country="se",
                       place="p", photo_url=None, pets=(), published=published)

    old_slot = Slot("2111592", date(2026, 12, 12), date(2027, 1, 10), True, seq=2111592)
    new_slot = Slot("2131900", date(2026, 12, 12), date(2027, 1, 10), True, seq=2131900)
    # The "Stan in Sweden" case: an old slot on an old listing, merely unseen.
    assert not appeared_after(lst(old_pub), old_slot, since, 2131639)
    # New dates on an old listing, without a re-publish.
    assert appeared_after(lst(old_pub), new_slot, since, 2131639)
    # A listing (re)published after the baseline, even with an older slot.
    assert appeared_after(lst(new_pub), old_slot, since, 2131639)
    # Nothing known: fall back to "unseen = news".
    assert appeared_after(lst(None), Slot("x", date(2026, 12, 1), date(2026, 12, 9), True), since, None)
    assert appeared_after(lst(old_pub), old_slot, None, None)
