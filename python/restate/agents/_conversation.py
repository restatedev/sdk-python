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
"""The conversation of an agent's key, which every framework integration stores the same way.

A conversation is the agent's key: one endless sequence of turns. A turn is
one prompt (the user's message) and what answers it. Its id is the
invocation's (``next_prompt_id``: ``<id>``, then ``<id>:2``, ... when a
handler prompts more than once), so each attempt of the invocation carries
on the same turn, and a retried attempt finds its answer.

The agent's ``chat`` handler is exclusive on the key, and keeps the key while
it waits (suspended, say), so turns never overlap: at most one turn has no
answer, the latest, and if a newer turn finds one, the invocation that ran it
is gone (killed, say). The newer turn closes it (``ENDED``), as does the
companion if it notices first (``settle``), rather than finishing it.

A turn is stored in two parts, in the key's database:

- The **transcript**, ``restate_agent_turns``: one row per turn, with the
  prompt (the framework's item for it), the turn's answer once it has one,
  and what a framework carries from turn to turn that is no message: the
  agent the conversation is with (after a handoff, say), and the framework's
  own state (``Turn.carry``).
- The **trajectories**, ``restate_agent_items``: every other item (the
  model's replies, tool calls and their results, compaction items), each
  with its turn. A turn is completed from its own trajectory.

In code, a turn is a ``Turn``: its run reads and writes its own rows, and
only the latest turn, while it has no answer, is written. It ends once, with a
status (``answered``, or closed: ``cancelled``, ``failed``, ``refused``,
``ended``). ``Conversation`` holds the turns, and composes them into what the
model gets (``history``): the one place where turns are made one flat list.
See ``docs/agents/`` at the repository's root: ``turns.md`` and ``conversation.md``.

The model gets the transcript, and the whole trajectories of the latest
``window`` turns, and of any turn after them that another invocation ended
(see ``_Walk``), in ``Conversation.history``. Earlier turns are the user's
message and the answer, as many as a budget of tokens holds, and those that
were summarized are one summary (``compact``). A turn is loaded with only
what it needs: the transcript, and the window's trajectories
(``Conversation.load``); a retried attempt of an answered turn reads only its
answer.

The framework stores and loads its items itself (``Items.dump`` and
``Items.load``), so that an item goes from the database to the framework's
own object in one step, such as pydantic's ``validate_json``. The two tables
share one sequence of positions, which are the ids of the messages that the
agent's ``messages`` and ``sync`` views render for clients (``_messages.py``).

Each row has a ``seq``, which increases with every row written, so that a
client can ask what changed since a cursor. Rows are only ever appended, in
one transaction per step, but for a reply at the end that is dropped (to ask
the model again), which is one range delete on the primary key; and a
turn's answer is one update of its row, by its unique prompt id.

``restate_agent_conversation_start`` has one row: the first position of the
conversation (``clear`` deletes the rows, and positions carry on), the last
``seq`` given (a drop deletes rows, and cursors must not go back), and the
summary of the turns before position ``summarized``.

An agent whose framework compacts the history itself takes it from
snapshots instead (``restate_agent_snapshots``, see ``Snapshot``): the model
gets the latest snapshot, then every row after it, whole; ``window``,
``budget`` and summaries then do not apply. The views never show snapshots.

On top of it, ``answer_once`` runs a prompt the same way for every framework:
each prompt is answered once per invocation, and a stopped, refused or
failed run is closed so that the next prompt starts afresh. A framework
provides its ``Items`` (how its items say what they are) and a ``Run`` (its
own loop, from the conversation to the answer, storing each step with
``append`` as it goes).
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from restate.agents._api import Steer
from restate.agents._calls import outcomes
from restate.agents._common import (
    BUDGET,
    CANCELLED,
    ENDED,
    SUMMARIZE,
    UNANSWERED,
    Check,
    Window,
    check_window,
    cut_off_result,
    is_cancellation,
    next_prompt_id,
    refusal_of,
    stored_answer,
)
from restate.agents._turn import AttemptEnded, answering, settings_of, stop_signal_of
from restate.exceptions import TerminalError
from restate.sessions import Database, DatabaseReader, DatabaseWriter, SessionContext, Transaction
from restate.sessions.internal import Schema, definition_of

_logger = logging.getLogger(__name__)

Item: typing.TypeAlias = Any
"""An item of the conversation: a framework's own message (a pydantic model, a dict, ...)."""

TURNS = "restate_agent_turns"
"""The transcript: one row per turn, in the key's database."""

STEPS = "restate_agent_items"
"""The trajectories: the items of each turn but its prompt, in the key's database."""

START = "restate_agent_conversation_start"
"""Where the conversation starts: its first position, the last ``seq`` given, and the summary of the earliest turns."""

SNAPSHOTS = "restate_agent_snapshots"
"""The history as a framework's own compaction left it, for agents that take their history from snapshots."""

STEERED = "restate_agent_steers"
"""The messages steered to a turn that it took (``Turn.steer``): which items of the trajectories they are, by
position, and their ids, each stored once."""

SCHEMA = Schema(
    "restate_agents.turns",
    [
        f"""CREATE TABLE {TURNS} (
            n INTEGER PRIMARY KEY,
            seq INTEGER NOT NULL,
            prompt TEXT NOT NULL UNIQUE,
            item TEXT NOT NULL,
            status TEXT,
            answer TEXT,
            agent TEXT,
            carried TEXT,
            answered_at INTEGER,
            answered_seq INTEGER,
            asked_at INTEGER
        )""",
        f"CREATE INDEX {TURNS}_seq ON {TURNS} (seq)",
        f"CREATE INDEX {TURNS}_answered_seq ON {TURNS} (answered_seq)",
        f"""CREATE TABLE {STEPS} (
            n INTEGER PRIMARY KEY,
            turn INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            item TEXT NOT NULL,
            reply INTEGER,
            agent TEXT
        )""",
        f"CREATE INDEX {STEPS}_seq ON {STEPS} (seq)",
        f"""CREATE TABLE {START} (
            row INTEGER PRIMARY KEY CHECK (row = 0),
            n INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            summary TEXT,
            summarized INTEGER
        )""",
        f"""CREATE TABLE {SNAPSHOTS} (
            id INTEGER PRIMARY KEY,
            covers INTEGER NOT NULL,
            turn INTEGER,
            items TEXT NOT NULL
        )""",
        # Appended: a new table, which costs a view that applies it to its own session nothing.
        f"""CREATE TABLE {STEERED} (
            n INTEGER PRIMARY KEY,
            turn INTEGER NOT NULL,
            id TEXT NOT NULL UNIQUE
        )""",
    ],
)


Status: typing.TypeAlias = Literal["answered", "cancelled", "failed", "refused", "ended"]
"""How a turn ended: answered by its run, or closed: stopped, failed for good, refused by the model's provider, or
ended by another invocation once its own was gone (``ENDED``)."""


class Call(typing.NamedTuple):
    """A tool call that an item makes."""

    id: str
    name: str
    input: Any


class Result(typing.NamedTuple):
    """A tool call's result that an item holds."""

    id: str
    output: str
    error: bool = False

    @property
    def failed(self) -> bool:
        """Whether the result is an error: marked as one, or ``"Error: <message>"``."""
        return self.error or self.output.startswith("Error: ")


