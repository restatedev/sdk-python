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

A turn's prompt (a row of the transcript) always starts a message, and the
rows of its trajectory only add to messages of their own turn. So the
messages from any turn on are rendered from the rows of both tables from that
turn's position on, in order, and a page reads only the rows it needs.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

from restate.agents._api import ChatMessage, Messages, MessagesRequest, Part, Role, ToolPart, text_part
from restate.agents._conversation import SCHEMA, STEERED, STEPS, TURNS, Items, last_of, load_item, start_of
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
        said, calls, results = items.read(load_item(items, str(stored)))
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
    summary = request.get("view") == "summary"
    if cursor is not None:
        result = await (_summary_since if summary else _since)(db, items, cursor, limit)
    else:
        result = await (_summary_latest if summary else _latest)(db, items, request.get("before"), limit)
    if start.n:
        result["first"] = start.n
    return result


def _latest_of(messages: list[_Message], limit: int) -> list[_Message]:
    """Return the latest ``limit`` of ``messages``: none for a ``limit`` of 0, though ``messages[-0:]`` is all of them."""
    return messages[-limit:] if limit > 0 else []


async def _rows(db: DatabaseReader, first: int, until: int | None = None) -> list[typing.Sequence[typing.Any]]:
    """Return the rows (``n``, ``seq``, ``item``) of the transcript and the trajectories in order.

    They are those from position ``first`` on, and before position ``until`` if given.
    """
    bounds = [first, 2**53 - 1 if until is None else until]
    turns = await db.values(f"SELECT n, seq, item FROM {TURNS} WHERE n >= ? AND n < ? ORDER BY n", bounds)
    steps = await db.values(f"SELECT n, seq, item FROM {STEPS} WHERE n >= ? AND n < ? ORDER BY n", bounds)
    return sorted([*turns, *steps], key=lambda row: int(row[0]))


async def _since(db: DatabaseReader, items: Items, cursor: int, limit: int) -> Messages:
    """Return the messages that changed since ``cursor``, rendered from the turn of the earliest change on."""
    [[prompt]] = await db.values(f"SELECT MIN(n) FROM {TURNS} WHERE seq > ?", [cursor])
    [[step]] = await db.values(f"SELECT MIN(turn) FROM {STEPS} WHERE seq > ?", [cursor])
    changed = [int(typing.cast(int, n)) for n in (prompt, step) if n is not None]
    if not changed:
        return {"messages": [], "more": False, "cursor": cursor}
    rows = await _rows(db, min(changed))
    messages = sorted((m for m in render(items, rows) if m.seq > cursor), key=lambda m: m.seq)
    shown = messages[:limit]
    return {
        "messages": [_chat(m) for m in shown],
        "more": len(messages) > limit,
        "cursor": shown[-1].seq if shown else cursor,
    }


async def _latest(db: DatabaseReader, items: Items, before: int | None, limit: int) -> Messages:
    """Return the latest ``limit`` messages before id ``before``, rendered from enough turns back to hold them.

    Each turn starts with a message, so the rows from the ``limit + 1``-th
    latest turn before ``before`` on hold more than ``limit`` messages. And
    to no later than the turn ``before`` is in: a turn's rows add only to its
    own messages, so the turns from ``before`` on change none before it.
    """
    end = before if before is not None else 2**53 - 1
    [[first]] = await db.values(
        f"SELECT COALESCE((SELECT n FROM {TURNS} WHERE n < ? ORDER BY n DESC LIMIT 1 OFFSET ?), "
        f"(SELECT MIN(n) FROM {TURNS}))",
        [end, limit],
    )
    start = await start_of(db)
    _, now = await last_of(db)
    cursor = max(start.seq, now)
    if first is None:
        return {"messages": [], "more": False, "cursor": cursor}
    until = None
    if before is not None:
        [[until]] = await db.values(f"SELECT MIN(n) FROM {TURNS} WHERE n >= ?", [end])
    rows = await _rows(db, int(typing.cast(int, first)), None if until is None else int(typing.cast(int, until)))
    messages = [m for m in render(items, rows) if m.id < end]
    [[earlier]] = await db.values(f"SELECT EXISTS (SELECT 1 FROM {TURNS} WHERE n < ?)", [first])
    return {
        "messages": [_chat(m) for m in _latest_of(messages, limit)],
        "more": len(messages) > limit or bool(earlier),
        "cursor": cursor,
    }


