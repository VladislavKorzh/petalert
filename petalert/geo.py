"""Countries for /search: ISO code → English name and continent.

Continents follow GeoNames, as THS does (verified with its country facets on
2026-10-18: Turkey, Armenia and Kazakhstan are in Asia; Cyprus, Gibraltar, the
Isle of Man and Jersey in Europe; Mexico and Central America in North
America). Names are the common English ones THS uses, so a name's slug is the
THS country slug ("Czech Republic" → `czech-republic`); test_live_ths.py checks
both against the live facets.
"""

from __future__ import annotations

import re
import unicodedata

_TABLE = {
    "europe": (
        "ad Andorra|al Albania|at Austria|ax Aland Islands|ba Bosnia and Herzegovina|be Belgium|"
        "bg Bulgaria|by Belarus|ch Switzerland|cy Cyprus|cz Czech Republic|de Germany|dk Denmark|"
        "ee Estonia|es Spain|fi Finland|fo Faroe Islands|fr France|gb United Kingdom|gg Guernsey|"
        "gi Gibraltar|gr Greece|hr Croatia|hu Hungary|ie Ireland|im Isle of Man|is Iceland|it Italy|"
        "je Jersey|li Liechtenstein|lt Lithuania|lu Luxembourg|lv Latvia|mc Monaco|md Moldova|"
        "me Montenegro|mk North Macedonia|mt Malta|nl Netherlands|no Norway|pl Poland|pt Portugal|"
        "ro Romania|rs Serbia|ru Russia|se Sweden|si Slovenia|sj Svalbard and Jan Mayen|sk Slovakia|"
        "sm San Marino|ua Ukraine|va Vatican City|xk Kosovo"
    ),
    "asia": (
        "ae United Arab Emirates|af Afghanistan|am Armenia|az Azerbaijan|bd Bangladesh|bh Bahrain|"
        "bn Brunei|bt Bhutan|cn China|ge Georgia|hk Hong Kong|id Indonesia|il Israel|in India|"
        "iq Iraq|ir Iran|jo Jordan|jp Japan|kg Kyrgyzstan|kh Cambodia|kp North Korea|kr South Korea|"
        "kw Kuwait|kz Kazakhstan|la Laos|lb Lebanon|lk Sri Lanka|mm Myanmar|mn Mongolia|mo Macao|"
        "mv Maldives|my Malaysia|np Nepal|om Oman|ph Philippines|pk Pakistan|ps Palestine|qa Qatar|"
        "sa Saudi Arabia|sg Singapore|sy Syria|th Thailand|tj Tajikistan|tl Timor-Leste|"
        "tm Turkmenistan|tr Turkey|tw Taiwan|uz Uzbekistan|vn Vietnam|ye Yemen"
    ),
    "africa": (
        "ao Angola|bf Burkina Faso|bi Burundi|bj Benin|bw Botswana|cd Democratic Republic of the Congo|"
        "cf Central African Republic|cg Republic of the Congo|ci Ivory Coast|cm Cameroon|cv Cape Verde|"
        "dj Djibouti|dz Algeria|eg Egypt|eh Western Sahara|er Eritrea|et Ethiopia|ga Gabon|gh Ghana|"
        "gm Gambia|gn Guinea|gq Equatorial Guinea|gw Guinea-Bissau|ke Kenya|km Comoros|lr Liberia|"
        "ls Lesotho|ly Libya|ma Morocco|mg Madagascar|ml Mali|mr Mauritania|mu Mauritius|mw Malawi|"
        "mz Mozambique|na Namibia|ne Niger|ng Nigeria|re Reunion|rw Rwanda|sc Seychelles|sd Sudan|"
        "sh Saint Helena|sl Sierra Leone|sn Senegal|so Somalia|ss South Sudan|"
        "st Sao Tome and Principe|sz Eswatini|td Chad|tg Togo|tn Tunisia|tz Tanzania|ug Uganda|"
        "yt Mayotte|za South Africa|zm Zambia|zw Zimbabwe"
    ),
    "oceania": (
        "as American Samoa|au Australia|ck Cook Islands|fj Fiji|fm Micronesia|gu Guam|ki Kiribati|"
        "mh Marshall Islands|mp Northern Mariana Islands|nc New Caledonia|nf Norfolk Island|nr Nauru|"
        "nu Niue|nz New Zealand|pf French Polynesia|pg Papua New Guinea|pn Pitcairn|pw Palau|"
        "sb Solomon Islands|tk Tokelau|to Tonga|tv Tuvalu|vu Vanuatu|wf Wallis and Futuna|ws Samoa"
    ),
    "north-america": (
        "ag Antigua and Barbuda|ai Anguilla|aw Aruba|bb Barbados|bl Saint Barthelemy|bm Bermuda|"
        "bq Bonaire, Saint Eustatius and Saba|bs Bahamas|bz Belize|ca Canada|cr Costa Rica|cu Cuba|"
        "cw Curacao|dm Dominica|do Dominican Republic|gd Grenada|gl Greenland|gp Guadeloupe|"
        "gt Guatemala|hn Honduras|ht Haiti|jm Jamaica|kn Saint Kitts and Nevis|ky Cayman Islands|"
        "lc Saint Lucia|mf Saint Martin|mq Martinique|ms Montserrat|mx Mexico|ni Nicaragua|pa Panama|"
        "pm Saint Pierre and Miquelon|pr Puerto Rico|sv El Salvador|sx Sint Maarten|"
        "tc Turks and Caicos Islands|tt Trinidad and Tobago|us United States|"
        "vc Saint Vincent and the Grenadines|vg British Virgin Islands|vi U.S. Virgin Islands"
    ),
    "south-america": (
        "ar Argentina|bo Bolivia|br Brazil|cl Chile|co Colombia|ec Ecuador|fk Falkland Islands|"
        "gf French Guiana|gy Guyana|pe Peru|py Paraguay|sr Suriname|uy Uruguay|ve Venezuela"
    ),
}