@dataclass
class Entry:
    """An item of the conversation, with what it is part of."""

    n: int
    """Its position; the conversation is ordered by it."""
    item: Item
    prompt: str | None = None
    """The id of the prompt it is, if it is one: it then starts a turn."""
    answer: str | None = None
    """The prompt's answer, once it has one."""
    reply: int | None = None
    """The id of the model's reply it is part of, if it is: the position of the reply's first item."""
    agent: str | None = None
    """The agent that gave the reply; on a prompt, the agent the conversation is with after its turn."""
    said: tuple[str, str] | None = None
    """The role (``user`` or ``assistant``) and text it says, if it is a message: read from ``item`` once, as the
    conversation loads or appends it, like ``calls`` and ``results``."""
    calls: Sequence[Call] = ()
    """The tool calls it makes."""
    results: Sequence[Result] = ()
    """The tool calls' results it holds."""
    turn: int | None = None
    """The position of its turn's prompt."""
    carried: str | None = None
    """On a prompt: the framework's state as of the end of its turn, if it carries one (``Turn.carry``)."""
    status: Status | None = None
    """On a prompt: how its turn ended, once it did."""
    answered_at: int | None = None
    """On a prompt: the position of its turn's answer, its last message of the assistant, once it has one."""
    answered_seq: int | None = None
    """On a prompt: the ``seq`` given as its turn was answered."""
    asked_at: int | None = None
    """On a prompt: when its turn's row was stored, in Unix milliseconds."""
    steer: str | None = None
    """The id of the steered message it is, if it is one: a message of the user that its turn took at a step
    (``Turn.steer``)."""
    steers: list[Entry] = field(default_factory=list["Entry"])
    """On a prompt of the transcript: the messages steered to its turn that it took, in order, which the model
    sees between its message and its answer once the turn is before the window."""


class Parsed(typing.NamedTuple):
    """What an item is, as ``Items.read`` parses it."""

    said: tuple[str, str] | None = None
    """The role (``user`` or ``assistant``) and text of a message, or ``None`` for any other item."""
    calls: Sequence[Call] = ()
    """The tool calls it makes."""
    results: Sequence[Result] = ()
    """The tool calls' results it holds."""


class Items(Protocol):
    """What a framework's items are: how the shared code stores, reads and makes them.

    A framework may also make the items that give the model the summary of
    the earliest turns, with ``summary(text) -> list[Item]``; by default they
    are a user's message with the summary and an assistant's acknowledgement.
    """

    def dump(self, item: Item) -> str:
        """Return ``item`` as it is stored: its JSON, most often."""
        ...

    def load(self, stored: str) -> Item:
        """Return the item that ``dump`` stored as ``stored``."""
        ...

    def prompt(self, text: str) -> Item:
        """Return the item of a prompt (the user's message)."""
        ...

    def reply(self, text: str, agent: str | None = None) -> Item:
        """Return the item of an assistant's message with ``text``, from ``agent`` if it is known.

        It closes a run; and it is an earlier turn's answer, as the model sees it.
        """
        ...

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        """Return the items that give each call its result, as text, which close a run's cut-off calls."""
        ...

    def read(self, item: Item) -> Parsed:
        """Return what ``item`` says, and the tool calls and results it holds, parsing it once."""
        ...


SUMMARY = "A summary of our conversation so far:\n\n"
"""What the user's message with the summary of the earliest turns starts with."""


def summary_items(items: Items, summary: str) -> list[Item]:
    """Return the items that give the model ``summary``, of the earliest turns: ``items.summary`` if it has one."""
    own = getattr(items, "summary", None)
    if own is not None:
        return list(own(summary))
    return [items.prompt(SUMMARY + summary), items.reply("Understood.")]


def load_item(items: Items, stored: str) -> Item:
    """Return the item stored as ``stored``, or fail for good: a retry would read the same row again.

    A row that ``items`` cannot read was stored by another framework's agent of the same name, or by another
    version of this framework, say.
    """
    try:
        return items.load(stored)
    except Exception as error:
        raise TerminalError(
            f"This conversation holds a message that this agent cannot read (stored by an agent of another "
            f"framework under the same name, or by another version of its framework?): {type(error).__name__}: "
            f"{error}"
        ) from error


@dataclass(frozen=True)
class Start:
    """Where a conversation starts: its first position, the last ``seq`` given, and the summary of the earliest turns."""

    n: int = 0
    seq: int = 0
    summary: str | None = None
    summarized: int | None = None
    """The position of the first turn after the summary: those before it are the summary, as the model sees them."""


async def start_of(db: DatabaseReader) -> Start:
    """Return where the conversation in ``db`` starts, as ``clear``, drops and summaries left it."""
    row = await db.get(f"SELECT n, seq, summary, summarized FROM {START}")
    if not row:
        return Start()
    return Start(_int(row["n"]) or 0, _int(row["seq"]) or 0, _str(row["summary"]), _int(row["summarized"]))


async def last_of(db: DatabaseReader) -> tuple[int | None, int]:
    """Return the last position of ``db``'s rows (``None`` if it has none), and the last ``seq`` they have (or 0)."""
    # One subquery per maximum: each reads the end of its b-tree, where one SELECT of several scans the table.
    [[turn, turn_seq, answered_seq, step, step_seq]] = await db.values(
        f"SELECT (SELECT MAX(n) FROM {TURNS}), (SELECT MAX(seq) FROM {TURNS}), (SELECT MAX(answered_seq) FROM {TURNS}), "
        f"(SELECT MAX(n) FROM {STEPS}), (SELECT MAX(seq) FROM {STEPS})"
    )
    positions = [int(typing.cast(int, n)) for n in (turn, step) if n is not None]
    seqs = [int(typing.cast(int, s)) for s in (turn_seq, answered_seq, step_seq) if s is not None]
    return (max(positions) if positions else None), max(seqs, default=0)


async def next_start(db: DatabaseReader) -> Start:
    """Return where the next conversation starts, past the positions and ``seq`` of the one in ``db``."""
    start = await start_of(db)
    last, seq = await last_of(db)
    return Start(start.n if last is None else last + 1, max(start.seq, seq))


async def restart(tx: DatabaseWriter, start: Start) -> None:
    """Store ``start`` as where the conversation starts: in the transaction of ``clear`` that emptied it, say."""
    await tx.execute(
        f"INSERT OR REPLACE INTO {START} (row, n, seq, summary, summarized) VALUES (0, ?, ?, ?, ?)",
        [start.n, start.seq, start.summary, start.summarized],
    )


@dataclass(frozen=True)
class Snapshot:
    """The history as a framework's own compaction left it: it stands for every position up to ``covers``.

    An agent that takes its history from snapshots (``Conversation.load``
    with ``snapshots=True``) gives the model the latest snapshot's items, then
    every item stored after ``covers``, whole. Snapshots are the model's only:
    the views never show them, and they take no position of their own.
    """

    covers: int
    """The last position the snapshot stands for."""
    turn: int | None
    """The position of the turn it was stored in, or ``None`` if it was stored between turns (by ``compact``)."""
    items: Sequence[Item]
    """The framework's items, in order."""
    results: frozenset[str]
    """The ids of the tool calls whose results its items hold: the model was given them."""


def _snapshot_of(items: Items, covers: int, turn: int | None, held: Sequence[Item]) -> Snapshot:
    results = frozenset(result.id for item in held for result in items.read(item).results)
    return Snapshot(covers, turn, tuple(held), results)


async def latest_snapshot(db: DatabaseReader, items: Items) -> Snapshot | None:
    """Return the latest snapshot stored in ``db``, if there is one."""
    row = await db.get(f"SELECT covers, turn, items FROM {SNAPSHOTS} ORDER BY covers DESC, id DESC LIMIT 1")
    if row is None:
        return None
    held = [load_item(items, str(stored)) for stored in json.loads(str(row["items"]))]
    return _snapshot_of(items, int(typing.cast(int, row["covers"])), _int(row["turn"]), held)


async def answer_in(db: DatabaseReader, prompt_id: str) -> tuple[str, Status | None] | None:
    """Return the stored answer of prompt ``prompt_id``, and how its turn ended, if it has one: one read, of its row."""
    row = await db.get(f"SELECT answer, status FROM {TURNS} WHERE prompt = ?", [prompt_id])
    answer = _str(row["answer"]) if row else None
    if row is None or answer is None:
        return None
    return answer, typing.cast("Status | None", _str(row["status"]))


