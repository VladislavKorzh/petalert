from __future__ import annotations

from aiogram import Dispatcher

from ..db import Store
from ..scheduler import Worker
from . import dialog, manage
from .access import AccessGate


def build_dispatcher(store: Store, sources: dict, worker: Worker, admins: frozenset[int]) -> Dispatcher:
    # store/sources/worker reach handlers as workflow data (matched by argument name).
    dp = Dispatcher(store=store, sources=sources, worker=worker)
    dp.update.outer_middleware(AccessGate(store, admins))
    # Order matters: commands before the dialog's text steps, the catch-all last.
    commands, fallback = manage.build_routers()
    dp.include_routers(commands, dialog.build_router(), fallback)
    return dp