# --- The summary: per turn, the user's messages and the answer ------------------------------------------------------

_TURN = f"SELECT n, seq, item, answer, answered_at, answered_seq FROM {TURNS}"

_STEERED = f"SELECT st.n, s.seq, s.item FROM {STEERED} st JOIN {STEPS} s ON s.n = st.n"
"""The messages steered to turns that took them: messages of the user, which the summary shows too."""


def _summarized(
    items: Items,
    rows: typing.Sequence[typing.Sequence[typing.Any]],
    steered: typing.Sequence[typing.Sequence[typing.Any]] = (),
) -> list[_Message]:
    """Render turns' rows (``_TURN``'s columns) as their user's messages and answers, with the messages steered to
    them (``_STEERED``'s columns), in order.

    The answer's message has the id it has in the full view (the turn's last
    message of the assistant), and the ``seq`` it was stored with; so does a
    steered message.
    """

    def user_message(n: typing.Any, seq: typing.Any, stored: typing.Any) -> _Message:
        said = items.read(load_item(items, str(stored))).said
        text = said[1] if said else ""
        return _Message(int(n), "user", [text_part(text)] if text.strip() else [], int(seq))

    messages: list[_Message] = []
    for n, seq, stored, answer, answered_at, answered_seq in rows:
        messages.append(user_message(n, seq, stored))
        if answer is not None and answered_at is not None:
            answered = str(answer)
            parts: list[Part] = [text_part(answered)] if answered.strip() else []
            messages.append(_Message(int(answered_at), "assistant", parts, int(typing.cast(int, answered_seq))))
    messages.extend(user_message(n, seq, stored) for n, seq, stored in steered)
    return sorted((message for message in messages if message.parts), key=lambda message: message.id)


async def _summary_since(db: DatabaseReader, items: Items, cursor: int, limit: int) -> Messages:
    """Return the user's messages and answers stored since ``cursor``, in the order they were."""
    asked = await db.values(f"{_TURN} WHERE seq > ?", [cursor])
    answered = await db.values(f"{_TURN} WHERE answered_seq > ?", [cursor])
    steered = await db.values(f"{_STEERED} WHERE s.seq > ?", [cursor])
    rows = {int(typing.cast(int, row[0])): row for row in [*asked, *answered]}
    summarized = _summarized(items, list(rows.values()), steered)
    messages = sorted((m for m in summarized if m.seq > cursor), key=lambda m: m.seq)
    shown = messages[:limit]
    return {
        "messages": [_chat(m) for m in shown],
        "more": len(messages) > limit,
        "cursor": shown[-1].seq if shown else cursor,
    }


async def _summary_latest(db: DatabaseReader, items: Items, before: int | None, limit: int) -> Messages:
    """Return the latest ``limit`` user's messages and answers before id ``before``: from as many turns, at most."""
    end = before if before is not None else 2**53 - 1
    rows = await db.values(f"{_TURN} WHERE n < ? ORDER BY n DESC LIMIT ?", [end, limit])
    start = await start_of(db)
    _, now = await last_of(db)
    cursor = max(start.seq, now)
    earliest = int(typing.cast(int, rows[-1][0])) if rows else end
    steered = await db.values(f"{_STEERED} WHERE st.n >= ? AND st.n < ? ORDER BY st.n", [earliest, end])
    messages = [m for m in _summarized(items, rows[::-1], steered) if m.id < end]
    [[earlier]] = await db.values(f"SELECT EXISTS (SELECT 1 FROM {TURNS} WHERE n < ?)", [earliest])
    return {
        "messages": [_chat(m) for m in _latest_of(messages, limit)],
        "more": len(messages) > limit or bool(earlier),
        "cursor": cursor,
    }
