"""Debug CLI for sources: see what the bot sees, without Telegram.

    python -m petalert.sources countries --continent europe
    python -m petalert.sources search --continent europe --country portugal --min-days 14
    python -m petalert.sources --source mmh search --continent europe --exclude united-kingdom
    python -m petalert.sources dump --continent europe --country portugal > tests/fixtures/x.json

`search` prints only the slots that pass the local filter — exactly what would
go into alerts. `dump` prints the raw THS response, for fixtures (THS only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date

from ..config import Settings
from ..http import Http
from ..models import CONTINENTS, PETS, Filter, listing_matches, matching_slots
from . import REGISTRY, build_sources
from .ths import PER_PAGE, TrustedHousesitters, build_query


def _filter(args: argparse.Namespace) -> Filter:
    pets = tuple(p.strip() for p in args.pets.split(",")) if args.pets else ()
    unknown = set(pets) - set(PETS)
    if unknown:
        sys.exit(f"unknown pets: {sorted(unknown)}; allowed: {', '.join(PETS)}")
    return Filter(
        continent=args.continent,
        country=args.country,
        date_from=date.fromisoformat(args.date_from) if args.date_from else None,
        date_to=date.fromisoformat(args.date_to) if args.date_to else None,
        min_days=args.min_days,
        max_days=args.max_days,
        pets=pets,
        exclude_countries=tuple(c.strip() for c in args.exclude.split(",")) if args.exclude else (),
    )


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.load()
    http = Http(settings.proxy_url, settings.proxy_mode, min_interval=1.0)
    source = build_sources(http, settings)[args.source]
    try:
        if args.cmd == "countries":
            for c in await source.countries(args.continent):
                print(f"{c.slug:30} {c.count:5}  {c.name}")
        elif args.cmd == "dump":
            if not isinstance(source, TrustedHousesitters):
                sys.exit("dump only supports THS for now")
            query = build_query(_filter(args), date.today(), facets=tuple(args.facets or ()))
            query["resultsPerPage"] = args.per_page
            print(json.dumps(await source._query(query), ensure_ascii=False, indent=1))
        else:
            flt = _filter(args)
            today = date.today()
            listings = await source.search(flt, max_pages=args.pages or None)
            shown = 0
            for listing in listings:
                slots = matching_slots(listing, flt, today) if listing_matches(listing, flt) else []
                if not slots:
                    continue
                shown += 1
                dates = ", ".join(f"{s.start}..{s.end} ({s.days}d)" for s in slots)
                print(f"{listing.id:>8} {listing.place} | {listing.title[:50]} | {dates}")
            print(f"--- {shown} of {len(listings)} listings have matching open dates")
    finally:
        await http.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m petalert.sources")
    parser.add_argument("--source", default="ths", choices=list(REGISTRY))
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("search", "dump", "countries"):
        p = sub.add_parser(name)
        p.add_argument("--continent", required=True, choices=list(CONTINENTS))
        if name == "countries":
            continue
        p.add_argument("--country")
        p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
        p.add_argument("--min-days", type=int)
        p.add_argument("--max-days", type=int)
        p.add_argument("--pets", help="comma-separated: cat,dog")
        p.add_argument("--exclude", help="comma-separated country slugs: united-kingdom,ireland")
        if name == "search":
            p.add_argument("--pages", type=int, default=1, help="0 = all pages")
        else:
            p.add_argument("--per-page", type=int, default=PER_PAGE)
            p.add_argument("--facets", nargs="*")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