class _Walk:
    """Tells, of the turns from the latest back, which are in a window of ``size`` turns.

    The window is the ``size`` latest turns that their framework answered, and
    the turns after them that it did not: the one being answered (only the
    latest can have no answer), and those another invocation ended (status
    ``ended``), whose framework never finished them, so that what they
    carried is a guess and their own rows say what happened.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.seen = 0
        self.done = False

    def holds(self, status: Status | None) -> bool:
        """Return whether the next turn back, which ended as ``status`` (or not yet), is in the window."""
        if self.done:
            return False
        if status is None or status == "ended":
            return True
        if self.seen == self.size:
            self.done = True
            return False
        self.seen += 1
        self.done = self.seen == self.size
        return True


def window_start(turns: Sequence[Entry], window: Window) -> int:
    """Return the index in ``turns`` (prompts, in order) of the first turn in ``window`` (see ``_Walk``)."""
    if window == "all":
        return 0
    walk, first = _Walk(window), len(turns)
    while first > 0 and walk.holds(turns[first - 1].status):
        first -= 1
    return first


class _Ledger:
    """What a conversation and its turns share: the database, the next position and ``seq``, and the writes' lock.

    Writes take the lock one after another (tool calls run at once append
    their results one after another), so each gets positions of its own.
    """

    def __init__(self, db: Database, items: Items, start: Start, transcript: list[Entry], next: int, seq: int) -> None:
        self.db = db
        self.items = items
        self.start = start
        self.transcript = transcript
        """The turns' prompts, in order: the latest is the latest turn's."""
        self.next = next
        """The next position to give."""
        self.seq = seq
        """The last ``seq`` given."""
        self.latest: Turn | None = None
        """The latest turn, if it is loaded whole: the only one that may be written."""
        self.closed = False
        """Whether ``Conversation.seal`` closed the conversation to writes."""
        self.lock = asyncio.Lock()
        self.ids: set[str] = set()
        """The ids of the tool calls made in the loaded turns."""
        self.warned = False
        """Whether a call's id that an earlier call has was logged: once is enough."""
        self.snapshot: Snapshot | None = None
        """The latest snapshot, if the conversation was loaded with snapshots and has one."""
        self.compaction = False
        """Whether the run asked for the history to be compacted after the turn (``Conversation.compact_after``)."""

    def called(self, call_id: str) -> None:
        """Note a tool call's id, and log one that an earlier call has (once)."""
        if call_id in self.ids and not self.warned:
            self.warned = True
            _logger.warning(
                "Tool call id %r is used by more than one call: the conversation matches results to calls by id, "
                "so a call may be taken as answered by another's result",
                call_id,
            )
        self.ids.add(call_id)

    def check_open(self) -> None:
        if self.closed:
            raise RuntimeError("the run was stopped: it stores nothing more")

    async def store_snapshot(self, held: Sequence[Item], turn: int | None) -> Snapshot:
        """Store ``held`` as the snapshot that covers every position given so far, in turn ``turn``; hold the lock.

        It takes the place of the earlier snapshots: only the latest is read.
        """
        covers = self.next - 1
        stored = json.dumps([self.items.dump(item) for item in held])
        async with self.db.transaction() as tx:
            await tx.execute(f"DELETE FROM {SNAPSHOTS}")
            await tx.execute(f"INSERT INTO {SNAPSHOTS} (covers, turn, items) VALUES (?, ?, ?)", [covers, turn, stored])
        self.snapshot = _snapshot_of(self.items, covers, turn, held)
        return self.snapshot

    def carried(self) -> tuple[str | None, str | None]:
        """Return the agent and the framework's state that the latest answered turn left."""
        latest = next((turn for turn in reversed(self.transcript) if turn.answer is not None), None)
        return (latest.agent, latest.carried) if latest is not None else (None, None)


_INSERT_STEP = f"INSERT INTO {STEPS} (n, turn, seq, item, reply, agent) VALUES (?, ?, ?, ?, ?, ?)"
"""Store a step of a turn: its position, its turn's, its ``seq``, its item, and the reply and agent it is of."""

_ANSWER = (
    f"UPDATE {TURNS} SET status = ?, answer = ?, agent = ?, carried = ?, answered_at = ?, answered_seq = ? WHERE n = ?"
)
"""Store a turn's answer on its row: how it ended, the answer, what it carries, its message's position, its ``seq``."""


