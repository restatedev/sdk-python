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

The conversation is one table, ``restate_agent_conversation``, of the
framework's own items (its messages), one row each, in order. The framework
stores and loads its items itself (``Items.dump`` and ``Items.load``), so
that an item goes from the database to the framework's own object in one
step, such as pydantic's ``validate_json``.

A prompt's item carries the prompt's id, and its answer once there is one; a
model reply's items carry the reply's id (the position of its first item) and
the agent that gave it. Rows are only ever appended, in one transaction per
step, but for a reply at the end that is dropped (to ask the model again),
which is one range delete on the primary key; and a prompt's answer is one
update of its row, by its unique prompt id.

The conversation is loaded whole, once per prompt, since the framework's
model gets all of it. Each item is read (its text, calls and results) once,
as it is loaded or appended, and kept beside it.

On top of it, ``answer_once`` runs a prompt the same way for every framework:
each prompt is answered once per invocation, an unfinished run (cut off by a
crash) is finished first, and a stopped, refused or failed run is closed so
that the next prompt starts afresh. A framework provides its ``Items`` (how
its items say what they are) and a ``Run`` (its own loop, from the
conversation to the answer, storing each step with ``append`` as it goes).
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from restate.agents._calls import outcomes
from restate.agents._common import (
    CANCELLATION,
    CANCELLED,
    UNANSWERED,
    cut_off_result,
    next_prompt_id,
    refusal_of,
    stored_answer,
)
from restate.agents._transcript import Transcript
from restate.agents._turn import AttemptEnded, stop_signal_of, transcribing
from restate.exceptions import TerminalError
from restate.sessions import Database, DatabaseReader, SessionContext
from restate.sessions.internal import Schema

Item: typing.TypeAlias = Any
"""An item of the conversation: a framework's own message (a pydantic model, a dict, ...)."""

TABLE = "restate_agent_conversation"
"""The table of the conversation, in the key's database."""

