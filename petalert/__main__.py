"""Entry point.

    python -m petalert                        # bot + worker (what systemd runs)
    python -m petalert codes generate 100     # a new pack of invite codes with deep links
    python -m petalert codes list             # unused codes
    python -m petalert rebaseline mmh         # re-index a site quietly, see rebaseline() below
    python -m petalert search "eu(-uk) ±3 30.11.2026"   # what /search finds, without Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import logging
import sys
from datetime import date

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .bot import build_dispatcher
from .bot.notify import TelegramOutbox
from .config import Settings
from .db import Store, format_code
from .geocode import Geocoder
from .http import Http
from .models import distance_km
from .scheduler import Worker
from .search import locate, parse, run
from .sources import REGISTRY, build_sources

log = logging.getLogger("petalert")

COMMANDS = [
    BotCommand(command="new", description="Create a subscription"),
    BotCommand(command="list", description="Your subscriptions"),
    BotCommand(command="search", description="Find open sits by start date"),
    BotCommand(command="sites", description="Sites you get alerts from"),
    BotCommand(command="cancel", description="Abort the current dialog"),
    BotCommand(command="help", description="How it works"),
]


async def run_bot(settings: Settings) -> None:
    store = Store(settings.db_path)
    http = Http(settings.proxy_url, settings.proxy_mode)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def admin(text: str) -> None:
        log.warning("admin: %s", text)
        if settings.admin_chat_id is not None:
            await bot.send_message(settings.admin_chat_id, html.escape(text))

    sources = build_sources(http, settings, notice=admin)
    outbox = TelegramOutbox(bot, {key: s.title for key, s in sources.items()}, store)
    worker = Worker(
        store, sources, outbox, admin, settings.poll_interval, full_every=settings.full_crawl_every
    )

    dp = build_dispatcher(store, sources, worker, settings.admin_user_ids, Geocoder(http))

    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    log.info("bot @%s, polling every %s, proxy: %s", me.username, settings.poll_interval,
             settings.proxy_mode if settings.proxy_url else "none")
    worker_task = asyncio.create_task(worker.run(), name="worker")
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await http.aclose()
        store.close()


def rebaseline(settings: Settings, source: str) -> None:
    """On the next tick, every subscription quietly marks what matches on
    `source` right now as seen (no messages; its baseline time stays).

    For a change on our side that makes old listings matchable for the first
    time (say, a country added to a source's table): without it they would be
    alerted as new. The first tick after a restart comes right away, so run
    this with the bot stopped, after updating the code and before starting it.
    New sits that appeared since the last tick are marked seen too.
    """
    store = Store(settings.db_path)
    try:
        n = store.reindex(source)
        print(f"{n} subscriptions will re-index {source} quietly on the next tick")
    finally:
        store.close()


def search(settings: Settings, text: str) -> None:
    """/search on every site, printed — no Telegram, no database."""
    from .bot.format import city
    from .bot.search import describe

    try:
        query = parse(text, date.today())
    except ValueError as exc:
        sys.exit(str(exc))

    async def go():
        http = Http(settings.proxy_url, settings.proxy_mode)
        try:
            located = await locate(query, Geocoder(http))
            return located, await run(located, list(build_sources(http, settings).values()))
        finally:
            await http.aclose()

    try:
        query, found = asyncio.run(go())
    except ValueError as exc:  # no such town
        sys.exit(str(exc))
    where, when = describe(query)
    n = len(found.items)
    print(f"{n} open sit{'' if n == 1 else 's'} · start {when} · {where}" + (" · more exist" if found.more else ""))
    for problem in found.problems:
        print(f"! {problem}")
    for listing, slots in found.items:
        dates = ", ".join(f"{s.start:%d.%m.%Y}–{s.end:%d.%m.%Y} ({s.days} d)" for s in slots)
        away = ""
        if query.near and listing.lat is not None and listing.lon is not None:
            away = f" {distance_km(query.near.lat, query.near.lon, listing.lat, listing.lon):.0f} km"
        print(f"{dates}  {(listing.country_code or '--').upper()} {city(listing)}{away}  [{listing.source}] {listing.url}")


async def _bot_username(token: str) -> str:
    async with Bot(token) as bot:
        return (await bot.get_me()).username


def codes(args: argparse.Namespace, settings: Settings) -> None:
    store = Store(settings.db_path)
    try:
        if args.action == "generate":
            new = store.generate_codes(args.n)
            username = asyncio.run(_bot_username(settings.bot_token)) if settings.bot_token else None
            for code in new:
                link = f"  https://t.me/{username}?start={code}" if username else ""
                print(f"{format_code(code)}{link}")
            print(f"--- {len(new)} new codes; unused in total: {len(store.unused_codes())}", file=sys.stderr)
            # Codes only work in the DB the running bot uses — on the server, not a local copy.
            print(f"--- written to {settings.db_path}", file=sys.stderr)
        else:
            unused = store.unused_codes()
            for code in unused:
                print(format_code(code))
            print(f"--- unused: {len(unused)}", file=sys.stderr)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m petalert")
    sub = parser.add_subparsers(dest="cmd")
    p_codes = sub.add_parser("codes", help="invite codes")
    codes_sub = p_codes.add_subparsers(dest="action", required=True)
    gen = codes_sub.add_parser("generate")
    gen.add_argument("n", type=int, nargs="?", default=100)
    codes_sub.add_parser("list")
    rb = sub.add_parser("rebaseline", help="re-index a site quietly; run with the bot stopped")
    rb.add_argument("source", choices=list(REGISTRY))
    p_search = sub.add_parser("search", help="what /search finds, without Telegram")
    p_search.add_argument("query")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    settings = Settings.load()

    if args.cmd == "codes":
        codes(args, settings)
        return
    if args.cmd == "rebaseline":
        rebaseline(settings, args.source)
        return
    if args.cmd == "search":
        search(settings, args.query)
        return
    if not settings.bot_token:
        sys.exit("BOT_TOKEN is not set — see .env.example")
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