# ISO 3166 alpha-2 (lower case) → (name, continent slug as in models.CONTINENTS)
COUNTRIES: dict[str, tuple[str, str]] = {
    entry[:2]: (entry[3:], continent)
    for continent, entries in _TABLE.items()
    for entry in entries.split("|")
}

CONTINENT_ALIASES = {
    "eu": "europe", "europe": "europe", "asia": "asia", "africa": "africa", "oceania": "oceania",
    "north-america": "north-america", "south-america": "south-america",
}

# Other ways people write a country, in key() form.
ALIASES = {
    "uk": "gb", "great-britain": "gb", "britain": "gb", "england": "gb", "scotland": "gb",
    "wales": "gb", "northern-ireland": "gb", "usa": "us", "united-states-of-america": "us",
    "czechia": "cz", "holland": "nl", "the-netherlands": "nl", "uae": "ae", "korea": "kr",
    "turkiye": "tr", "macedonia": "mk", "cote-d-ivoire": "ci", "vatican": "va",
}


def key(text: str) -> str:
    """`Curaçao` → `curacao`, `United Kingdom` → `united-kingdom`: the slug form
    names are compared in (and THS builds its country slugs in)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


_BY_NAME = {key(name): code for code, (name, _) in COUNTRIES.items()} | ALIASES


def lookup_place(text: str) -> tuple[str, str] | None:
    """("continent", slug), ("country", iso code) or None if unknown."""
    k = key(text)
    if k in CONTINENT_ALIASES:
        return "continent", CONTINENT_ALIASES[k]
    if k in COUNTRIES:
        return "country", k
    if k in _BY_NAME:
        return "country", _BY_NAME[k]
    return None


def name(code: str) -> str:
    return COUNTRIES[code][0]


def continent(code: str) -> str:
    return COUNTRIES[code][1]


def slug(code: str) -> str:
    return key(name(code))