class Turn:
    """One turn of the conversation: its prompt, and what its run stored to answer it.

    A turn's rows are read and written here, and only here: its open tool
    calls, its replies and its final reply are this turn's, never another's.
    Only the latest turn, while it has no answer, can be written to: by its
    own run, or by whoever closes it (``close``), once its run is gone.
    """

    def __init__(self, ledger: _Ledger, prompt: Entry, items: Sequence[Entry] = ()) -> None:
        self._ledger = ledger
        self.prompt = prompt
        """The turn's row: its prompt, and once it has them, its status, answer and what it carried."""
        self._entries: list[Entry] = []
        self._results: dict[str, Result] = {}
        """The tool calls' results, by their calls' ids."""
        self._open: list[Call] = []
        """The tool calls that have no result, in the order they were made: each of them, even if ids repeat."""
        self._carried: dict[str, str] = {}
        """What the turn carries to the turns after it (``agent``, ``carried``), stored with its answer."""
        self._add([prompt, *items])

    def __repr__(self) -> str:
        return f"Turn({self.id!r}, n={self.n}, status={self.status!r})"

    @property
    def id(self) -> str:
        """The turn's id: its prompt's (see ``next_prompt_id``)."""
        return typing.cast(str, self.prompt.prompt)

    @property
    def n(self) -> int:
        """The turn's position: its prompt's, the id of its user's message."""
        return self.prompt.n

    @property
    def status(self) -> Status | None:
        """How the turn ended, or ``None`` while it is being answered."""
        return self.prompt.status

    @property
    def answer(self) -> str | None:
        """The turn's answer, once it has one: the run's, or the note that closed it."""
        return self.prompt.answer

    @property
    def entries(self) -> Sequence[Entry]:
        """The turn's entries, in order: its prompt, then its items."""
        return self._entries

    @property
    def items(self) -> Sequence[Entry]:
        """The turn's items, in order: the model's replies, the tool calls' results, ..."""
        return self._entries[1:]

    def _add(self, entries: list[Entry]) -> None:
        for entry in entries:
            entry.said, entry.calls, entry.results = self._ledger.items.read(entry.item)
            entry.turn = self.n
            self._index(entry)
        self._entries.extend(entries)

    def _index(self, entry: Entry, *, note: bool = True) -> None:
        for call in entry.calls:
            if note:
                self._ledger.called(call.id)
            if call.id not in self._results:
                self._open.append(call)
        for result in entry.results:
            self._results[result.id] = result
            if any(call.id == result.id for call in self._open):
                self._open = [call for call in self._open if call.id != result.id]

    # --- Reading ---

    def replies(self) -> list[int]:
        """Return the ids of the model's replies in the turn, in order."""
        return sorted({entry.reply for entry in self._entries if entry.reply is not None})

    def last_reply(self) -> int | None:
        """Return the id of the model's reply that the turn ends with, if it ends with one."""
        return self._entries[-1].reply

    def reply_entries(self, reply: int) -> Sequence[Entry]:
        """Return the entries of the model's reply ``reply``, in order: they follow one another, from position ``reply``."""
        first = bisect.bisect_left(self._entries, reply, key=_position)
        end = first
        while end < len(self._entries) and self._entries[end].reply == reply:
            end += 1
        return self._entries[first:end]

    def agent_of(self, reply: int) -> str | None:
        """Return the agent that gave the model's reply ``reply``."""
        entries = self.reply_entries(reply)
        return entries[0].agent if entries else None

    def final_reply(self) -> str | None:
        """Return the text of the model's reply the turn ends with, if it ends the run: text, and no tool calls."""
        reply = self.last_reply()
        if reply is None:
            return None
        entries = self.reply_entries(reply)
        if any(e.calls for e in entries):
            return None
        said = [e.said for e in entries if e.said is not None]
        return said[-1][1] if said else None

    def open_calls(self) -> list[Call]:
        """Return the turn's tool calls that have no result, in the order they were made."""
        return list(self._open)

    def latest_ask(self) -> Entry:
        """Return the turn's latest message of the user that asks for its answer: its prompt, or the message that
        asked again (``ask_again``).

        A framework that runs from a message of its own (ADK's invocation, a
        LangGraph graph's input) runs from this one. A steered message
        (``steer``) is not one: the model reads it within the run, which goes
        on from the same ask.
        """
        return next(
            (e for e in reversed(self.items) if e.steer is None and e.said is not None and e.said[0] == "user"),
            self.prompt,
        )

    def steered(self) -> list[Entry]:
        """Return the messages steered to the turn that it took, in order."""
        return [entry for entry in self.items if entry.steer is not None]

    def has_result(self, call_id: str) -> bool:
        """Return whether the turn's tool call ``call_id`` has a result."""
        return call_id in self._results

    def stored(self, call_id: str, failed: str | None = None) -> bool:
        """Return whether tool call ``call_id`` has its result: not an error, unless it is ``failed``, its own."""
        result = self._results.get(call_id)
        return result is not None and (not result.failed or result.output == failed)

    # --- Writing: only the latest turn, while it has no answer ---

    async def append(self, *items: Item) -> None:
        """Append ``items`` (a tool's results, say) to the turn, in one transaction.

        Appends may run at once, such as those of tool calls that run at once:
        each is stored after the ones before it.
        """
        await self._insert(lambda n: [Entry(n + i, item) for i, item in enumerate(items)])

    async def append_reply(self, *items: Item, agent: str | None = None) -> int:
        """Append the model's reply, of ``items`` (given by ``agent``), to the turn, and return its id."""
        return await self._insert(lambda n: [Entry(n + i, item, reply=n, agent=agent) for i, item in enumerate(items)])

    async def ask_again(self, text: str) -> None:
        """Ask the model again within the turn, with ``text``: a message of the user that is a step of the turn.

        It is the turn's, not a turn of its own: the transcript (and the
        ``messages`` view's summary) has the turn's prompt and its answer, as
        the user asked it.
        """
        await self.append(self._ledger.items.prompt(text))

    async def steer(
        self,
        messages: Sequence[Steer],
        sent: Callable[[Transaction, list[str]], None] | None = None,
        going: Callable[[], bool] | None = None,
    ) -> list[Entry]:
        """Store the steered ``messages`` that are not stored yet as messages of the user in the turn, and return them.

        Each is stored once, by its id, as a step of the turn after those
        before: the model reads it in its next request, and in every turn's
        history after, between the turn's message and its answer. ``sent``
        adds what goes out with the transaction that stores them (the
        companion's ``taken``), which commits once.

        ``going`` tells whether the turn still goes on: once it has the
        ledger, a turn that stopped meanwhile takes none, which are then
        answered as turns of their own. A store under way finishes even if the
        turn stops: the turn then closes after it, at the positions after its
        messages, and the messages are its own (stopped with it).
        """
        ledger = self._ledger

        async def store() -> list[Entry]:
            async with ledger.lock:
                if going is not None and not going():
                    return []
                self._check_writable()
                wanted = list({message["id"]: message for message in messages}.values())
                if not wanted:
                    return []
                ids = [message["id"] for message in wanted]
                marks = ", ".join("?" for _ in ids)
                # The database, not the turn: an id is stored once in the conversation, by whichever turn took it.
                stored = await ledger.db.values(f"SELECT id FROM {STEERED} WHERE id IN ({marks})", ids)
                known = {str(row[0]) for row in stored}
                new = [message for message in wanted if message["id"] not in known]
                if not new:
                    return []
                n = ledger.next
                added = [
                    Entry(n + i, ledger.items.prompt(message["text"]), steer=message["id"])
                    for i, message in enumerate(new)
                ]
                async with ledger.db.transaction() as tx:
                    for i, entry in enumerate(added, 1):
                        dumped = ledger.items.dump(entry.item)
                        await tx.execute(_INSERT_STEP, [entry.n, self.n, ledger.seq + i, dumped, None, None])
                        await tx.execute(
                            f"INSERT INTO {STEERED} (n, turn, id) VALUES (?, ?, ?)", [entry.n, self.n, entry.steer]
                        )
                    if sent is not None:
                        sent(tx, [typing.cast(str, entry.steer) for entry in added])
                ledger.seq += len(added)
                ledger.next = n + len(added)
                self._add(added)
                # The transcript's entry of the turn has them too, as the next turn loads it: for its size, say.
                self.prompt.steers.extend(added)
                return added

        # Shielded: a stop cancels the run, but a commit that is under way still lands, and the turn's own record of
        # its positions with it, so that the turn's close stores after it rather than over it.
        return await asyncio.shield(store())

    def carry(self, *, agent: str | None = None, state: str | None = None) -> None:
        """Keep what the turn carries to the turns after it: it is stored with its answer.

        Args:
            agent: The agent the conversation is with. By default, the agent of the turn's latest reply,
                or else the previous turn's.
            state: The framework's own state, as text (ADK's session state, say): the latest is kept.
                By default, the previous turn's.
        """
        if agent is not None:
            self._carried["agent"] = agent
        if state is not None:
            self._carried["carried"] = state

    async def snapshot(self, items: Sequence[Item]) -> None:
        """Store ``items`` as a snapshot of the conversation, as the framework's own compaction left it.

        The snapshot stands for every position stored so far, the turn's own
        included: an agent that takes its history from snapshots gives the
        model, from the next request on, ``items`` and then what is stored
        after it (see ``Conversation.history``). A retried attempt carries on
        from it. Only the turn being answered stores one.
        """
        ledger = self._ledger
        async with ledger.lock:
            self._check_writable()
            await ledger.store_snapshot(list(items), self.n)

    async def drop(self, reply: int) -> None:
        """Drop the turn's reply ``reply``, and what came after it, to ask the model again.

        It is one range delete on the primary key: the reply's items start at
        position ``reply``, and what follows them (their tools' results, say)
        was made from the reply. The positions are given again, so the new
        reply's messages take the dropped ones' ids; the ``seq`` is kept, so
        that a client's cursor still sees them.

        A steered message after the reply is dropped too: it is no longer the
        turn's, so the agent's ``steered`` answers it as a turn of its own.
        """
        self._check_writable()
        if reply not in self.replies():
            raise KeyError(f"turn {self.id!r} has no reply {reply}")
        ledger = self._ledger
        if ledger.snapshot is not None and ledger.snapshot.covers >= reply:
            raise RuntimeError(f"turn {self.id!r} has a snapshot that covers reply {reply}: it cannot be dropped")
        async with ledger.lock:
            self._check_writable()
            start = replace(ledger.start, seq=ledger.seq)
            async with ledger.db.transaction() as tx:
                await tx.execute(f"DELETE FROM {STEPS} WHERE n >= ?", [reply])
                await tx.execute(f"DELETE FROM {STEERED} WHERE n >= ?", [reply])
                await restart(tx, start)
            ledger.start = start
            ledger.next = min(ledger.next, reply)
            self.prompt.steers = [steered for steered in self.prompt.steers if steered.n < reply]
            cut = bisect.bisect_left(self._entries, reply, key=_position)
            kept, dropped = self._entries[:cut], self._entries[cut:]
            # The dropped calls are not made: their ids may be made again, by the reply that replaces them.
            ledger.ids.difference_update(call.id for entry in dropped for call in entry.calls)
            ledger.ids.update(call.id for entry in kept for call in entry.calls)
            self._entries, self._results, self._open = kept, {}, []
            for entry in kept:
                self._index(entry, note=False)

    async def answered(self, answer: str, status: Status = "answered") -> None:
        """Store the turn's ``answer``, how it ended (``status``) and what it carries, in one update of its row.

        The answer's message is the message of the assistant that the turn
        ends with, whose id the ``messages`` view's summary gives it. A turn
        that ends otherwise gets ``answer`` as its reply first, from the agent
        the turn is with; an empty answer gets none, and has no message.
        """
        at = self._answer_message()
        if at is None and answer.strip():
            agent = self._agent()
            at = await self.append_reply(self._ledger.items.reply(answer, agent), agent=agent)
        ledger = self._ledger
        async with ledger.lock:
            self._check_writable()
            agent = self._agent()
            carried = self._carried.get("carried", ledger.carried()[1])
            seq = ledger.seq + 1
            await ledger.db.execute(_ANSWER, [status, answer, agent, carried, at, seq, self.n])
            ledger.seq = seq
            prompt = self.prompt
            prompt.status, prompt.answer, prompt.agent, prompt.carried = status, answer, agent, carried
            prompt.answered_at, prompt.answered_seq = at, seq

    def _answer_message(self) -> int | None:
        """Return the position of the message of the assistant that the turn ends with, if it ends with one.

        That is a message with no tool calls, with no call nor result after it: items that are neither (a
        reasoning item, say) do not count. A turn that ends otherwise (with a tool's output that is the
        answer, say) has none.
        """
        for entry in reversed(self.items):
            if entry.said is None and not entry.calls and not entry.results:
                continue
            if entry.said is not None and entry.said[0] == "assistant" and not entry.calls:
                return entry.n
            return None
        return None

    def _agent(self) -> str | None:
        """Return the agent the turn is with: the one it carries, else its latest reply's, else the previous turn's."""
        replied = next((e.agent for e in reversed(self._entries) if e.reply is not None), None)
        return self._carried.get("agent") or replied or self._ledger.carried()[0]

    async def close(self, ctx: SessionContext, status: Status, note: str) -> None:
        """Close the turn (stopped, refused, failed, or ended once its invocation is gone) with ``note``.

        Each of the turn's tool calls without a result gets one: the error its
        Restate call returned, a note that it did happen if it returned, or
        else ``note``. Then ``note`` is the turn's last message, and its
        answer. The calls' outcomes are those of the invocation that made the
        turn's prompt: this one, or an earlier one whose calls are still
        stored. A turn that has its answer has nothing to close.

        The results, the note and the answer are stored in one transaction: a
        crash between them would leave the turn without its answer, and the
        next attempt would close it again, with a second note.
        """
        if self.status is not None:
            return
        ledger = self._ledger
        items = ledger.items
        calls = self.open_calls()
        returned = await outcomes(ctx, [call.id for call in calls], invocation_of(self.id))
        closing = [
            *items.results([(call, cut_off_result(returned, call.id, note)) for call in calls]),
            items.reply(note),
        ]
        async with ledger.lock:
            self._check_writable()
            n, seq = ledger.next, ledger.seq
            added = [Entry(n + i, item) for i, item in enumerate(closing)]
            at = added[-1].n  # the note: the turn's last message, and its answer
            answered_seq = seq + len(added) + 1
            agent = self._agent()
            carried = self._carried.get("carried", ledger.carried()[1])
            async with ledger.db.transaction() as tx:
                for i, entry in enumerate(added, 1):
                    await tx.execute(_INSERT_STEP, [entry.n, self.n, seq + i, items.dump(entry.item), None, None])
                await tx.execute(_ANSWER, [status, note, agent, carried, at, answered_seq, self.n])
            ledger.next = n + len(added)
            ledger.seq = answered_seq
            self._add(added)
            prompt = self.prompt
            prompt.status, prompt.answer, prompt.agent, prompt.carried = status, note, agent, carried
            prompt.answered_at, prompt.answered_seq = at, answered_seq

    def _check_writable(self) -> None:
        """Refuse a write unless this is the latest turn and has no answer: only its run (or its close) writes it."""
        self._ledger.check_open()
        if self._ledger.latest is not self or self.status is not None:
            raise RuntimeError(f"turn {self.id!r} is not the turn being answered: only that turn is written")

    async def _insert(self, made: Callable[[int], list[Entry]]) -> int:
        """Store the entries that ``made`` makes from the next position, and return that position.

        The position is taken once the appends before have stored theirs, so
        appends that run at once each get places of their own.
        """
        ledger = self._ledger
        async with ledger.lock:
            self._check_writable()
            n = ledger.next
            added = made(n)
            rows = [
                [e.n, self.n, ledger.seq + i, ledger.items.dump(e.item), e.reply, e.agent]
                for i, e in enumerate(added, 1)
            ]
            if len(rows) == 1:
                # One statement commits on its own.
                await ledger.db.execute(_INSERT_STEP, rows[0])
            else:
                async with ledger.db.transaction() as tx:
                    # The same SQL for each row: the database prepares it once.
                    for row in rows:
                        await tx.execute(_INSERT_STEP, row)
            ledger.seq += len(rows)
            ledger.next = n + len(rows)
            self._add(added)
            return n


