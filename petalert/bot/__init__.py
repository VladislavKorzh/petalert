from __future__ import annotations

from aiogram import Dispatcher

from ..db import Store
from ..scheduler import Worker
from . import browse, dialog, manage, search
from .access import AccessGate


def build_dispatcher(store: Store, sources: dict, worker: Worker, admins: frozenset[int]) -> Dispatcher:
    # store/sources/worker reach handlers as workflow data (matched by argument name);
    # `searching`: chats with a /search running.
    dp = Dispatcher(store=store, sources=sources, worker=worker, searching=set())
    dp.update.outer_middleware(AccessGate(store, admins))
    # Order matters: commands before the dialog's text steps, plain text as a
    # search after them, the catch-all last.
    commands, fallback = manage.build_routers()
    dp.include_routers(commands, browse.build_router(), dialog.build_router(), search.build_router(), fallback)
    return dp