SCHEMA = Schema(
    "restate_agents.conversation",
    [
        f"""CREATE TABLE {TABLE} (
            n INTEGER PRIMARY KEY,
            item TEXT NOT NULL,
            prompt TEXT UNIQUE,
            answer TEXT,
            reply INTEGER,
            agent TEXT
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


@dataclass
class Entry:
    """An item of the conversation, with what it is part of."""

    n: int
    """Its position; the conversation is ordered by it."""
    item: Item
    prompt: str | None = None
    """The id of the prompt it is, if it is one."""
    answer: str | None = None
    """The prompt's answer, once it has one."""
    reply: int | None = None
    """The id of the model's reply it is part of, if it is: the position of the reply's first item."""
    agent: str | None = None
    """The agent that gave the reply."""
    said: tuple[str, str] | None = None
    """The role (``user`` or ``assistant``) and text it says, if it is a message: read from ``item`` once, as the
    conversation loads or appends it, like ``calls`` and ``results``."""
    calls: Sequence[Call] = ()
    """The tool calls it makes."""
    results: Sequence[Result] = ()
    """The tool calls' results it holds."""


class Parsed(typing.NamedTuple):
    """What an item is, as ``Items.read`` parses it."""

    said: tuple[str, str] | None = None
    """The role (``user`` or ``assistant``) and text of a message, or ``None`` for any other item."""
    calls: Sequence[Call] = ()
    """The tool calls it makes."""
    results: Sequence[Result] = ()
    """The tool calls' results it holds."""


class Items(Protocol):
    """What a framework's items are: how the shared code stores, reads and makes them."""

    def dump(self, item: Item) -> str:
        """Return ``item`` as it is stored: its JSON, most often."""
        ...

    def load(self, stored: str) -> Item:
        """Return the item that ``dump`` stored as ``stored``."""
        ...

    def prompt(self, text: str) -> Item:
        """Return the item of a prompt (the user's message)."""
        ...

    def reply(self, text: str) -> Item:
        """Return the item of an assistant's message with ``text``, which closes a run."""
        ...

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        """Return the items that give each call its result, as text, which close a run's cut-off calls."""
        ...

    def read(self, item: Item) -> Parsed:
        """Return what ``item`` says, and the tool calls and results it holds, parsing it once."""
        ...


class Conversation:
    """The conversation in the key's database, loaded once and kept in step with it.

    It is read from memory; ``append``, ``answered`` and ``drop`` write to the
    database and to memory alike. Each item is read (its calls and results)
    once, as it is loaded or appended.
    """

    def __init__(self, db: Database, items: Items, entries: list[Entry]) -> None:
        self.db = db
        self.items = items
        self.entries: list[Entry] = []
        self._results: set[str] = set()
        """The ids of the tool calls that have a result."""
        self._prompts: dict[str, Entry] = {}
        """The prompts, by id."""
        self._transcript: Transcript | None = None
        self.closed = False
        """Whether a stop closed the run: the run, unwinding, then stores nothing more."""
        self._add(entries)

    def _add(self, entries: list[Entry]) -> None:
        for entry in entries:
            entry.said, entry.calls, entry.results = self.items.read(entry.item)
            self._results.update(result.id for result in entry.results)
            if entry.prompt is not None:
                self._prompts[entry.prompt] = entry
            if self._transcript is not None:
                self._transcribe(entry)
        self.entries.extend(entries)

    @classmethod
    async def load(cls, db: Database, items: Items) -> Conversation:
        """Load the conversation from ``db``, whose items are ``items``'."""
        await SCHEMA.ensure(db)
        return cls(db, items, await _entries(db, items))

    # --- Reading ---

    def answer_of(self, prompt_id: str) -> str | None:
        """Return the answer of prompt ``prompt_id``, if it has one."""
        prompt = self._prompts.get(prompt_id)
        return prompt.answer if prompt is not None else None

    def running(self) -> Entry | None:
        """Return the prompt whose run the conversation ends with, if it has no answer yet."""
        prompt = next((e for e in reversed(self.entries) if e.prompt is not None), None)
        return prompt if prompt is not None and prompt.answer is None else None

    def last_reply(self) -> int | None:
        """Return the id of the model's reply that the conversation ends with, if it ends with one."""
        return self.entries[-1].reply if self.entries else None

    def agent_of(self, reply: int) -> str | None:
        """Return the agent that gave the model's reply ``reply``."""
        return next((e.agent for e in self.entries if e.reply == reply), None)

    def has_result(self, call_id: str) -> bool:
        """Return whether tool call ``call_id`` has a result."""
        return call_id in self._results

    def unanswered(self) -> list[Call]:
        """Return the tool calls of the conversation that have no result."""
        return [call for e in self.entries for call in e.calls if call.id not in self._results]

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
        entries = [e for e in self.entries if e.reply == reply]
        if any(e.calls for e in entries):
            return None
        said = [e.said for e in entries if e.said is not None]
        return said[-1][1] if said else None

    def transcript(self) -> Transcript:
        """Return the conversation in the form the ``messages`` view returns, kept up to date as items are appended."""
        if self._transcript is None:
            self._transcript = Transcript()
            for entry in self.entries:
                self._transcribe(entry)
        return self._transcript

    def _transcribe(self, entry: Entry) -> None:
        transcript = self.transcript()
        if entry.said is not None:
            role, text = entry.said
            transcript.say("user" if role == "user" else "assistant", text)
        for call in entry.calls:
            transcript.call(call.id, call.name, call.input)
        for result in entry.results:
            transcript.result(result.id, result.output, result.error)

    # --- Writing ---

    async def append(self, *items: Item) -> None:
        """Append ``items`` (a tool's results, say), in one transaction."""
        await self._insert([Entry(n, item) for n, item in enumerate(items, start=self._next())])

    async def append_reply(self, *items: Item, agent: str | None = None) -> int:
        """Append the model's reply, of ``items`` (given by ``agent``), in one transaction, and return its id."""
        reply = self._next()
        await self._insert([Entry(n, item, reply=reply, agent=agent) for n, item in enumerate(items, start=reply)])
        return reply

    def _next(self) -> int:
        return self.entries[-1].n + 1 if self.entries else 0

    async def ask(self, prompt_id: str, text: str) -> None:
        """Append prompt ``prompt_id``, with ``text``."""
        await self._insert([Entry(self._next(), self.items.prompt(text), prompt=prompt_id)])

    async def answered(self, prompt_id: str, answer: str) -> None:
        """Store ``answer`` as the answer of prompt ``prompt_id``."""
        await self.db.execute(f"UPDATE {TABLE} SET answer = ? WHERE prompt = ?", [answer, prompt_id])
        self._prompts[prompt_id].answer = answer

    async def drop(self, reply: int) -> None:
        """Drop the model's reply ``reply``, and what came after it, to ask the model again.

        It is one range delete on the primary key: the reply's items start at
        position ``reply``, and what follows them (their tools' results, say)
        was made from the reply.
        """
        await self.db.execute(f"DELETE FROM {TABLE} WHERE n >= ?", [reply])
        kept = [e for e in self.entries if e.n < reply]
        for dropped in self.entries[len(kept) :]:
            self._results.difference_update(result.id for result in dropped.results)
            if dropped.prompt is not None:
                del self._prompts[dropped.prompt]
        self.entries = kept
        self._transcript = None

    async def _insert(self, added: list[Entry]) -> None:
        if self.closed:
            raise RuntimeError("the run was stopped: it stores nothing more")
        sql = f"INSERT INTO {TABLE} (n, item, prompt, reply, agent) VALUES (?, ?, ?, ?, ?)"
        rows = [[e.n, self.items.dump(e.item), e.prompt, e.reply, e.agent] for e in added]
        if len(rows) == 1:
            # One statement commits on its own.
            await self.db.execute(sql, rows[0])
        else:
            async with self.db.transaction() as tx:
                # The same SQL for each row: the database prepares it once.
                for row in rows:
                    await tx.execute(sql, row)
        self._add(added)


async def _entries(db: DatabaseReader, items: Items) -> list[Entry]:
    rows = await db.values(f"SELECT n, item, prompt, answer, reply, agent FROM {TABLE} ORDER BY n")
    return [
        Entry(int(typing.cast(int, n)), items.load(str(item)), _str(prompt), _str(answer), _int(reply), _str(agent))
        for n, item, prompt, answer, reply, agent in rows
    ]


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _int(value: object) -> int | None:
    return None if value is None else int(typing.cast(int, value))


# --- Answering a prompt ---------------------------------------------------------------------------

Run = Callable[[Conversation], Awaitable[str]]
"""A framework's run: from the conversation (which ends with a prompt, or with a run it left unfinished)
to the prompt's answer, appending each step as it goes. A tool's error fails the attempt."""


async def close(ctx: SessionContext, conversation: Conversation, text: str) -> None:
    """Close the run that the conversation ends with (cancelled, refused or failed) with ``text``.

    Each tool call without a result gets one: the error its Restate call
    returned, a note that it did happen if it returned, or else ``text``.
    Then ``text`` is the reply, and the prompt's answer. A conversation that
    ends with no unfinished run has nothing to close.
    """
    running = conversation.running()
    if running is None or running.prompt is None:
        return
    calls = conversation.unanswered()
    returned = await outcomes(ctx, [call.id for call in calls])
    results = conversation.items.results([(call, cut_off_result(returned, call.id, text)) for call in calls])
    await conversation.append(*results, conversation.items.reply(text))
    await conversation.answered(running.prompt, text)


async def answer_once(ctx: SessionContext, text: str, items: Items, run: Run) -> str:
    """Answer the prompt ``text`` with a framework's ``run``, exactly once.

    The prompt's id is the invocation's (``next_prompt_id``), so a retried
    attempt returns the stored answer, or finishes the run first. A run that
    an earlier prompt left unfinished is finished before this one starts.
    """
    conversation = await Conversation.load(ctx.db, items)

    async def work() -> str:
        return await _answer(ctx, conversation, text, run)

    return await transcribing(ctx, _read(conversation), work)


def _read(conversation: Conversation) -> Callable[[], Awaitable[Transcript]]:
    async def read() -> Transcript:
        return conversation.transcript()

    return read


async def _answer(ctx: SessionContext, conversation: Conversation, text: str, run: Run) -> str:
    prompt_id = next_prompt_id(ctx)
    while True:
        if (answer := conversation.answer_of(prompt_id)) is not None:
            return stored_answer(answer)
        running = conversation.running()
        if running is None:
            await conversation.ask(prompt_id, text)
            continue
        stop = stop_signal_of(ctx)
        going = run(conversation)
        try:
            answer = await (stop.guard(going) if stop is not None else going)
        except AttemptEnded:
            # The attempt ended: stop here, and let the next attempt continue the run.
            raise
        except TerminalError as error:
            if error.status_code == CANCELLATION:
                # Cancelled: close the run, so that the next prompt starts afresh.
                await close(ctx, conversation, CANCELLED)
                conversation.closed = True
                raise
            # Failed for good: close the run with the error, so that the next prompt does not run into it again.
            await close(ctx, conversation, f"Error: {error.message}")
            if running.prompt == prompt_id:
                raise
            continue
        except Exception as error:
            # Refused for good (a wrong API key, say): close the run unanswered and fail with a terminal error.
            # If the run was an earlier prompt's, carry on with this one.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await close(ctx, conversation, UNANSWERED)
            if running.prompt == prompt_id:
                raise refused from error
            continue
        if running.prompt is not None:
            await conversation.answered(running.prompt, answer)