class Conversation:
    """The conversation in the key's database, loaded once and kept in step with it.

    The conversation is turns (``Turn``): the transcript, every turn's prompt
    and answer, as far back as a turn needs it; and the window, the latest
    turns whole. A turn is read and written through its ``Turn``, and only the
    latest one is written; the conversation composes the turns into what the
    model gets (``history``), until ``seal`` closes it to writes.

    Tool calls and their results are matched by the call's id, within a turn:
    each call's id must be its own, as model providers make them. A call whose
    id an earlier call has (or none, read as ``""``) is logged, as its result
    may be taken for the other's.
    """

    def __init__(
        self,
        db: Database,
        items: Items,
        entries: list[Entry],
        start: Start | None = None,
        *,
        transcript: list[Entry] | None = None,
        last: int | None = None,
        seq: int = 0,
        budget: int = BUDGET,
        window: Window = "all",
        snapshots: bool = False,
        snapshot: Snapshot | None = None,
    ) -> None:
        """Make the conversation of ``entries``, as ``load`` loaded them from ``db``.

        Args:
            db: The key's database.
            items: The framework's items.
            entries: The window's entries, in order: each prompt, then its turn's items.
            start: Where the conversation starts.
            transcript: The turns' prompts, in order, the window's being the same objects as in ``entries``:
                by default, those of ``entries``.
            last: The last position stored, if past those of ``entries`` and ``transcript``.
            seq: The last ``seq`` stored.
            budget: How many tokens of the turns before the window ``history`` gives the model.
            window: How many of the latest answered turns ``entries`` holds whole, or ``"all"``.
            snapshots: Whether the model gets its history from snapshots: the latest (``snapshot``), then every
                item of ``entries`` after it. ``window`` and ``budget`` then do not apply.
            snapshot: The latest snapshot, with ``snapshots``.
        """
        self.db = db
        self.snapshots = snapshots
        """Whether the model gets its history from snapshots, rather than from the transcript and the window."""
        self.items = items
        self.budget = budget
        self.size: Window = window
        """How many of the latest answered turns are loaded whole: the window's size."""
        start = start or Start()
        transcript = transcript if transcript is not None else [e for e in entries if e.prompt is not None]
        positions = [n for n in (last, entries[-1].n if entries else None) if n is not None]
        positions.extend(turn.n for turn in transcript[-1:])
        following = max(start.n, max(positions) + 1 if positions else 0)
        self._ledger = _Ledger(db, items, start, transcript, following, max(start.seq, seq))
        self._ledger.snapshot = snapshot
        self._turns: list[Turn] = []
        grouped: list[tuple[Entry, list[Entry]]] = []
        for entry in entries:
            if entry.prompt is not None:
                grouped.append((entry, []))
            elif grouped:
                grouped[-1][1].append(entry)
            else:
                raise ValueError(f"entry {entry.n} comes before any turn's prompt")
        self._turns = [Turn(self._ledger, prompt, items) for prompt, items in grouped]
        if self._turns and transcript and self._turns[-1].prompt is transcript[-1]:
            self._ledger.latest = self._turns[-1]

    @classmethod
    async def load(
        cls,
        db: Database,
        items: Items,
        window: Window = "all",
        budget: int = BUDGET,
        *,
        complete: bool = False,
        snapshots: bool = False,
    ) -> Conversation:
        """Load the conversation from ``db``, whose items are ``items``': the transcript, and the window's turns whole.

        The transcript is read from the latest turn back, in pages, until it
        holds the window and the turns before it that ``budget`` holds (the
        model sees no more of them), or the summary's start: so a turn reads
        a bounded part of a conversation that lives long, whether or not its
        agent summarizes it.

        Args:
            db: The key's database.
            items: The framework's items.
            window: How many of the latest answered turns to load whole, with their items, or ``"all"``. The
                latest turn is loaded whole if it has no answer yet.
            budget: How many tokens of the turns before the window ``history`` gives the model.
            complete: Whether to read the transcript from the summary's start whatever the budget: to summarize it.
            snapshots: Whether the model gets its history from snapshots: the latest snapshot, then every row after
                it, whole (see ``Snapshot``). ``window``, ``budget`` and ``complete`` then do not apply.
        """
        check_window(window)
        await SCHEMA.ensure(db)
        start = await start_of(db)
        last, seq = await last_of(db)
        if snapshots:
            return await cls._from_snapshot(db, items, start, last, seq)
        first = start.n if window == "all" or start.summarized is None else start.summarized
        transcript = await _transcript(db, items, first, None if window == "all" or complete else (window, budget))
        whole = transcript[window_start(transcript, window) :]
        steps = await _steps(db, items, whole[0].n) if whole else []
        entries = sorted([*whole, *steps], key=_position)
        return cls(db, items, entries, start, transcript=transcript, last=last, seq=seq, budget=budget, window=window)

    @classmethod
    async def _from_snapshot(cls, db: Database, items: Items, start: Start, last: int | None, seq: int) -> Conversation:
        """Load the latest snapshot, and the turns after it whole: from the turn it was stored in, if any.

        The latest turn is loaded whole too while it has no answer, so that it
        is carried on or closed; and the transcript starts at the latest
        answered turn before them, for what it carried.
        """
        snapshot = await latest_snapshot(db, items)
        first = start.n
        if snapshot is not None:
            first = max(first, snapshot.turn if snapshot.turn is not None else snapshot.covers + 1)
        latest = await db.get(f"SELECT n, status FROM {TURNS} ORDER BY n DESC LIMIT 1")
        if latest is not None and latest["status"] is None:
            first = min(first, int(typing.cast(int, latest["n"])))
        [[carried]] = await db.values(f"SELECT MAX(n) FROM {TURNS} WHERE n < ? AND answer IS NOT NULL", [first])
        transcript = await _transcript(db, items, first if carried is None else int(typing.cast(int, carried)), None)
        whole = [turn for turn in transcript if turn.n >= first]
        steps = await _steps(db, items, whole[0].n) if whole else []
        entries = sorted([*whole, *steps], key=_position)
        return cls(
            db, items, entries, start, transcript=transcript, last=last, seq=seq, snapshots=True, snapshot=snapshot
        )

    # --- Reading: the conversation as a whole ---

    @property
    def transcript(self) -> Sequence[Entry]:
        """The turns' prompts, in order, from the first after the summary (from the first of all, if loaded whole).

        Each has its turn's ``status`` and ``answer``, and what the turn carried (``agent``, ``carried``), once
        it has them.
        """
        return self._ledger.transcript

    @property
    def turns(self) -> Sequence[Turn]:
        """The window's turns, whole, in order: the last of them is the latest turn."""
        return self._turns

    @property
    def turn(self) -> Turn:
        """The latest turn: the one being answered, during a run.

        Raises:
            RuntimeError: If the latest turn is not loaded whole (or there is none).
        """
        if self._ledger.latest is None:
            raise RuntimeError("the conversation has no turn loaded whole yet: ask first")
        return self._ledger.latest

    def running(self) -> Turn | None:
        """Return the latest turn if it is being answered: it has no answer yet."""
        latest = self._ledger.latest
        return latest if latest is not None and latest.status is None else None

    @property
    def entries(self) -> Sequence[Entry]:
        """The window's turns, flat, in order (by position): each prompt, then its turn's items.

        To read what the model was given, across turns (a framework's ids, say). A turn's own state (its
        open calls, its final reply) is its ``Turn``'s.
        """
        return [entry for turn in self._turns for entry in turn.entries]

    @property
    def summary(self) -> str | None:
        """The summary of the turns before the transcript, if they have one (and the transcript is after them)."""
        if self.snapshots:
            return None
        transcript, start = self._ledger.transcript, self._ledger.start
        first = transcript[0].n if transcript else self._ledger.next
        return start.summary if start.summarized is not None and first >= start.summarized else None

    @property
    def closed(self) -> bool:
        """Whether ``seal`` closed the conversation to writes."""
        return self._ledger.closed

    def answer_of(self, prompt_id: str) -> str | None:
        """Return the answer of the turn of prompt ``prompt_id``, if the transcript has it and it has one."""
        return next((turn.answer for turn in self._ledger.transcript if turn.prompt == prompt_id), None)

    def carried(self) -> tuple[str | None, str | None]:
        """Return the agent the conversation is with, and the framework's state, as the latest answered turn left them."""
        return self._ledger.carried()

    @property
    def snapshot(self) -> Snapshot | None:
        """The latest snapshot, if the model gets its history from snapshots and one is stored."""
        return self._ledger.snapshot

    def knows_result(self, call_id: str) -> bool:
        """Return whether a turn of the window, or the snapshot, holds the result of tool call ``call_id``: the model
        was given it."""
        snapshot = self._ledger.snapshot
        if snapshot is not None and call_id in snapshot.results:
            return True
        return any(turn.has_result(call_id) for turn in self._turns)

    def new_results(self, items: Sequence[Item]) -> list[Item]:
        """Return the items of ``items`` (a framework's run) that hold a tool call's result the window lacks.

        Results are found by their calls' ids, not by their place: what a
        framework hands back may be trimmed, or be the whole conversation again.
        """
        return [item for item in items if any(not self.knows_result(r.id) for r in self.items.read(item).results)]

    def window(self) -> int:
        """Return the position of the window's first turn: the turns before it are in the transcript only."""
        return self._turns[0].n if self._turns else self._ledger.next

    def _before(self) -> list[Entry]:
        """Return the turns before the window, which the model sees as their messages and answers."""
        start = self.window()
        return [turn for turn in self._ledger.transcript if turn.n < start and turn.answer is not None]

    def _size(self, turn: Entry) -> int:
        """Return the size of ``turn`` as the transcript has it, in characters: its message, the messages steered
        to it, and its answer."""
        if turn.said is None:
            turn.said = self.items.read(turn.item).said
        return len(turn.said[1] if turn.said else "") + _steered_size(turn) + len(turn.answer or "")

    def _latest(self, turns: list[Entry], characters: int) -> list[Entry]:
        """Return the latest of ``turns`` that ``characters`` holds, in order."""
        kept = 0
        for turn in reversed(turns):
            characters -= self._size(turn)
            if characters < 0:
                break
            kept += 1
        return turns[len(turns) - kept :]

    def earlier(self) -> list[Entry]:
        """Return the turns before the window that ``history`` gives the model: the latest that ``budget`` holds."""
        return self._latest(self._before(), self.budget * 4)

    def _upcoming(self) -> tuple[list[Entry], int]:
        """Return the turns before the next turn's window, and the position the window starts at.

        That is the window as the next turn loads it, once the latest turn is
        answered (see ``_Walk``).
        """
        transcript = self._ledger.transcript
        if self.size == "all":
            return [], transcript[0].n if transcript else self._ledger.next
        first = window_start(transcript, self.size)
        start = transcript[first].n if first < len(transcript) else self._ledger.next
        return [turn for turn in transcript[:first] if turn.answer is not None], start

    def overflowing(self) -> bool:
        """Return whether the turns before the next turn's window outgrow ``budget``: then they are summarized."""
        if self.snapshots:
            return False
        before, _ = self._upcoming()
        return sum(self._size(turn) for turn in before) > self.budget * 4

    def to_summarize(self) -> tuple[list[Entry], int] | None:
        """Return the turns to summarize, and the position of the first turn after them, if any are to be.

        Those are the turns before the next turn's window but the latest that
        half the ``budget`` holds, which stay as they are: the next turns have
        room.
        """
        before, start = self._upcoming()
        kept = self._latest(before, self.budget * 2)
        summarized = before[: len(before) - len(kept)]
        if not summarized:
            return None
        return summarized, kept[0].n if kept else start

    def history(self) -> list[Item]:
        """Return the items the model gets, in order: the one place where turns are composed, flat.

        That is the summary of the earliest turns, if they have one; the turns
        before the window, as their prompts, the messages steered to them and
        their answers, as many of the latest as ``budget`` holds; then the
        window's turns, whole.

        With snapshots, it is the latest snapshot's items, then every item
        stored after it, whole; before the first snapshot, the whole
        conversation.
        """
        if self.snapshots:
            snapshot = self._ledger.snapshot
            covers = snapshot.covers if snapshot is not None else -1
            held = list(snapshot.items) if snapshot is not None else []
            return held + [entry.item for turn in self._turns for entry in turn.entries if entry.n > covers]
        out: list[Item] = []
        if (summary := self.summary) is not None:
            out.extend(summary_items(self.items, summary))
        for turn in self.earlier():
            out.append(turn.item)
            out.extend(steered.item for steered in turn.steers)
            out.append(self.items.reply(turn.answer or "", turn.agent))
        out.extend(entry.item for turn in self._turns for entry in turn.entries)
        return out

    # --- Writing ---

    async def ask(self, prompt_id: str, text: str) -> Turn:
        """Start the turn of prompt ``prompt_id``, with ``text``, and return it: the latest turn now."""
        ledger = self._ledger
        async with ledger.lock:
            ledger.check_open()
            if (running := self.running()) is not None:
                raise RuntimeError(f"turn {running.id!r} has no answer yet: turns never overlap")
            n = ledger.next
            entry = Entry(n, self.items.prompt(text), prompt=prompt_id, asked_at=int(time.time() * 1000))
            await self.db.execute(
                f"INSERT INTO {TURNS} (n, seq, prompt, item, asked_at) VALUES (?, ?, ?, ?, ?)",
                [n, ledger.seq + 1, prompt_id, self.items.dump(entry.item), entry.asked_at],
            )
            ledger.seq += 1
            ledger.next = n + 1
            ledger.transcript.append(entry)
            turn = Turn(ledger, entry)
            self._turns.append(turn)
            ledger.latest = turn
            return turn

    async def snapshot_between(self, items: Sequence[Item]) -> None:
        """Store ``items`` as a snapshot of the whole conversation, between turns (in ``compact``, say).

        Raises:
            RuntimeError: If a turn is being answered: its run stores its own (``Turn.snapshot``).
        """
        ledger = self._ledger
        async with ledger.lock:
            ledger.check_open()
            if (running := self.running()) is not None:
                raise RuntimeError(f"turn {running.id!r} has no answer yet: it stores its own snapshots")
            await ledger.store_snapshot(list(items), None)

    def compact_after(self) -> None:
        """Ask for the history to be compacted after the turn's answer, in an invocation of its own (``compact``).

        For an agent that takes its history from snapshots, whose framework
        tells when the history nears its limit. ``compact`` then hands the
        history to the agent's ``compact_history``, and stores what it returns
        as a snapshot, so that the next turn starts compacted.
        """
        self._ledger.compaction = True

    @property
    def compaction_asked(self) -> bool:
        """Whether the run asked for the history to be compacted after the turn (``compact_after``)."""
        return self._ledger.compaction

    async def seal(self) -> None:
        """Close the conversation to writes, once those under way are stored.

        For a stopped run: once its turn is closed, the run (unwinding, or a task
        of it left behind) stores nothing more, and any write it tries fails.
        """
        async with self._ledger.lock:
            self._ledger.closed = True


