"""Entry point.

    python -m petalert                        # bot + worker (what systemd runs)
    python -m petalert codes generate 100     # a new pack of invite codes with deep links
    python -m petalert codes list             # unused codes
    python -m petalert rebaseline             # one-off upgrade step, see rebaseline() below
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .bot import build_dispatcher
from .bot.notify import TelegramOutbox
from .config import Settings
from .db import Store, format_code, utcnow
from .http import Http
from .models import Filter, listing_matches, matching_slots, slot_key
from .scheduler import Worker
from .sources import build_sources

log = logging.getLogger("petalert")

COMMANDS = [
    BotCommand(command="new", description="Create a subscription"),
    BotCommand(command="list", description="Your subscriptions"),
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

    dp = build_dispatcher(store, sources, worker, settings.admin_user_ids)

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


async def rebaseline(settings: Settings, keep_newest: int) -> None:
    """Silently mark as seen what a subscription could never see before.

    Run it once, with the bot stopped, after a change that lets a source reach
    listings it couldn't reach before (here: the cursor crawl past the old
    1000-result cap). Without it the first new-style crawl would alert all of
    those old listings as new. The newest `keep_newest` listings — the old
    crawl's horizon — and the current first page are left alone: anything new
    among them still alerts through the normal dedup.
    """
    store = Store(settings.db_path)
    http = Http(settings.proxy_url, settings.proxy_mode)
    sources = build_sources(http, settings)
    try:
        groups: dict[tuple[str, str], list] = {}
        for sub in store.due_subscriptions():
            if sub.status == "active":
                groups.setdefault(sub.group, []).append(sub)
        for (source_key, filter_json), subs in groups.items():
            flt = Filter.from_json(filter_json)
            source = sources[source_key]
            listings = await source.search(flt, max_pages=None)  # newest first
            keep = {l.id for l in listings[:keep_newest]}
            keep |= {l.id for l in await source.search(flt, max_pages=1)}  # published meanwhile
            today = utcnow().date()
            keys = [
                (slot_key(l, s), s.end)
                for l in listings
                if l.id not in keep and listing_matches(l, flt)
                for s in matching_slots(l, flt, today)
            ]
            for sub in subs:
                known = store.delivered_keys(sub.id)
                store.mark_delivered(sub, keys)
                added = len(store.delivered_keys(sub.id)) - len(known)
                print(f"#{sub.id}: {len(listings)} listings, {len(listings) - len(keep)} beyond the newest; "
                      f"{added} slots newly marked seen")
    finally:
        await http.aclose()
        store.close()


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
    rb = sub.add_parser("rebaseline", help="one-off upgrade step; run with the bot stopped")
    rb.add_argument("--keep-newest", type=int, default=1000,
                    help="leave the N newest listings to normal dedup (the old crawl's horizon)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    settings = Settings.load()

    if args.cmd == "codes":
        codes(args, settings)
        return
    if args.cmd == "rebaseline":
        asyncio.run(rebaseline(settings, args.keep_newest))
        return
    if not settings.bot_token:
        sys.exit("BOT_TOKEN is not set — see .env.example")
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
