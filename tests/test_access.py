from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from aiogram.types import Chat, Message, Update, User

from petalert.bot.access import BAD_CODE, INVITE_ONLY, WELCOME, AccessGate, code_from_text
from petalert.db import Store, format_code


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


class Gate(AccessGate):
    def __init__(self, *a):
        super().__init__(*a)
        self.replies: list[str] = []

    async def reply(self, message, text):
        self.replies.append(text)


def run(gate: Gate, text: str, user_id: int, chat_type: str = "private") -> bool:
    """Run an update through the gate; True if it reached the handler."""
    user = User(id=user_id, is_bot=False, first_name="u")
    chat = Chat(id=user_id, type=chat_type)
    update = Update(
        update_id=1,
        message=Message(message_id=1, date=datetime.now(), chat=chat, from_user=user, text=text),
    )
    passed = []

    async def handler(event, data):
        passed.append(True)

    asyncio.run(gate(handler, update, {"event_from_user": user, "event_chat": chat}))
    return bool(passed)


def test_code_from_text():
    assert code_from_text("/start K7QM4XPA") == "K7QM4XPA"
    assert code_from_text("/start@petalert_bot k7qm-4xpa") == "K7QM4XPA"
    assert code_from_text("k7qm-4xpa") == "K7QM4XPA"
    assert code_from_text("/start") is None
    assert code_from_text("/list") is None


def test_strangers_need_a_code_and_codes_are_single_use(store):
    (code,) = store.generate_codes(1)
    gate = Gate(store, frozenset())

    assert not run(gate, "/new", 5)
    assert not run(gate, "/start", 5)
    assert not run(gate, "hello", 5)
    assert gate.replies == [INVITE_ONLY, INVITE_ONLY, INVITE_ONLY]

    assert not run(gate, f"/start {code}", 5)  # deep link
    assert gate.replies[-1] == WELCOME
    assert run(gate, "/new", 5)

    assert not run(gate, format_code(code), 6)  # the same code by someone else
    assert gate.replies[-1] == BAD_CODE
    assert not run(gate, "/new", 6)


def test_admins_pass_without_code_and_groups_are_ignored(store):
    gate = Gate(store, frozenset({1}))
    assert run(gate, "/new", 1)
    assert not run(gate, "/new", 1, chat_type="group")
    assert gate.replies == []