def _position(entry: Entry) -> int:
    return entry.n


PAGE = 64
"""How many turns of the transcript a load reads at a time, from the latest back."""


async def _transcript(db: DatabaseReader, items: Items, first: int, bounded: tuple[int, int] | None) -> list[Entry]:
    """Return the turns from position ``first`` on, in order: all of them, or as many as ``bounded`` needs.

    ``bounded`` is the window and the budget: the turns are read from the
    latest back, a page at a time, and the reading stops once they hold the
    window and more of the turns before it than the budget holds.
    """
    sql = (
        f"SELECT n, prompt, item, status, answer, agent, carried, answered_at, answered_seq, asked_at FROM {TURNS} "
        "WHERE n >= ? AND n < ? ORDER BY n DESC LIMIT ?"
    )
    read: list[Entry] = []
    before = 2**63 - 1
    walk = None if bounded is None else _Walk(bounded[0])
    left = None if bounded is None else bounded[1] * 4
    while True:
        rows = await db.values(sql, [first, before, PAGE if bounded is not None else 2**62])
        # The messages steered to the page's turns: positions between its earliest turn's and the turn after it.
        steered = await _steered_in(db, items, int(typing.cast(int, rows[-1][0])), before) if rows else {}
        for n, prompt, item, status, answer, agent, carried, answered_at, answered_seq, asked_at in rows:
            at = int(typing.cast(int, n))
            entry = Entry(
                at,
                load_item(items, str(item)),
                str(prompt),
                _str(answer),
                agent=_str(agent),
                turn=at,
                carried=_str(carried),
                status=typing.cast(Status | None, _str(status)),
                answered_at=_int(answered_at),
                answered_seq=_int(answered_seq),
                asked_at=_int(asked_at),
                steers=steered.get(at, []),
            )
            read.append(entry)
            if walk is None or left is None or walk.holds(entry.status):
                continue
            if entry.answer is not None:
                # A turn before the window: the model sees its message and answer, while the budget holds them.
                entry.said = items.read(entry.item).said
                left -= len(entry.said[1] if entry.said else "") + _steered_size(entry) + len(entry.answer or "")
                if left < 0:
                    return read[::-1]
        if bounded is None or len(rows) < PAGE:
            return read[::-1]
        before = read[-1].n


