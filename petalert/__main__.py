"""Entry point.

    python -m petalert                        # bot + worker (what systemd runs)
    python -m petalert codes generate 100     # a new pack of invite codes with deep links
    python -m petalert codes list             # unused codes
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
from .db import Store, format_code
from .http import Http
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
    outbox = TelegramOutbox(bot, {key: s.title for key, s in sources.items()})
    worker = Worker(store, sources, outbox, admin, settings.poll_interval)

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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    settings = Settings.load()

    if args.cmd == "codes":
        codes(args, settings)
        return
    if not settings.bot_token:
        sys.exit("BOT_TOKEN is not set — see .env.example")
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
