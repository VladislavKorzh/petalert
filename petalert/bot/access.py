"""Access via single-use invite codes.

The gate is an outer middleware on updates, so no handler ever sees strangers.
Admins (ADMIN_USER_IDS) and users who already entered a code pass; everyone
else can only enter a code — via the deep link `t.me/<bot>?start=<code>` or as
plain text. The bot only works in private chats: a subscription belongs to a
chat, and in a group anyone could press its buttons.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update, User

from ..db import CODE_LENGTH, Store, normalize_code
from .manage import HELP
from .ui import answer

log = logging.getLogger(__name__)

INVITE_ONLY = "🔒 This bot is invite-only. Send your access code."
BAD_CODE = "❌ Invalid or already used code."
WELCOME = "🎉 You're in!\n\n" + HELP


def code_from_text(text: str) -> str | None:
    """The code from `/start <code>` or from plain text; None if it clearly isn't one."""
    if text.startswith("/"):
        command, _, arg = text.partition(" ")
        if command.split("@")[0] != "/start":
            return None
        text = arg
    code = normalize_code(text)
    return code or None


class AccessGate(BaseMiddleware):
    def __init__(self, store: Store, admins: frozenset[int]):
        self.store = store
        self.admins = admins

    def allowed(self, user_id: int) -> bool:
        return user_id in self.admins or self.store.is_allowed(user_id)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        assert isinstance(event, Update)
        user: User | None = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None or chat.type != "private":
            return None
        if self.allowed(user.id):
            return await handler(event, data)
        if event.message is not None:
            await self.reply(event.message, self.enter_code(event.message.text or "", user.id))
        elif event.callback_query is not None:
            await answer(event.callback_query, INVITE_ONLY, show_alert=True)
        return None

    def enter_code(self, text: str, user_id: int) -> str:
        """A sign-in attempt; returns the reply for the user."""
        code = code_from_text(text)
        if code is None or len(code) != CODE_LENGTH:
            return INVITE_ONLY
        if not self.store.redeem(code, user_id):
            log.info("bad code from %s", user_id)
            return BAD_CODE
        log.info("user %s signed in with a code", user_id)
        return WELCOME

    async def reply(self, message: Message, text: str) -> None:
        await message.answer(text)
