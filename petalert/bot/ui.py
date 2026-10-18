"""Editing the message a button belongs to, robustly.

Two Telegram realities the handlers would otherwise crash on:
* a button on a message older than 48 h arrives with an `InaccessibleMessage`
  that can't be edited — send a fresh message instead;
* a double tap edits the message to the same content — Telegram answers
  "message is not modified", which is harmless.
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

STALE = "⌛ This button has expired. Start again with /new."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def chat_id(query: CallbackQuery) -> int:
    # The bot only works in private chats, where the chat id is the user id.
    return query.message.chat.id if query.message else query.from_user.id


async def answer(query: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    """Answering a button press is a courtesy: an expired query (pressed while
    the bot was down) must not abort a handler that already changed state."""
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if "query is too old" not in str(exc) and "query ID is invalid" not in str(exc):
            raise


def _harmless(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc)


async def edit(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    preview: LinkPreviewOptions | None = None,
) -> None:
    extra: dict[str, Any] = {"link_preview_options": preview} if preview else {}
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=markup, **extra)
        except TelegramBadRequest as exc:
            if not _harmless(exc):
                raise
        return
    await query.bot.send_message(chat_id(query), text, reply_markup=markup, **extra)


async def edit_markup(query: CallbackQuery, markup: InlineKeyboardMarkup) -> None:
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as exc:
        if not _harmless(exc):
            raise
