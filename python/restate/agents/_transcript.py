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
"""
A conversation as ``messages`` gives it, built from a framework's transcript.
As sdk-sessions' ``Transcript`` (``common.ts``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from restate.agents._api import Part, Role, Said, ToolPart, is_tool_part, text_part


class Transcript:
    """
    A conversation as ``messages`` gives it, built from a framework's
    transcript: each message is started by ``say``, a tool call goes to the
    assistant's message, and its result to its call. Messages without parts
    are left out.

    A message, once given out by ``messages()``, is never changed: a change
    makes a new one in its place. So a framework can keep its transcript and
    add to it as its conversation grows, and the log stores only the messages
    that are not the ones it stored last (see ``_log.py``).
    """

    def __init__(self) -> None:
        self._messages: list[Said] = []
        # The message of each tool call, by the call's id: its index.
        self._calls: dict[str, int] = {}

    def say(self, role: Role, text: str = "") -> None:
        """A new message of ``role``, with ``text`` if there is any."""
        self._messages.append({"role": role, "parts": [text_part(text)] if text.strip() else []})

    def call(self, call_id: str, name: str, input: Any) -> None:
        """Tool call ``call_id`` of the assistant's message."""
        if not self._messages or self._messages[-1]["role"] != "assistant":
            self.say("assistant")
        at = len(self._messages) - 1
        call: ToolPart = {"type": "tool", "id": call_id, "name": name, "input": input}
        self._change(at, lambda parts: [*parts, call])
        self._calls[call_id] = at

    def result(self, call_id: str, output: str, error: bool = False) -> None:
        """The output of tool call ``call_id``; an error's if the framework says so, or it is a tool's ``Error: <message>``."""
        at = self._calls.get(call_id)
        if at is None:
            return
        failed = error or output.startswith("Error: ")

        def with_output(parts: list[Part]) -> list[Part]:
            changed: list[Part] = []
            for part in parts:
                if is_tool_part(part) and part["id"] == call_id:
                    answered: ToolPart = {**part, "output": output}
                    if failed:
                        answered["error"] = True
                    changed.append(answered)
                else:
                    changed.append(part)
            return changed

        self._change(at, with_output)

    def answered(self, call_id: str, failed: str | None = None) -> bool:
        """Whether tool call ``call_id`` has its output: not an error's, unless it is ``failed``, the error the call answered with."""
        at = self._calls.get(call_id)
        if at is None:
            return False
        return any(
            is_tool_part(part)
            and part["id"] == call_id
            and "output" in part
            and (not part.get("error") or part["output"] == failed)
            for part in self._messages[at]["parts"]
        )

    def messages(self) -> list[Said]:
        """Its messages, in order: those with parts."""
        return [message for message in self._messages if message["parts"]]

    def _change(self, at: int, change: Callable[[list[Part]], list[Part]]) -> None:
        """Message ``at`` with its parts as ``change`` makes them: a new message, in its place."""
        message = self._messages[at]
        self._messages[at] = {"role": message["role"], "parts": change(message["parts"])}
