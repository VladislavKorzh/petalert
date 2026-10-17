"""Commands outside the dialog: /start, /help, /list, /cancel and subscription buttons."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Store, Subscription
from ..scheduler import Worker
from .format import describe_filter
from .ui import STALE, answer, chat_id, edit

HELP = (
    "🐾 <b>House-sit alerts</b>\n"
    "I watch house-sitting sites and message you as soon as new sits match your filters.\n\n"
    "<b>Commands</b>\n"
    "➕ /new — create a subscription (continent, country, dates, length, pets)\n"
    "📋 /list — your subscriptions: pause, resume, delete\n"
    "✖️ /cancel — abort the current dialog\n\n"
    "ℹ️ New dates on an existing listing count as new too. "
    "Sites are checked about every 30 minutes."
)

STATUS = {"active": "🟢 active", "baselining": "⏳ indexing", "paused": "⏸ paused"}


class SubAction(CallbackData, prefix="s"):
    action: str  # pause | resume | delete
    sub_id: int


def sub_text(sub: Subscription, sources_titles: dict[str, str]) -> str:
    title = sources_titles.get(sub.source, sub.source)
    return f"🔔 <b>Subscription #{sub.id}</b> · {title}\n{STATUS[sub.status]}\n\n{describe_filter(sub.filter)}"


def sub_keyboard(sub: Subscription) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if sub.status == "paused":
        kb.button(text="▶️ Resume", callback_data=SubAction(action="resume", sub_id=sub.id))
    else:
        kb.button(text="⏸ Pause", callback_data=SubAction(action="pause", sub_id=sub.id))
    kb.button(text="🗑 Delete", callback_data=SubAction(action="delete", sub_id=sub.id))
    return kb.as_markup()


def _titles(sources) -> dict[str, str]:
    return {key: source.title for key, source in sources.items()}


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("👋 Welcome!\n\n" + HELP)


async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    had_dialog = await state.get_state() is not None
    await state.clear()
    await message.answer("✖️ Cancelled." if had_dialog else "🤷 Nothing to cancel.")


async def cmd_list(message: Message, state: FSMContext, store: Store, sources) -> None:
    await state.clear()
    subs = store.subscriptions(message.chat.id)
    if not subs:
        await message.answer("📭 No subscriptions yet. Create one with /new.")
        return
    titles = _titles(sources)
    for sub in subs:
        await message.answer(sub_text(sub, titles), reply_markup=sub_keyboard(sub))


async def on_sub_action(
    query: CallbackQuery, callback_data: SubAction, store: Store, worker: Worker, sources
) -> None:
    chat = chat_id(query)
    sub_id = callback_data.sub_id
    if callback_data.action == "delete":
        if not store.delete(sub_id, chat):
            await answer(query, "Subscription not found", show_alert=True)
            return
        await answer(query, "Deleted")
        await edit(query, f"🗑 Subscription #{sub_id} deleted.")
        return
    if callback_data.action == "pause":
        changed = store.transition(sub_id, chat, "paused", allowed_from=("active", "baselining"))
        toast = "Paused" if changed else "Already paused"
    elif callback_data.action == "resume":
        # Resume = baseline again: whatever appeared while paused is not sent.
        # Only from paused: a stale Resume on a running subscription would
        # otherwise silently swallow its undelivered new dates.
        changed = store.transition(sub_id, chat, "baselining", allowed_from=("paused",))
        toast = "Resumed" if changed else "Already running"
        if changed:
            worker.wake()
    else:
        await answer(query, "Unknown action", show_alert=True)
        return
    sub = store.subscription(sub_id, chat)
    if sub is None:
        await answer(query, "Subscription not found", show_alert=True)
        return
    await answer(query, toast)
    await edit(query, sub_text(sub, _titles(sources)), sub_keyboard(sub))


async def stale_button(query: CallbackQuery) -> None:
    await answer(query, STALE, show_alert=True)


async def unknown_text(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        await message.answer("👆 Please use the buttons above, or /cancel.")
    else:
        await message.answer("➕ /new — create a subscription\n📋 /list — manage them")


def build_routers() -> tuple[Router, Router]:
    """(commands and subscription buttons, catch-all). The catch-all is a separate
    router because it must be included last — after the dialog."""
    router = Router(name="manage")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_list, Command("list"))
    router.callback_query.register(on_sub_action, SubAction.filter())
    fallback = Router(name="fallback")
    fallback.callback_query.register(stale_button)
    fallback.message.register(unknown_text, F.text)
    return router, fallback
