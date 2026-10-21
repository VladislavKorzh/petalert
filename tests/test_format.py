from __future__ import annotations

from datetime import date

from petalert.bot.format import (
    CAPTION_LIMIT,
    PAGE_SIZE,
    TEXT_LIMIT,
    city,
    country_name,
    country_tag,
    describe_filter,
    flag,
    render_alert,
    render_table,
    tg_len,
)
from petalert.models import Filter, Listing, Slot


def listing(title="Cosy flat", lid="1", pets=(("cat", 2), ("dog", 1))) -> Listing:
    return Listing(
        source="ths", id=lid, title=title, url=f"https://x/l/{lid}/", continent="europe",
        country="portugal", place="Espiche, Faro, Portugal", photo_url=None, pets=pets,
    )


def slot(i: int) -> Slot:
    return Slot(str(i), date(2026, 12, 1), date(2026, 12, 14), True, applicants=i % 3)


def test_alert_layout():
    text, markup = render_alert(listing(), [slot(1)], [3], "TrustedHousesitters")
    assert text == (
        "🏡 <b>Cosy flat</b>\n\n📍 Espiche, Faro, Portugal\n🐈 2 cats · 🐕 1 dog\n\n"
        "🆕 <b>New date:</b>\n📅 <b>1 Dec – 14 Dec 2026</b>\n      └ ⏳ 13 days · 👥 1 applicant\n\n"
        "🔔 Subscription #3 · TrustedHousesitters"
    )
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://x/l/1/" and button.text == "🔗 Open on TrustedHousesitters"


def test_unknown_pet_counts_are_plural_without_a_number():
    text, _ = render_alert(listing(pets=(("dog", None), ("fish", None))), [slot(1)], [1], "MindMyHouse")
    assert "🐕 dogs · 🐟 fish\n" in text


def test_zero_applicants_is_highlighted():
    text, _ = render_alert(listing(), [slot(3)], [1], "THS")
    assert "🟢 no applicants yet" in text


def test_caption_is_truncated_by_dropping_date_lines():
    text, _ = render_alert(listing(title="x" * 500), [slot(i) for i in range(30)], [1, 2], "THS")
    assert tg_len(text) <= CAPTION_LIMIT
    assert "more dates" in text and text.endswith("🔔 Subscriptions #1, #2 · THS")
    full, _ = render_alert(listing(title="x" * 500), [slot(i) for i in range(30)], [1], "THS", limit=TEXT_LIMIT)
    assert "more date" not in full and full.count("📅") == 30 and tg_len(full) <= TEXT_LIMIT


def test_one_hidden_date_is_singular():
    text, _ = render_alert(listing(title="x" * 145), [slot(i) for i in range(13)], [1], "THS")
    assert text.count("📅") == 12 and "… and 1 more date</i>" in text


def test_html_is_escaped():
    text, _ = render_alert(listing(title="<Cats & Dogs>"), [slot(1)], [1], "THS")
    assert "&lt;Cats &amp; Dogs&gt;" in text


def dated(i: str, start: str, end: str) -> Slot:
    return Slot(i, date.fromisoformat(start), date.fromisoformat(end), True)


SITES = {"ths": "TrustedHousesitters", "mmh": "MindMyHouse", "nmd": "Nomador"}


def placed(lid: str, place: str, code: str | None, source: str = "ths") -> Listing:
    return Listing(
        source=source, id=lid, title="t", url=f"https://x/l/{lid}/", continent="europe",
        country="x", place=place, photo_url=None, pets=(), country_code=code,
    )


def test_table_layout():
    items = [
        (placed("1", "Kungsbacka, Halland, Sweden", "se", "nmd"), [dated("a", "2026-11-02", "2026-11-09")], [1]),
        (
            placed("2", "Villefranche-sur-Mer, Provence, France", "fr"),
            [dated("b", "2026-12-09", "2027-01-09"), dated("c", "2027-02-05", "2027-02-20")],
            [1, 3],
        ),
        (placed("3", "<A & B>", None), [dated("d", "2027-03-01", "2027-06-01")], [1]),
    ]
    assert render_table(items, 0, sites=SITES) == (
        "🔔 <b>3 new sits</b> · Subscriptions #1, #3\n"
        "<pre> # Dates         D Where\n"
        " 1 02.11–09.11   7 🇸🇪 SE Kungsbacka\n"
        " 2 09.12–09.01  31 🇫🇷 FR Villefran…\n"
        "   05.02–20.02  15\n"
        " 3 01.03–01.06  92 📍 &lt;A &amp; B&gt;</pre>\n"
        "🌐 (T) TrustedHousesitters · (N) Nomador\n"  # the sites on the page, in /sites order
        "👇 Tap a number to open it on the site"
    )


