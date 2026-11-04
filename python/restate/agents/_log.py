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
"""The conversation log that an agent's clients read.

The log lives in the object's database. It is brought in step with the
framework's transcript each time the agent publishes a change, and the
``messages`` and ``sync`` views read it.

Each message has an ``id`` that never changes, and a ``seq``: the sequence
number of its latest change, which serves as the cursor. A message that grows
in place (when a tool call's output arrives, for example) keeps its id and
gets a new ``seq``. When a transcript is replaced (compacted or reset), its
old messages stay in the log, and the replacement's messages are new ones. So
a client that reads from a cursor gets each new or changed message exactly
once.

``clear`` deletes the log with the rest of the conversation, but remembers
where it got to, in a row of ``restate_agent_log_start``. The next
conversation's ids and cursors carry on from there, so a client that follows
the conversation keeps reading, and drops the messages before ``first``.
"""

from __future__ import annotations

import json
import typing
from dataclasses import dataclass

from restate.agents._api import ChatMessage, Messages, MessagesRequest, Said
from restate.sessions import DatabaseReader, DatabaseWriter, SessionContext
from restate.sessions.internal import Schema, ScopeKey

TABLE = "restate_agent_log"

SCHEMA = Schema(
    "restate-agents.log",
    [
        f"""CREATE TABLE {TABLE} (
            id INTEGER PRIMARY KEY,
            seq INTEGER NOT NULL UNIQUE,
            position INTEGER UNIQUE,
            message TEXT NOT NULL
        )""",
        # Where the log starts; there is no row until the first `clear`.
        f"""CREATE TABLE {TABLE}_start (
            row INTEGER PRIMARY KEY CHECK (row = 0),
            seq INTEGER NOT NULL,
            id INTEGER NOT NULL
        )""",
    ],
)


@dataclass(frozen=True)
class Start:
    """Where the log starts: past the ids and cursors of the conversations that ``clear`` deleted."""

    seq: int
    """The cursor the log starts at."""
    id: int
    """The id of the log's first message."""


async def _start_of(db: DatabaseReader) -> Start | None:
    """Return where the log of ``db`` starts, or ``None`` if ``clear`` never set it."""
    row = await db.get(f"SELECT seq, id FROM {TABLE}_start")
    return Start(seq=int(typing.cast(int, row["seq"])), id=int(typing.cast(int, row["id"]))) if row else None


async def restart(tx: DatabaseWriter, start: Start) -> None:
    """Restart the log at ``start``, inside the transaction of ``clear`` that emptied the database.

    The next conversation's ids and cursors carry on from ``start``.
    """
    await tx.execute(f"INSERT OR REPLACE INTO {TABLE}_start (row, seq, id) VALUES (0, ?, ?)", [start.seq, start.id])


@dataclass
class _Row:
    """A message of the log's head (the transcript as last stored): its id, its JSON, and the message."""

    id: int
    json: str
    said: Said | None = None
    """The message: the transcript's own once synced, else parsed from ``json`` only when needed."""

    @property
    def message(self) -> Said:
        said = self.said
        if said is None:
            said = self.said = json.loads(self.json)
        return said


@dataclass
class _Head:
    """The log's head as last stored, with the latest cursor and the next id."""

    rows: list[_Row]
    seq: int
    next: int


_HEAD: ScopeKey[_Head] = ScopeKey("the log")
"""The log's head as the attempt's latest ``sync`` stored it; the next ``sync`` compares the transcript with it."""


def _json(message: Said) -> str:
    return json.dumps(message, separators=(",", ":"))


async def _head_of(ctx: SessionContext) -> _Head:
    """Return the log's head: as the attempt last stored it, or else as read from the database."""
    cached = _HEAD.get(ctx)
    if cached is not None:
        return cached
    db = ctx.db
    # Compared by their JSON: a message is parsed only if it changed (see ``sync``).
    rows = [
        _Row(id=int(typing.cast(int, id)), json=typing.cast(str, message))
        for id, message in await db.values(
            f"SELECT id, message FROM {TABLE} WHERE position IS NOT NULL ORDER BY position"
        )
    ]
    [[last, nxt]] = await db.values(f"SELECT MAX(seq), MAX(id) + 1 FROM {TABLE}")
    if last is not None:
        return _Head(rows=rows, seq=int(typing.cast(int, last)), next=int(typing.cast(int, nxt)))
    start = await _start_of(db) or Start(seq=0, id=0)
    return _Head(rows=rows, seq=start.seq, next=start.id)


