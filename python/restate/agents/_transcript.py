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
"""Build the conversation that clients read from a framework's transcript."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from restate.agents._api import Part, Role, Said, ToolPart, is_tool_part, text_part


class Transcript:
    """A conversation in the form the ``messages`` view returns, built from a framework's transcript.

    ``say`` starts each message, ``call`` adds a tool call to the assistant's
    message, and ``result`` adds the call's output. Messages without parts
    are left out.

    A message returned by ``messages()`` is never mutated: a change replaces
    it with a new one. So a framework can keep its ``Transcript`` and add to
    it as the conversation grows, and the log stores only the messages that
    are not the same objects it stored last time (see ``_log.py``).
    """

    def __init__(self) -> None:
        self._messages: list[Said] = []
        # The index of each tool call's message, by the call's id.
        self._calls: dict[str, int] = {}

    def say(self, role: Role, text: str = "") -> None:
        """Start a new message of ``role``, with ``text`` if it is not blank."""
        self._messages.append({"role": role, "parts": [text_part(text)] if text.strip() else []})

    def call(self, call_id: str, name: str, input: Any) -> None:
        """Add tool call ``call_id`` to the assistant's message, starting one if needed."""
        if not self._messages or self._messages[-1]["role"] != "assistant":
            self.say("assistant")
        at = len(self._messages) - 1
        call: ToolPart = {"type": "tool", "id": call_id, "name": name, "input": input}
        self._change(at, lambda parts: [*parts, call])
        self._calls[call_id] = at

    def result(self, call_id: str, output: str, error: bool = False) -> None:
        """Set the output of tool call ``call_id``.

        The output counts as an error if ``error`` is true or it starts with
        ``"Error: "``.
        """
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
        """Return whether tool call ``call_id`` has its output.

        An error output does not count, unless it is ``failed``: the error
        the call itself returned.
        """
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
        """Return the messages in order, leaving out those without parts."""
        return [message for message in self._messages if message["parts"]]

    def _change(self, at: int, change: Callable[[list[Part]], list[Part]]) -> None:
        """Replace message ``at`` with a new message whose parts ``change`` computes."""
        message = self._messages[at]
        self._messages[at] = {"role": message["role"], "parts": change(message["parts"])}
