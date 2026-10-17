from __future__ import annotations

from aiogram import Dispatcher

from ..db import Store
from ..scheduler import Worker
from . import dialog, manage
from .access import AccessGate


def build_dispatcher(store: Store, sources: dict, worker: Worker, admins: frozenset[int]) -> Dispatcher:
    # store/sources/worker доходять до хендлерів як workflow data (за іменем аргументу).
    dp = Dispatcher(store=store, sources=sources, worker=worker)
    dp.update.outer_middleware(AccessGate(store, admins))
    # Порядок важливий: команди раніше за текстові кроки діалогу, catch-all — останнім.
    commands, fallback = manage.build_routers()
    dp.include_routers(commands, dialog.build_router(), fallback)
    return dp
