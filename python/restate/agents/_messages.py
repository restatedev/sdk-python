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
"""The conversation as clients read it: the ``messages`` view's pages, rendered from the conversation's rows.

A message is what a person sees: the user's text, or the assistant's text and
tool calls, each call with its output once it has one. It is made of rows of
the conversation: the row that starts it (whose position is the message's
id), and the rows that add to it (a tool call's result comes later, in a row
of its own). A message changes when a row is added to it, so its ``seq`` is
the latest of its rows', and a client that follows the conversation from a
cursor gets each message again whenever it changes.

A prompt's row always starts a message, and a result's row only adds to a
message of its own run (after its prompt). So the messages from any prompt on
can be rendered from the rows from that prompt on, and a page reads only the
rows it needs.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

from restate.agents._api import ChatMessage, Messages, MessagesRequest, Part, Role, ToolPart, text_part
from restate.agents._conversation import SCHEMA, TABLE, Items, start_of
from restate.sessions import DatabaseReader


@dataclass
class _Message:
    id: int
    role: Role
    parts: list[Part] = field(default_factory=list[Part])
    seq: int = 0
    """The latest ``seq`` of its rows: when it last changed."""


def render(items: Items, rows: typing.Sequence[typing.Sequence[typing.Any]]) -> list[_Message]:
    """Render the conversation's ``rows`` (``n``, ``seq``, ``item``, in order) as messages, leaving out empty ones."""
    messages: list[_Message] = []
    at: dict[str, _Message] = {}
    """Each tool call's message, by the call's id."""
    for n, seq, stored in rows:
        position, changed = int(n), int(seq)
        said, calls, results = items.read(items.load(str(stored)))
        if said is not None:
            role, text = said
            messages.append(_Message(position, "user" if role == "user" else "assistant"))
            if text.strip():
                messages[-1].parts.append(text_part(text))
            messages[-1].seq = changed
        for call in calls:
            if not messages or messages[-1].role != "assistant":
                messages.append(_Message(position, "assistant"))
            message = messages[-1]
            message.parts.append({"type": "tool", "id": call.id, "name": call.name, "input": call.input})
            message.seq = changed
            at[call.id] = message
        for result in results:
            answered = at.get(result.id)
            if answered is None:
                continue
            answered.parts = [_with_output(part, result.id, result.output, result.failed) for part in answered.parts]
            answered.seq = changed
    return [message for message in messages if message.parts]


def _with_output(part: Part, call_id: str, output: str, failed: bool) -> Part:
    if part["type"] != "tool" or part["id"] != call_id:
        return part
    answered: ToolPart = {**part, "output": output}
    if failed:
        answered["error"] = True
    return answered


def _chat(message: _Message) -> ChatMessage:
    return {"id": message.id, "role": message.role, "parts": message.parts}


async def page(db: DatabaseReader, items: Items, request: MessagesRequest | None = None) -> Messages:
    """Return a page of the conversation in ``db``, whose items are ``items``'.

    With a ``cursor``, the page holds the messages that changed since it, in
    the order they changed, and the cursor to read on from. Without one, it
    holds the latest ``limit`` messages (or the latest before message id
    ``before``), oldest first, and the cursor to follow the conversation from.
    """
    request = request or {}
    limit = request.get("limit", 50)
    await SCHEMA.ensure(db)
    start = await start_of(db)
    cursor = request.get("cursor")
    if cursor is not None:
        result = await _since(db, items, cursor, limit)
    else:
        result = await _latest(db, items, request.get("before"), limit)
    if start.n:
        result["first"] = start.n
    return result


async def _since(db: DatabaseReader, items: Items, cursor: int, limit: int) -> Messages:
    """Return the messages that changed since ``cursor``, rendered from the run of the earliest change on."""
    [[changed]] = await db.values(f"SELECT MIN(n) FROM {TABLE} WHERE seq > ?", [cursor])
    if changed is None:
        return {"messages": [], "more": False, "cursor": cursor}
    # From the prompt of the earliest change's run: read back along the primary key to it.
    prompt = await db.values(
        f"SELECT n FROM {TABLE} WHERE prompt IS NOT NULL AND n <= ? ORDER BY n DESC LIMIT 1", [changed]
    )
    first = prompt[0][0] if prompt else changed
    rows = await db.values(f"SELECT n, seq, item FROM {TABLE} WHERE n >= ? ORDER BY n", [first])
    messages = sorted((m for m in render(items, rows) if m.seq > cursor), key=lambda m: m.seq)
    shown = messages[:limit]
    return {
        "messages": [_chat(m) for m in shown],
        "more": len(messages) > limit,
        "cursor": shown[-1].seq if shown else cursor,
    }


async def _latest(db: DatabaseReader, items: Items, before: int | None, limit: int) -> Messages:
    """Return the latest ``limit`` messages before id ``before``, rendered from enough prompts back to hold them.

    Each prompt starts a message, so the rows from the ``limit + 1``-th
    latest prompt before ``before`` on hold more than ``limit`` messages.
    """
    end = before if before is not None else 2**53 - 1
    [[first]] = await db.values(
        f"SELECT COALESCE((SELECT n FROM {TABLE} WHERE prompt IS NOT NULL AND n < ? "
        f"ORDER BY n DESC LIMIT 1 OFFSET ?), (SELECT MIN(n) FROM {TABLE}))",
        [end, limit],
    )
    [[now]] = await db.values(f"SELECT MAX(seq) FROM {TABLE}")
    start = await start_of(db)
    cursor = max(start.seq, 0 if now is None else int(typing.cast(int, now)))
    if first is None:
        return {"messages": [], "more": False, "cursor": cursor}
    rows = await db.values(f"SELECT n, seq, item FROM {TABLE} WHERE n >= ? ORDER BY n", [first])
    messages = [m for m in render(items, rows) if m.id < end]
    [[earlier]] = await db.values(f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE n < ?)", [first])
    return {
        "messages": [_chat(m) for m in messages[-limit:]],
        "more": len(messages) > limit or bool(earlier),
        "cursor": cursor,
    }
