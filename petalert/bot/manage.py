"""Commands outside the dialog: /start, /help, /list, /sites, /cancel and their buttons.
(/search has its own module.)"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Store, Subscription
from ..scheduler import Worker
from .browse import Id
from .format import describe_filter
from .ui import STALE, answer, chat_id, edit

HELP = (
    "🐾 <b>House-sit alerts</b>\n"
    "I watch house-sitting sites and message you as soon as new sits match your filters.\n\n"
    "<b>Commands</b>\n"
    "➕ /new — create a subscription (continent, country, dates, length, pets)\n"
    "📋 /list — your subscriptions: pause, resume, delete\n"
    "🔎 /search — open sits by start date, e.g. <code>eu(-uk) ±3 30.11.2026</code>\n"
    "🌐 /sites — which sites you get alerts from\n"
    "✖️ /cancel — abort the current dialog\n\n"
    "ℹ️ New dates on an existing listing count as new too. "
    "Sites are checked about every 30 minutes; several new sits at once "
    "arrive as one table — tap a number to open the sit on its site."
)

SITES_HELP = (
    "🌐 <b>Sites</b>\n"
    "Your subscriptions watch every site that is on.\n"
    "A site you turn on is indexed silently first, so you only get its new sits."
)


class SubAction(CallbackData, prefix="s"):
    action: str  # pause | resume | delete
    sub_id: Id


class SiteToggle(CallbackData, prefix="w"):
    source: str
    on: bool  # the state asked for: a double tap or an old message mustn't flip it back


def sub_text(sub: Subscription, sites: list[tuple[str, bool]]) -> str:
    """`sites`: (title, baselined) of the sites the chat has on."""
    if sub.status == "paused":
        status = "⏸ paused"
    elif not sites:
        status = "💤 all sites are off — /sites"
    elif not all(done for _, done in sites):
        status = "⏳ indexing " + ", ".join(title for title, done in sites if not done)
    else:
        status = "🟢 active"
    where = ", ".join(title for title, _ in sites) or "none"
    return (
        f"🔔 <b>Subscription #{sub.id}</b>\n{status}\n\n{describe_filter(sub.filter)}\n"
        f"🌐 Sites: {where}"
    )


def sites_of(store: Store, sources, sub: Subscription, watches) -> list[tuple[str, bool]]:
    return [
        (sources[key].title, (sub.id, key) in watches)
        for key in store.enabled_sources(sub.chat_id, sources)
    ]


def sites_keyboard(store: Store, sources, chat: int) -> InlineKeyboardMarkup:
    on = set(store.enabled_sources(chat, sources))
    kb = InlineKeyboardBuilder()
    for key, source in sources.items():
        kb.button(
            text=f"{'✅' if key in on else '⬜'} {source.title}",
            callback_data=SiteToggle(source=key, on=key not in on),
        )
    kb.adjust(1)
    return kb.as_markup()


def sub_keyboard(sub: Subscription) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if sub.status == "paused":
        kb.button(text="▶️ Resume", callback_data=SubAction(action="resume", sub_id=sub.id))
    else:
        kb.button(text="⏸ Pause", callback_data=SubAction(action="pause", sub_id=sub.id))
    kb.button(text="🗑 Delete", callback_data=SubAction(action="delete", sub_id=sub.id))
    return kb.as_markup()


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
    watches = store.watches(message.chat.id)
    for sub in subs:
        await message.answer(sub_text(sub, sites_of(store, sources, sub, watches)), reply_markup=sub_keyboard(sub))


async def cmd_sites(message: Message, store: Store, sources) -> None:
    # The FSM is left alone: an open /new can be saved after changing sites.
    await message.answer(SITES_HELP, reply_markup=sites_keyboard(store, sources, message.chat.id))


async def on_site_toggle(
    query: CallbackQuery, callback_data: SiteToggle, store: Store, worker: Worker, sources
) -> None:
    key = callback_data.source
    if key not in sources:
        await answer(query, "Unknown site", show_alert=True)
        return
    chat = chat_id(query)
    on, title = callback_data.on, sources[key].title
    if on == (key in store.enabled_sources(chat, sources)):
        await answer(query, f"{title} is already {'on' if on else 'off'}")
    else:
        store.set_source(chat, key, on)
        if on:
            worker.wake()  # index it now, not in half an hour
        await answer(query, f"{title} on — indexing current listings" if on else f"{title} off")
    await edit(query, SITES_HELP, sites_keyboard(store, sources, chat))


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
        changed = store.pause(sub_id, chat)
        toast = "Paused" if changed else "Already paused"
    elif callback_data.action == "resume":
        # Resume = baseline again: whatever appeared while paused is not sent.
        changed = store.resume(sub_id, chat)
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
    await edit(query, sub_text(sub, sites_of(store, sources, sub, store.watches(chat))), sub_keyboard(sub))


async def stale_button(query: CallbackQuery) -> None:
    await answer(query, STALE, show_alert=True)


async def unknown_text(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        await message.answer("👆 Please use the buttons above, or /cancel.")
    else:
        await message.answer(
            "➕ /new — create a subscription\n📋 /list — manage them\n"
            "🔎 /search — find open sits by start date"
        )


def build_routers() -> tuple[Router, Router]:
    """(commands and subscription buttons, catch-all). The catch-all is a separate
    router because it must be included last — after the dialog."""
    router = Router(name="manage")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_list, Command("list"))
    router.message.register(cmd_sites, Command("sites"))
    router.callback_query.register(on_sub_action, SubAction.filter())
    router.callback_query.register(on_site_toggle, SiteToggle.filter())
    fallback = Router(name="fallback")
    fallback.callback_query.register(stale_button)
    fallback.message.register(unknown_text, F.text)
    return router, fallback