async def _steps(db: DatabaseReader, items: Items, after: int) -> list[Entry]:
    """Return the trajectories' entries after position ``after``, in order, each steered message marked as one."""
    stored = await db.values(
        f"SELECT s.n, s.item, s.reply, s.agent, st.id FROM {STEPS} s LEFT JOIN {STEERED} st ON st.n = s.n "
        "WHERE s.n > ? ORDER BY s.n",
        [after],
    )
    return [
        Entry(
            int(typing.cast(int, n)), load_item(items, str(item)), reply=_int(reply), agent=_str(agent), steer=_str(i)
        )
        for n, item, reply, agent, i in stored
    ]


async def _steered_in(db: DatabaseReader, items: Items, first: int, end: int) -> dict[int, list[Entry]]:
    """Return the messages steered to the turns at positions ``first`` (included) to ``end``, by their turn's position.

    It reads only the steered messages: one range of their table's primary key, joined to their items.
    """
    rows = await db.values(
        f"SELECT st.n, st.turn, st.id, s.item FROM {STEERED} st JOIN {STEPS} s ON s.n = st.n "
        "WHERE st.n >= ? AND st.n < ? ORDER BY st.n",
        [first, end],
    )
    steered: dict[int, list[Entry]] = {}
    for n, turn, steer_id, item in rows:
        loaded = load_item(items, str(item))
        at = int(typing.cast(int, turn))
        entry = Entry(int(typing.cast(int, n)), loaded, steer=str(steer_id), turn=at, said=items.read(loaded).said)
        steered.setdefault(at, []).append(entry)
    return steered


def _steered_size(turn: Entry) -> int:
    """Return the size, in characters, of the messages steered to ``turn`` (a prompt of the transcript)."""
    return sum(len(steered.said[1]) if steered.said else 0 for steered in turn.steers)


async def steer_stored(db: DatabaseReader, steer_id: str) -> bool:
    """Return whether a turn took the steered message ``steer_id``: one read, of its unique id."""
    await SCHEMA.ensure(db)
    return await db.get(f"SELECT 1 FROM {STEERED} WHERE id = ?", [steer_id]) is not None


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _int(value: object) -> int | None:
    return None if value is None else int(typing.cast(int, value))


# --- Answering a prompt ---------------------------------------------------------------------------

