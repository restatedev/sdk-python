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
  own state (``Conversation.carry``).
- The **trajectories**, ``restate_agent_items``: every other item (the
  model's replies, tool calls and their results, compaction items), each
  with its turn. A turn is completed from its own trajectory.

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
import logging
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from restate.agents._calls import outcomes
from restate.agents._common import (
    BUDGET,
    CANCELLATION,
    CANCELLED,
    ENDED,
    SUMMARIZE,
    UNANSWERED,
    Window,
    check_window,
    cut_off_result,
    next_prompt_id,
    refusal_of,
    stored_answer,
)
from restate.agents._turn import AttemptEnded, answering, settings_of, stop_signal_of
from restate.exceptions import TerminalError
from restate.sessions import Database, DatabaseReader, DatabaseWriter, SessionContext
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

SCHEMA = Schema(
    "restate_agents.turns",
    [
        # The conversation as one table, from before turns had their own: not kept.
        "DROP TABLE IF EXISTS restate_agent_conversation",
        f"DROP TABLE IF EXISTS {START}",
        f"""CREATE TABLE {TURNS} (
            n INTEGER PRIMARY KEY,
            seq INTEGER NOT NULL,
            prompt TEXT NOT NULL UNIQUE,
            item TEXT NOT NULL,
            answer TEXT,
            agent TEXT,
            carried TEXT
        )""",
        f"CREATE INDEX {TURNS}_seq ON {TURNS} (seq)",
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
    ],
)


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
    """On a prompt: the framework's state as of the end of its turn, if it carries one (``Conversation.carry``)."""


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
    [[turn, turn_seq]] = await db.values(f"SELECT MAX(n), MAX(seq) FROM {TURNS}")
    [[step, step_seq]] = await db.values(f"SELECT MAX(n), MAX(seq) FROM {STEPS}")
    positions = [int(typing.cast(int, n)) for n in (turn, step) if n is not None]
    seqs = [int(typing.cast(int, s)) for s in (turn_seq, step_seq) if s is not None]
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


async def answer_in(db: DatabaseReader, prompt_id: str) -> str | None:
    """Return the stored answer of prompt ``prompt_id``, if it has one: one read, of the turn's row."""
    row = await db.get(f"SELECT answer FROM {TURNS} WHERE prompt = ?", [prompt_id])
    return _str(row["answer"]) if row else None


class _Walk:
    """Tells, of the turns from the latest back, which are in a window of ``size`` turns.

    The window is the ``size`` latest turns that their framework answered, and
    the turns after them that it did not: the one being answered (only the
    latest can have no answer), and those another invocation ended
    (``ENDED``), whose framework never finished them, so that what they
    carried is a guess and their own rows say what happened.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.seen = 0
        self.done = False

    def holds(self, answer: str | None) -> bool:
        """Return whether the next turn back, with ``answer``, is in the window."""
        if self.done:
            return False
        if answer is None or answer == ENDED:
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
    while first > 0 and walk.holds(turns[first - 1].answer):
        first -= 1
    return first


class Conversation:
    """The conversation in the key's database, loaded once and kept in step with it.

    It is read from memory: ``transcript``, ``entries`` and the methods under
    *Reading*. Only its own methods change it: ``ask``, ``append``,
    ``append_reply``, ``answered`` and ``drop`` write to the database and to
    memory alike, one after another, until ``seal`` closes it to writes.

    ``entries`` are the window's turns, whole: each prompt, then its turn's
    items, in order. They always reach the end of the conversation, so the
    run of the latest turn reads them. Each of their items is read (its text,
    calls and results) once, as it is loaded or appended.

    Tool calls and their results are matched by the call's id, within the
    window: each call's id must be its own, as model providers make them. A
    call whose id an earlier call has (or none, read as ``""``) is logged, as
    its result may be taken for the other's.
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
    ) -> None:
        """Make the conversation of ``entries``, as ``load`` loaded them from ``db``.

        Args:
            db: The key's database.
            items: The framework's items.
            entries: The window's entries, in order: its prompts, and their items.
            start: Where the conversation starts.
            transcript: The turns' prompts, in order, the window's being the same objects as in ``entries``:
                by default, those of ``entries``.
            last: The last position stored, if past those of ``entries`` and ``transcript``.
            seq: The last ``seq`` stored.
            budget: How many tokens of the turns before the window ``history`` gives the model.
            window: How many of the latest answered turns ``entries`` holds whole, or ``"all"``.
        """
        self.db = db
        self.items = items
        self.budget = budget
        self.size: Window = window
        """How many of the latest answered turns are loaded whole: the window's size."""
        self._start = start or Start()
        self._transcript = transcript if transcript is not None else [e for e in entries if e.prompt is not None]
        positions = [n for n in (last, entries[-1].n if entries else None) if n is not None]
        positions.extend(turn.n for turn in self._transcript[-1:])
        self._next = max(self._start.n, max(positions) + 1 if positions else 0)
        """The next position to give."""
        self._seq = max(self._start.seq, seq)
        """The last ``seq`` given."""
        self._entries: list[Entry] = []
        self._results: dict[str, Result] = {}
        """The tool calls' results, by their calls' ids."""
        self._open: list[Call] = []
        """The tool calls that have no result, in the order they were made: each of them, even if ids repeat."""
        self._called: set[str] = set()
        """The ids of the tool calls made."""
        self._prompts: dict[str, Entry] = {e.prompt: e for e in self._transcript if e.prompt is not None}
        """The turns, by their prompts' ids."""
        self._carried: dict[str, str] = {}
        """What the turn being answered carries (``agent``, ``carried``), stored with its answer."""
        self._warned = False
        """Whether a call's id that an earlier call has was logged: once is enough."""
        self._closed = False
        self._writing = asyncio.Lock()
        """Held while the conversation changes: tool calls run at once append their results one after another."""
        self._add(entries)

    def _add(self, entries: list[Entry]) -> None:
        for entry in entries:
            entry.said, entry.calls, entry.results = self.items.read(entry.item)
            self._index(entry)
        self._entries.extend(entries)

    def _index(self, entry: Entry) -> None:
        for call in entry.calls:
            if call.id in self._called and not self._warned:
                self._warned = True
                _logger.warning(
                    "Tool call id %r is used by more than one call: the conversation matches results to calls by id, "
                    "so a call may be taken as answered by another's result",
                    call.id,
                )
            self._called.add(call.id)
            if call.id not in self._results:
                self._open.append(call)
        for result in entry.results:
            self._results[result.id] = result
            if any(call.id == result.id for call in self._open):
                self._open = [call for call in self._open if call.id != result.id]

    def _reindex(self) -> None:
        self._results.clear()
        self._open.clear()
        self._called.clear()
        for entry in self._entries:
            self._index(entry)

    @classmethod
    async def load(
        cls, db: Database, items: Items, window: Window = "all", budget: int = BUDGET, *, complete: bool = False
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
        """
        check_window(window)
        await SCHEMA.ensure(db)
        start = await start_of(db)
        last, seq = await last_of(db)
        first = start.n if window == "all" or start.summarized is None else start.summarized
        transcript = await _transcript(db, items, first, None if window == "all" or complete else (window, budget))
        whole = transcript[window_start(transcript, window) :]
        steps: list[Entry] = []
        if whole:
            stored = await db.values(
                f"SELECT n, turn, item, reply, agent FROM {STEPS} WHERE n > ? ORDER BY n", [whole[0].n]
            )
            steps = [
                Entry(
                    int(typing.cast(int, n)),
                    items.load(str(item)),
                    reply=_int(reply),
                    agent=_str(agent),
                    turn=_int(turn),
                )
                for n, turn, item, reply, agent in stored
            ]
        entries = sorted([*whole, *steps], key=_position)
        return cls(db, items, entries, start, transcript=transcript, last=last, seq=seq, budget=budget, window=window)

    # --- Reading ---

    @property
    def entries(self) -> Sequence[Entry]:
        """The entries of the window's turns, in order (by position): each prompt, and its turn's items.

        To read: the conversation changes only through its own methods.
        """
        return self._entries

    @property
    def transcript(self) -> Sequence[Entry]:
        """The turns' prompts, in order, from the first after the summary (from the first of all, if loaded whole).

        Each has its turn's ``answer``, and what the turn carried (``agent``, ``carried``) once answered.
        """
        return self._transcript

    @property
    def summary(self) -> str | None:
        """The summary of the turns before the transcript, if they have one (and the transcript is after them)."""
        first = self._transcript[0].n if self._transcript else self._next
        summarized = self._start.summarized
        return self._start.summary if summarized is not None and first >= summarized else None

    @property
    def closed(self) -> bool:
        """Whether ``seal`` closed the conversation to writes."""
        return self._closed

    def window(self) -> int:
        """Return the position of the window's first turn: the turns before it are in the transcript only."""
        return self._entries[0].n if self._entries else self._next

    def _before(self) -> list[Entry]:
        """Return the turns before the window, which the model sees as their messages and answers."""
        start = self.window()
        return [turn for turn in self._transcript if turn.n < start and turn.answer is not None]

    def _size(self, turn: Entry) -> int:
        """Return the size of ``turn`` as the transcript has it, in characters: its message and its answer."""
        if turn.said is None:
            turn.said = self.items.read(turn.item).said
        return len(turn.said[1] if turn.said else "") + len(turn.answer or "")

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
        if self.size == "all":
            return [], self._transcript[0].n if self._transcript else self._next
        first = window_start(self._transcript, self.size)
        start = self._transcript[first].n if first < len(self._transcript) else self._next
        return [turn for turn in self._transcript[:first] if turn.answer is not None], start

    def overflowing(self) -> bool:
        """Return whether the turns before the next turn's window outgrow ``budget``: then they are summarized."""
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
        """Return the items the model gets, in order: what a framework's run starts from.

        That is the summary of the earliest turns, if they have one; the turns
        before the window, as their prompts and answers, as many of the latest
        as ``budget`` holds; then the window's turns, whole.
        """
        out: list[Item] = []
        if (summary := self.summary) is not None:
            out.extend(summary_items(self.items, summary))
        for turn in self.earlier():
            out.extend((turn.item, self.items.reply(turn.answer or "", turn.agent)))
        out.extend(entry.item for entry in self._entries)
        return out

    def after(self, n: int) -> Sequence[Entry]:
        """Return the entries after position ``n``, in order: those of the run that the prompt at ``n`` started, say."""
        return self._entries[bisect.bisect_right(self._entries, n, key=_position) :]

    def reply_entries(self, reply: int) -> Sequence[Entry]:
        """Return the entries of the model's reply ``reply``, in order: they follow one another, from position ``reply``."""
        first = bisect.bisect_left(self._entries, reply, key=_position)
        end = first
        while end < len(self._entries) and self._entries[end].reply == reply:
            end += 1
        return self._entries[first:end]

    def answer_of(self, prompt_id: str) -> str | None:
        """Return the answer of prompt ``prompt_id``, if it has one."""
        prompt = self._prompts.get(prompt_id)
        return prompt.answer if prompt is not None else None

    def running(self) -> Entry | None:
        """Return the prompt of the latest turn if it has no answer yet: the turn being answered."""
        latest = self._transcript[-1] if self._transcript else None
        return latest if latest is not None and latest.answer is None else None

    def last_reply(self) -> int | None:
        """Return the id of the model's reply that the conversation ends with, if it ends with one."""
        return self._entries[-1].reply if self._entries else None

    def agent_of(self, reply: int) -> str | None:
        """Return the agent that gave the model's reply ``reply``."""
        entries = self.reply_entries(reply)
        return entries[0].agent if entries else None

    def has_result(self, call_id: str) -> bool:
        """Return whether tool call ``call_id`` has a result."""
        return call_id in self._results

    def stored(self, call_id: str, failed: str | None = None) -> bool:
        """Return whether tool call ``call_id`` has its result: not an error, unless it is ``failed``, its own."""
        result = self._results.get(call_id)
        return result is not None and (not result.failed or result.output == failed)

    def unanswered(self) -> list[Call]:
        """Return the tool calls of the window that have no result, in the order they were made."""
        return list(self._open)

    def new_results(self, items: Sequence[Item]) -> list[Item]:
        """Return the items of ``items`` (a framework's run) that hold a tool call's result the conversation lacks.

        Results are found by their calls' ids, not by their place: what a
        framework hands back may be trimmed, or be the whole conversation again.
        """
        return [item for item in items if any(r.id not in self._results for r in self.items.read(item).results)]

    def final_reply(self) -> str | None:
        """Return the text of the model's reply the conversation ends with, if it ends the run: text, and no tool calls."""
        reply = self.last_reply()
        if reply is None:
            return None
        entries = self.reply_entries(reply)
        if any(e.calls for e in entries):
            return None
        said = [e.said for e in entries if e.said is not None]
        return said[-1][1] if said else None

    # --- Writing ---

    async def ask(self, prompt_id: str, text: str) -> None:
        """Start the turn of prompt ``prompt_id``, with ``text``."""
        async with self._writing:
            self._check_open()
            n = self._next
            entry = Entry(n, self.items.prompt(text), prompt=prompt_id, turn=n)
            await self.db.execute(
                f"INSERT INTO {TURNS} (n, seq, prompt, item) VALUES (?, ?, ?, ?)",
                [n, self._seq + 1, prompt_id, self.items.dump(entry.item)],
            )
            self._seq += 1
            self._next = n + 1
            self._transcript.append(entry)
            self._prompts[prompt_id] = entry
            self._carried = {}
            self._add([entry])

    async def append(self, *items: Item) -> None:
        """Append ``items`` (a tool's results, say) to the latest turn, in one transaction.

        Appends may run at once, such as those of tool calls that run at once:
        each is stored after the ones before it.
        """
        await self._insert(lambda n, turn: [Entry(n + i, item, turn=turn) for i, item in enumerate(items)])

    async def append_reply(self, *items: Item, agent: str | None = None) -> int:
        """Append the model's reply, of ``items`` (given by ``agent``), to the latest turn, and return its id."""
        return await self._insert(
            lambda n, turn: [Entry(n + i, item, reply=n, agent=agent, turn=turn) for i, item in enumerate(items)]
        )

    def carry(self, *, agent: str | None = None, state: str | None = None) -> None:
        """Keep what the turn being answered carries to the turns after it: it is stored with its answer.

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

    def carried(self) -> tuple[str | None, str | None]:
        """Return the agent the conversation is with, and the framework's state, as the latest answered turn left them."""
        latest = next((turn for turn in reversed(self._transcript) if turn.answer is not None), None)
        return (latest.agent, latest.carried) if latest is not None else (None, None)

    async def answered(self, prompt_id: str, answer: str) -> None:
        """Store ``answer`` as the answer of prompt ``prompt_id``, which the conversation has, with what it carries."""
        prompt = self._prompts.get(prompt_id)
        if prompt is None:
            raise KeyError(f"the conversation has no prompt {prompt_id!r}")
        async with self._writing:
            self._check_open()
            agent, carried = self.carried()
            replied = next(
                (e.agent for e in reversed(self._entries) if e.turn == prompt.n and e.reply is not None), None
            )
            agent = self._carried.get("agent") or replied or agent
            carried = self._carried.get("carried", carried)
            await self.db.execute(
                f"UPDATE {TURNS} SET answer = ?, agent = ?, carried = ? WHERE prompt = ?",
                [answer, agent, carried, prompt_id],
            )
            prompt.answer, prompt.agent, prompt.carried = answer, agent, carried

    async def drop(self, reply: int) -> None:
        """Drop the model's reply ``reply``, and what came after it, to ask the model again.

        It is one range delete on the primary key: the reply's items start at
        position ``reply``, and what follows them (their tools' results, say)
        was made from the reply. The positions are given again, so the new
        reply's messages take the dropped ones' ids; the ``seq`` is kept, so
        that a client's cursor still sees them.
        """
        async with self._writing:
            self._check_open()
            start = replace(self._start, seq=self._seq)
            async with self.db.transaction() as tx:
                await tx.execute(f"DELETE FROM {STEPS} WHERE n >= ?", [reply])
                await restart(tx, start)
            self._start = start
            del self._entries[bisect.bisect_left(self._entries, reply, key=_position) :]
            self._next = min(self._next, reply)
            self._reindex()

    async def seal(self) -> None:
        """Close the conversation to writes, once those under way are stored.

        For a stopped run: once its run is closed, the run (unwinding, or a task
        of it left behind) stores nothing more, and any write it tries fails.
        """
        async with self._writing:
            self._closed = True

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the run was stopped: it stores nothing more")

    async def _insert(self, made: Callable[[int, int], list[Entry]]) -> int:
        """Store the entries that ``made`` makes from the next position, in the latest turn, and return that position.

        The position is taken once the appends before have stored theirs, so
        appends that run at once each get places of their own.
        """
        async with self._writing:
            self._check_open()
            if not self._transcript:
                raise RuntimeError("the conversation has no turn to append to: ask first")
            n = self._next
            added = made(n, self._transcript[-1].n)
            sql = f"INSERT INTO {STEPS} (n, turn, seq, item, reply, agent) VALUES (?, ?, ?, ?, ?, ?)"
            rows = [
                [e.n, e.turn, self._seq + i, self.items.dump(e.item), e.reply, e.agent] for i, e in enumerate(added, 1)
            ]
            if len(rows) == 1:
                # One statement commits on its own.
                await self.db.execute(sql, rows[0])
            else:
                async with self.db.transaction() as tx:
                    # The same SQL for each row: the database prepares it once.
                    for row in rows:
                        await tx.execute(sql, row)
            self._seq += len(rows)
            self._next = n + len(rows)
            self._add(added)
            return n


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
    sql = f"SELECT n, prompt, item, answer, agent, carried FROM {TURNS} WHERE n >= ? AND n < ? ORDER BY n DESC LIMIT ?"
    read: list[Entry] = []
    before = 2**63 - 1
    walk = None if bounded is None else _Walk(bounded[0])
    left = None if bounded is None else bounded[1] * 4
    while True:
        rows = await db.values(sql, [first, before, PAGE if bounded is not None else 2**62])
        for n, prompt, item, answer, agent, carried in rows:
            at = int(typing.cast(int, n))
            entry = Entry(
                at, items.load(str(item)), str(prompt), _str(answer), agent=_str(agent), turn=at, carried=_str(carried)
            )
            read.append(entry)
            if walk is None or left is None or walk.holds(entry.answer):
                continue
            if entry.answer is not None:
                # A turn before the window: the model sees its message and answer, while the budget holds them.
                entry.said = items.read(entry.item).said
                left -= len(entry.said[1] if entry.said else "") + len(entry.answer or "")
                if left < 0:
                    return read[::-1]
        if bounded is None or len(rows) < PAGE:
            return read[::-1]
        before = read[-1].n


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _int(value: object) -> int | None:
    return None if value is None else int(typing.cast(int, value))


# --- Answering a prompt ---------------------------------------------------------------------------

Run = Callable[[Conversation], Awaitable[str]]
"""A framework's run: from the conversation (which ends with a prompt, or with the run an earlier attempt left
unfinished) to the prompt's answer, appending each step as it goes. A tool's error fails the attempt."""


def invocation_of(prompt_id: str) -> str:
    """Return the id of the invocation that made prompt ``prompt_id`` (see ``next_prompt_id``)."""
    return prompt_id.split(":", 1)[0]


async def close(ctx: SessionContext, conversation: Conversation, text: str) -> None:
    """Close the turn being answered (stopped, refused, failed or ended) with ``text``.

    Each tool call without a result gets one: the error its Restate call
    returned, a note that it did happen if it returned, or else ``text``.
    Then ``text`` is the reply, and the prompt's answer. A conversation whose
    latest turn has its answer has nothing to close.

    The calls' outcomes are those of the invocation that made the turn's
    prompt: this one, or an earlier one whose calls are still stored.
    """
    running = conversation.running()
    if running is None or running.prompt is None:
        return
    calls = conversation.unanswered()
    returned = await outcomes(ctx, [call.id for call in calls], invocation_of(running.prompt))
    results = conversation.items.results([(call, cut_off_result(returned, call.id, text)) for call in calls])
    await conversation.append(*results, conversation.items.reply(text))
    await conversation.answered(running.prompt, text)


async def answer_once(
    ctx: SessionContext, text: str, items: Items, run: Run, window: Window | None = None, budget: int | None = None
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
    """
    settings = settings_of(ctx)
    window = check_window(settings.window if window is None else window)
    budget = settings.budget if budget is None else budget
    prompt_id = next_prompt_id(ctx)
    await SCHEMA.ensure(ctx.db)
    if (answer := await answer_in(ctx.db, prompt_id)) is not None:
        return stored_answer(answer)
    conversation = await Conversation.load(ctx.db, items, window, budget)

    async def work() -> str:
        return await _answer(ctx, conversation, prompt_id, text, run)

    answered = await answering(ctx, conversation.stored, work)
    definition = definition_of(ctx)
    if settings.summarize is not None and definition is not None and conversation.overflowing():
        # Summarized in an invocation of its own, which the key runs before its next message.
        ctx.generic_send(definition.name, "compact", key=ctx.key, idempotency_key=f"{prompt_id}:compact")
    return answered


async def _answer(ctx: SessionContext, conversation: Conversation, prompt_id: str, text: str, run: Run) -> str:
    running = conversation.running()
    if running is not None and running.prompt != prompt_id:
        # Turns never overlap, so the invocation of this one is gone: it ends unanswered.
        await close(ctx, conversation, ENDED)
    if conversation.running() is None:
        await conversation.ask(prompt_id, text)
    stop = stop_signal_of(ctx)
    going = run(conversation)
    try:
        answer = await (stop.guard(going) if stop is not None else going)
    except AttemptEnded:
        # The attempt ended: stop here, and let the next attempt carry on the run.
        raise
    except TerminalError as error:
        if error.status_code == CANCELLATION:
            # Cancelled: close the run, so that the next prompt starts afresh.
            await close(ctx, conversation, CANCELLED)
            await conversation.seal()
            raise
        # Failed for good: close the run with the error, so that the next prompt does not run into it again.
        await close(ctx, conversation, f"Error: {error.message}")
        raise
    except Exception as error:
        # Refused for good (a wrong API key, say): close the run unanswered and fail with a terminal error.
        refused = refusal_of(ctx, error)
        if refused is None:
            raise
        await close(ctx, conversation, UNANSWERED)
        raise refused from error
    await conversation.answered(prompt_id, answer)
    return stored_answer(answer)


async def settle(ctx: SessionContext, items: Items, invocation_id: str) -> bool:
    """Close the latest turn with ``ENDED`` if invocation ``invocation_id`` made it and left it without an answer.

    The companion has this done when it finds the turn's invocation gone. It
    is idempotent: a turn that has its answer (closed by a newer turn first,
    say) is left as it is. Return whether it closed the turn.
    """
    await SCHEMA.ensure(ctx.db)
    row = await ctx.db.get(f"SELECT prompt, answer FROM {TURNS} ORDER BY n DESC LIMIT 1")
    if row is None or row["answer"] is not None or invocation_of(str(row["prompt"])) != invocation_id:
        return False
    conversation = await Conversation.load(ctx.db, items, 0)
    await close(ctx, conversation, ENDED)
    return True


def _transcribed(conversation: Conversation, turns: Sequence[Entry]) -> str:
    """Return ``turns`` as text, after the summary of the turns before them, if any: what is summarized."""
    lines = [f"Summary of the conversation before:\n{conversation.summary}\n"] if conversation.summary else []
    for turn in turns:
        said = turn.said[1] if turn.said else ""
        lines.append(f"User: {said}\nAssistant: {turn.answer or ''}\n")
    return "\n".join(lines)


async def compact(ctx: SessionContext, items: Items) -> bool:
    """Summarize the turns before the window that outgrow the agent's budget, with the agent's ``summarize``.

    The summary, of the earlier summary and those turns, takes their place in
    what the model sees (``Conversation.history``); the turns stay stored,
    and the ``messages`` view shows them as before. A summary that fails is
    logged, and tried again after the next turn. Return whether it
    summarized.
    """
    settings = settings_of(ctx)
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