async def sync(ctx: SessionContext, messages: list[Said]) -> None:
    """Store the transcript's ``messages`` in the log, writing only those that changed, in one transaction."""
    db = ctx.db
    await SCHEMA.ensure(db)
    head = await _head_of(ctx)
    # A message the transcript did not change is the very object stored (see Transcript): compare only the others, as JSON.
    jsons: dict[int, str] = {}

    def json_of(position: int) -> str:
        if position not in jsons:
            jsons[position] = _json(messages[position])
        return jsons[position]

    def unchanged(position: int) -> bool:
        row = head.rows[position]
        return row.said is messages[position] or row.json == json_of(position)

    both = min(len(head.rows), len(messages))
    same = 0
    while same < both and unchanged(same):
        same += 1
    grown = same
    while grown < both and _grown_from(messages[grown], head.rows[grown].message):
        grown += 1
    if same == len(head.rows) == len(messages):
        return
    seq, next_id = head.seq, head.next
    rows = head.rows[:same]
    try:
        async with db.transaction() as tx:
            for position in range(same, grown):
                row = _Row(id=head.rows[position].id, json=json_of(position), said=messages[position])
                seq += 1
                await tx.execute(f"UPDATE {TABLE} SET seq = ?, message = ? WHERE id = ?", [seq, row.json, row.id])
                rows.append(row)
            # The rest of the old transcript is replaced: its messages stay in the log, but leave the transcript.
            await tx.execute(f"UPDATE {TABLE} SET position = NULL WHERE position >= ?", [grown])
            for position in range(grown, len(messages)):
                row = _Row(id=next_id, json=json_of(position), said=messages[position])
                next_id += 1
                seq += 1
                await tx.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?)", [row.id, seq, position, row.json])
                rows.append(row)
    except BaseException:
        # Rolled back: the log is unchanged, and the next sync reads it again.
        _HEAD.delete(ctx)
        raise
    _HEAD.set(ctx, _Head(rows=rows, seq=seq, next=next_id))


async def page(db: DatabaseReader, request: MessagesRequest | None = None) -> Messages:
    """Return a page of the log.

    With a ``cursor``, the page holds the messages that are new or changed
    since it, in the order they changed, and the cursor to read on from.
    Without one, it holds the latest ``limit`` messages (or the latest before
    message id ``before``), oldest first, and the cursor to follow the
    conversation from.
    """
    request = request or {}
    limit = request.get("limit", 50)
    await SCHEMA.ensure(db)
    start = await _start_of(db)
    cursor = request.get("cursor")
    if cursor is not None:
        rows = await db.values(
            f"SELECT id, message, seq FROM {TABLE} WHERE seq > ? ORDER BY seq LIMIT ?", [cursor, limit + 1]
        )
        shown = rows[:limit]
        result: Messages = {
            "messages": [_message(row) for row in shown],
            "more": len(rows) > limit,
            "cursor": int(typing.cast(int, shown[-1][2])) if shown else cursor,
        }
    else:
        [[now]] = await db.values(f"SELECT COALESCE(MAX(seq), ?) FROM {TABLE}", [start.seq if start else 0])
        before = request.get("before")
        rows = await db.values(
            f"SELECT id, message FROM {TABLE} WHERE id < ? ORDER BY id DESC LIMIT ?",
            [before if before is not None else 2**53 - 1, limit + 1],
        )
        result = {
            "messages": [_message(row) for row in reversed(rows[:limit])],
            "more": len(rows) > limit,
            "cursor": int(typing.cast(int, now)),
        }
    if start is not None:
        result["first"] = start.id
    return result


def _message(row: list[typing.Any]) -> ChatMessage:
    """Convert a row of the log to a message, as ``messages`` returns it."""
    said: Said = json.loads(typing.cast(str, row[1]))
    return {"id": int(row[0]), "role": said["role"], "parts": said["parts"]}


def _grown_from(now: Said, was: Said) -> bool:
    """Return whether ``now`` is ``was`` grown in place: the same role and parts, with more added (such as a tool output)."""
    if was["role"] != now["role"] or len(now["parts"]) < len(was["parts"]):
        return False
    for part, other in zip(was["parts"], now["parts"], strict=False):
        if part["type"] == "text":
            if other["type"] != "text" or not other["text"].startswith(part["text"]):
                return False
        elif other["type"] != "tool" or other["id"] != part["id"]:
            return False
    return True