Run = Callable[[Conversation], Awaitable[str]]
"""A framework's run: from the conversation, whose latest turn (``conversation.turn``) is the one to answer (just
asked, or left unfinished by an earlier attempt), to its answer. It reads what the model gets from the
conversation (``history``), and stores each step in the turn as it goes. A tool's error fails the attempt."""


def invocation_of(prompt_id: str) -> str:
    """Return the id of the invocation that made prompt ``prompt_id`` (see ``next_prompt_id``)."""
    return prompt_id.split(":", 1)[0]


async def answer_once(
    ctx: SessionContext,
    text: str,
    items: Items,
    run: Run,
    window: Window | None = None,
    budget: int | None = None,
    *,
    check: Check | None = None,
) -> str:
    """Answer the prompt ``text`` with a framework's ``run``, exactly once.

    The prompt's id is the invocation's (``next_prompt_id``), so a retried
    attempt returns the stored answer (reading only it), or carries on the
    turn. A turn that an earlier invocation left without an answer is closed
    first (``ENDED``): that invocation is gone.

    Args:
        ctx: The handler's context.
        text: The prompt.
        items: The framework's items.
        run: The framework's run.
        window: How many of the latest answered turns the model sees whole, with their tool calls and results.
            Defaults to the agent's (``AgentObject``'s ``window``).
        budget: How many tokens of the turns before the window the model sees, as their messages and answers.
            Defaults to the agent's.
        check: Checks the answer: if it returns a message, the turn asks the model again with it, once, as a
            step of the turn (``Turn.ask_again``), and the turn's answer is the reply to that.

    An agent that takes its history from snapshots (``AgentInfo.snapshots``) loads the latest snapshot and every
    row after it instead, whatever ``window`` and ``budget``.
    """
    settings = settings_of(ctx)
    window = check_window(settings.window if window is None else window)
    budget = settings.budget if budget is None else budget
    prompt_id = next_prompt_id(ctx)
    await SCHEMA.ensure(ctx.db)
    if (found := await answer_in(ctx.db, prompt_id)) is not None:
        # Answered (or closed) already: a retry answers, or fails, as the turn ended.
        answer, status = found
        return stored_answer(answer, status)
    conversation = await Conversation.load(ctx.db, items, window, budget, snapshots=settings.snapshots)

    async def work() -> str:
        return await _answer(ctx, conversation, prompt_id, text, run, check)

    def stored(call_id: str, failed: str | None) -> bool:
        # The Restate calls of this invocation are its turn's.
        running = conversation.running()
        return running is not None and running.id == prompt_id and running.stored(call_id, failed)

    answered = await answering(ctx, stored, work)
    definition = definition_of(ctx)
    if settings.snapshots:
        due = settings.compact_history is not None and conversation.compaction_asked
    else:
        due = settings.summarize is not None and conversation.overflowing()
    if due and definition is not None:
        # Compacted in an invocation of its own, which the key runs before its next message.
        ctx.generic_send(definition.name, "compact", key=ctx.key, idempotency_key=f"{prompt_id}:compact")
    return answered


async def _answer(
    ctx: SessionContext, conversation: Conversation, prompt_id: str, text: str, run: Run, check: Check | None
) -> str:
    running = conversation.running()
    if running is not None and running.id != prompt_id:
        # Turns never overlap, so the invocation of this one is gone: it ends unanswered.
        await running.close(ctx, "ended", ENDED)
        running = None
    turn = running or await conversation.ask(prompt_id, text)

    async def answering() -> str:
        answer = await run(conversation)
        if check is None:
            return answer
        # Once at most: a turn that asked again (in an earlier attempt, say) answers with the reply to that, or
        # its check fails it (a terminal error, which closes it failed, below).
        asked = turn.latest_ask() is not turn.prompt
        again = check(answer, asked)
        if again is not None and not asked:
            await turn.ask_again(again)
            answer = await run(conversation)
            check(answer, True)
        return answer

    stop = stop_signal_of(ctx)
    going = answering()
    try:
        answer = await (stop.guard(going) if stop is not None else going)
    except AttemptEnded:
        # The attempt ended: stop here, and let the next attempt carry on the run.
        raise
    except TerminalError as error:
        if is_cancellation(error):
            # Cancelled: close the turn, so that the next prompt starts afresh.
            await turn.close(ctx, "cancelled", CANCELLED)
            await conversation.seal()
            raise
        # Failed for good: close the turn with the error, so that the next prompt does not run into it again.
        await turn.close(ctx, "failed", f"Error: {error.message}")
        raise
    except Exception as error:
        # Refused for good (a wrong API key, say): close the turn unanswered and fail with a terminal error.
        refused = refusal_of(ctx, error)
        if refused is None:
            raise
        await turn.close(ctx, "refused", UNANSWERED)
        raise refused from error
    await turn.answered(answer)
    return stored_answer(answer)


async def settle(ctx: SessionContext, items: Items, invocation_id: str) -> bool:
    """Close the latest turn (``ended``) if invocation ``invocation_id`` made it and left it without an answer.

    The companion has this done when it finds the turn's invocation gone. It
    is idempotent: a turn that has its answer (closed by a newer turn first,
    say) is left as it is. Return whether it closed the turn.
    """
    await SCHEMA.ensure(ctx.db)
    row = await ctx.db.get(f"SELECT prompt, status FROM {TURNS} ORDER BY n DESC LIMIT 1")
    if row is None or row["status"] is not None or invocation_of(str(row["prompt"])) != invocation_id:
        return False
    running = (await Conversation.load(ctx.db, items, 0)).running()
    if running is None:
        return False
    await running.close(ctx, "ended", ENDED)
    return True


def _transcribed(conversation: Conversation, turns: Sequence[Entry]) -> str:
    """Return ``turns`` as text, after the summary of the turns before them, if any: what is summarized."""
    lines = [f"Summary of the conversation before:\n{conversation.summary}\n"] if conversation.summary else []
    for turn in turns:
        said = turn.said[1] if turn.said else ""
        steered = "".join(f"User: {steer.said[1] if steer.said else ''}\n" for steer in turn.steers)
        lines.append(f"User: {said}\n{steered}Assistant: {turn.answer or ''}\n")
    return "\n".join(lines)


async def compact(ctx: SessionContext, items: Items) -> bool:
    """Summarize the turns before the window that outgrow the agent's budget, with the agent's ``summarize``.

    The summary, of the earlier summary and those turns, takes their place in
    what the model sees (``Conversation.history``); the turns stay stored,
    and the ``messages`` view shows them as before. A summary that fails is
    logged, and tried again after the next turn. Return whether it
    summarized.

    An agent that takes its history from snapshots compacts it with its
    ``compact_history`` instead, and stores what that returns as a snapshot
    between turns: the next turn starts from it.
    """
    settings = settings_of(ctx)
    if settings.snapshots:
        return await _compact_history(ctx, items)
    if settings.summarize is None:
        return False
    conversation = await Conversation.load(ctx.db, items, settings.window, settings.budget, complete=True)
    if not conversation.overflowing() or (found := conversation.to_summarize()) is None:
        return False
    turns, after = found
    try:
        summary = await settings.summarize(ctx, SUMMARIZE + _transcribed(conversation, turns))
    except Exception as error:
        _logger.warning("Summarizing the earlier turns failed, and is tried again after the next turn: %s", error)
        return False
    start = replace(await start_of(ctx.db), summary=summary, summarized=after)
    await restart(ctx.db, start)
    return True


async def _compact_history(ctx: SessionContext, items: Items) -> bool:
    """Compact the history of an agent that takes it from snapshots, with its ``compact_history``: see ``compact``."""
    compact_history = settings_of(ctx).compact_history
    if compact_history is None:
        return False
    conversation = await Conversation.load(ctx.db, items, snapshots=True)
    if conversation.running() is not None:
        # A turn left without an answer (its invocation gone): the next message closes it first.
        return False
    try:
        compacted = await compact_history(ctx, conversation.history())
    except Exception as error:
        _logger.warning("Compacting the history failed, and is tried again when a turn asks for it: %s", error)
        return False
    if compacted is None:
        return False
    await conversation.snapshot_between(compacted)
    return True