def test_a_site_no_longer_known_gets_its_key_in_an_old_table():
    items = [(placed("1", "Lisbon", "pt", "xyz"), [dated("a", "2026-12-01", "2026-12-10")], [1])]
    text = render_table(items, 0, sites=SITES)
    assert "\n🌐 (X) xyz\n" in text


def test_a_search_table_has_its_own_header_and_its_cards_no_subscriptions():
    items = [(placed("1", "Lisbon", "pt"), [dated("a", "2026-12-01", "2026-12-10")], [])]
    assert render_table(items, 0, "🔎 <b>1 open sit</b>", sites=SITES).startswith("🔎 <b>1 open sit</b>\n<pre>")
    text, _ = render_alert(listing(), [slot(1), slot(2)], [], "MindMyHouse")
    assert "🗓 <b>Open dates:</b>" in text and text.endswith("\n\n🌐 MindMyHouse")
    assert "New date" not in text and "Subscription" not in text


def test_table_pages_and_many_dates():
    one = (placed("1", "Lisbon", "pt"), [dated(str(i), "2026-12-01", "2026-12-10") for i in range(5)], [2])
    items = [one] * (PAGE_SIZE + 3)
    first = render_table(items, 0, sites=SITES)
    assert first.startswith(f"🔔 <b>{PAGE_SIZE + 3} new sits</b> · Subscription #2\n")
    # A full page with two extra dates each would be over a message: one each fits.
    assert first.count("Lisbon") == PAGE_SIZE and first.count("+ 3 more") == PAGE_SIZE
    assert tg_len(first) <= TEXT_LIMIT
    second = render_table(items, 1, sites=SITES)
    assert second.count("Lisbon") == 3 and second.count("+ 2 more") == 3
    assert f"\n{PAGE_SIZE + 1:>2} 01.12" in second
    # A page of the widest rows, each with dozens of dates, fits too.
    wide = (placed("1", "<Frankfurt am Main>", "de"), [dated(str(i), "2026-12-01", "2027-03-31") for i in range(99)], [2])
    assert tg_len(render_table([wide] * PAGE_SIZE, 0, sites=SITES)) <= TEXT_LIMIT
    # Rows still fit a phone.
    rows = first.split("<pre>")[1].split("</pre>")[0].split("\n")
    assert max(len(r) for r in rows) <= 35


def test_city_is_cut_without_a_dangling_space():
    assert city(placed("1", "Frankfurt am Main, Hesse, Germany", "de")) == "Frankfurt…"
    assert city(placed("1", "Lisbon, Portugal", "pt")) == "Lisbon"
    assert city(placed("1", "", "pt")) == "X"  # no place: the country slug


def test_flag():
    assert flag("pt") == "🇵🇹" and flag("GB") == "🇬🇧"
    assert flag(None) == flag("") == flag("xyz") == flag("1a") == "📍"
    assert country_tag("pt") == "🇵🇹 PT" and country_tag("gb") == "🇬🇧 GB"
    assert country_tag(None) == country_tag("xyz") == "📍"


def test_describe_filter():
    flt = Filter(
        "europe", country="bosnia-and-herzegovina", date_from=date(2026, 11, 1),
        date_to=date(2027, 1, 31), min_days=7, max_days=21, pets=("cat", "dog"),
    )
    assert describe_filter(flt) == (
        "🌍 Where: Bosnia and Herzegovina (Europe)\n📅 Dates: 1 Nov 2026 – 31 Jan 2027\n"
        "⏳ Length: 7–21 days\n🐾 Pets: 🐈 cat or 🐕 dog"
    )
    assert describe_filter(Filter("europe", exclude_countries=("united-kingdom", "ireland"))).startswith(
        "🌍 Where: Europe, except Ireland, United Kingdom\n"
    )
    assert describe_filter(Filter("asia")) == (
        "🌍 Where: Asia, any country\n📅 Dates: any\n⏳ Length: any\n🐾 Pets: any"
    )


def test_country_name():
    assert country_name("united-kingdom") == "United Kingdom"
    assert country_name("isle-of-man") == "Isle of Man"


def test_tg_len_counts_like_telegram():
    # Tags don't count, entities count as one character, an emoji is 2 UTF-16 units.
    assert tg_len("<b>a&amp;b</b>") == 3
    assert tg_len("🏡 x") == 4
