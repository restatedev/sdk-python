#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""The agents' command line: what its terminal shows of the conversation as it follows it."""

# pylint: disable=missing-function-docstring

from types import SimpleNamespace
from typing import Any

import pytest

from restate.agents.__main__ import Terminal


def change(*messages: dict[str, Any]) -> Any:
    """A change of the conversation, as ``AgentClient.follow`` yields it."""
    return SimpleNamespace(messages=list(messages), waiting=[])


def user(message_id: int, text: str) -> dict[str, Any]:
    return {"id": message_id, "role": "user", "parts": [{"type": "text", "text": text}]}


def answer(message_id: int, text: str) -> dict[str, Any]:
    return {"id": message_id, "role": "assistant", "parts": [{"type": "text", "text": text}]}


def tool(message_id: int, name: str, output: str | None) -> dict[str, Any]:
    part = {"type": "tool", "id": f"call-{message_id}", "name": name, "input": {}}
    if output is not None:
        part["output"] = output
    return {"id": message_id, "role": "assistant", "parts": [part]}


def test_the_conversation_so_far_is_shown_whole(capsys: pytest.CaptureFixture[str]):
    terminal = Terminal("support")
    terminal.show(change(user(0, "Hi"), answer(1, "Hello")))
    assert capsys.readouterr().out.splitlines() == ["you: Hi", "support: Hello"]


def test_a_message_typed_here_is_not_shown_again_but_a_steered_one_is_shown_where_the_turn_took_it(
    capsys: pytest.CaptureFixture[str],
):
    terminal = Terminal("support")
    terminal.show(change())
    terminal.sending("Refund order 103")  # typed: the terminal shows it already
    terminal.show(change(user(0, "Refund order 103"), tool(1, "approval", None)))
    # Steered with /steer: the turn took it after the approval, before the refund.
    terminal.show(change(tool(1, "approval", "approved"), user(2, "And order 104?"), tool(3, "refund", "done")))
    terminal.show(change(user(0, "Refund order 103"), answer(4, "Refunded, and 104 ships today.")))
    assert capsys.readouterr().out.splitlines() == [
        "  → approval({})",
        "  ← approved",
        "you: And order 104?",
        "  → refund({})",
        "  ← done",
        "support: Refunded, and 104 ships today.",
    ]


def test_a_message_from_elsewhere_is_shown_and_each_typed_message_is_hidden_once(capsys: pytest.CaptureFixture[str]):
    terminal = Terminal("support")
    terminal.show(change())
    terminal.sending("again")
    terminal.show(change(user(0, "again"), answer(1, "ok")))
    terminal.show(change(user(2, "again")))  # the same text, from another client
    terminal.sending("never sent")
    terminal.unsent("never sent")  # its send failed
    terminal.show(change(user(3, "never sent")))
    assert capsys.readouterr().out.splitlines() == ["support: ok", "you: again", "you: never sent"]


def test_a_message_typed_before_the_conversation_so_far_came_is_shown_there_and_hides_no_later_one(
    capsys: pytest.CaptureFixture[str],
):
    terminal = Terminal("support")
    terminal.sending("Hi")  # typed before the first change came, which has it already
    terminal.show(change(user(0, "Hi"), answer(1, "Hello")))
    terminal.show(change(user(2, "Hi")))  # the same text, from another client: shown
    assert capsys.readouterr().out.splitlines() == ["you: Hi", "support: Hello", "you: Hi"]
