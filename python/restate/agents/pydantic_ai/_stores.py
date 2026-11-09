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
"""The stores of Pydantic AI Harness's capabilities, over the key's database.

- ``plan_store``: ``Planning``'s plan, which lasts across turns (by default
  each run, so each turn, gets a fresh one).
- ``overflow_store``: where ``ToolOutputLimits`` spills a large tool result,
  which ``read_tool_result`` reads back in the same turn or a later one.
- ``history_source``: the turns, for ``ConversationSearch``: one run per
  turn, whose messages are the turn's as stored (the originals, which a
  compaction summarized for the model).

They find the key's database in the run of ``agent`` or ``prompt`` in
progress, and ``clear`` empties them with the rest of the conversation. A
tool call runs again after a crash, so the plan's reads and writes in a tool
call are journaled: the call gets what each returned before, and so makes the
writes it made before, each done once (see ``_journaled``). A spill is named
by the tool call, as the harness's keys are made of the run's id, which every
attempt has its own of.
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import typing
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage
from pydantic_ai_harness.planning import PlanItem, TaskStatus
from pydantic_ai_harness.step_persistence import RunRecord

from restate.agents._conversation import STEPS, TURNS, Item, Items, load_item
from restate.agents._tools import current_call
from restate.sessions import SessionContext
from restate.sessions.internal import Schema, definition_of

PLAN = "restate_pydantic_ai_plan"
"""``Planning``'s plan: its steps, in order."""
SPILLS = "restate_pydantic_ai_spills"
"""``ToolOutputLimits``' spilled tool results, by handle."""
JOURNAL = "restate_pydantic_ai_journal"
"""What the plan's reads and writes in each tool call returned, by the invocation, the call, and their order in it."""

SCHEMA = Schema(
    "restate_agents.pydantic_ai",
    [
        f"CREATE TABLE {PLAN} (position INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, item TEXT NOT NULL)",
        f"CREATE TABLE {SPILLS} (handle TEXT PRIMARY KEY, data TEXT NOT NULL)",
        f"""CREATE TABLE {JOURNAL} (
            invocation TEXT NOT NULL,
            call TEXT NOT NULL,
            n INTEGER NOT NULL,
            result TEXT NOT NULL,
            PRIMARY KEY (invocation, call, n)
        )""",
    ],
)


@dataclasses.dataclass
class _Call:
    """The tool call running, and how far it got: its reads and writes of the plan, and its writes of memory."""

    id: str
    steps: int = 0
    """How many reads and writes of the plan it made (``_journaled``)."""
    writes: int = 0
    """How many writes of memory it made (``_memory.py``)."""


CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar("restate_pydantic_ai_call", default=None)


def calling(call_id: str | None) -> None:
    """Mark tool call ``call_id`` as running (``None``: none is): ``_Restate`` does, as each starts and ends."""
    CALL.set(_Call(call_id) if call_id is not None else None)


def running() -> SessionContext:
    """Return the handler's context of the run in progress (``tools_in``)."""
    return current_call("").ctx


def conversation_id_of(ctx: SessionContext) -> str:
    """Return the id of the conversation of ``ctx``'s key for Pydantic AI: ``<agent>/<key>``.

    Every run of the key has it, and ``history_source`` lists the turns under it.
    """
    definition = definition_of(ctx)
    return f"{definition.name}/{ctx.key}" if definition is not None else ctx.key


async def _db() -> Any:
    ctx = running()
    await SCHEMA.ensure(ctx.db)
    return ctx.db


T = typing.TypeVar("T")


async def _journaled(
    op: Callable[[Any], Awaitable[T]], dump: Callable[[T], str], load: Callable[[str], T], *, writes: bool
) -> T:
    """Do ``op``, a read of the plan (with the database) or, if ``writes``, a write (with a transaction), and return
    its result: once for the tool call running.

    A call that runs again (after a crash) gets what each of its reads and
    writes returned before, told apart by their order in it, and so makes the
    writes it made before, each done once. Its reads are journaled too: its
    earlier writes are committed, and reading them would change what it writes
    next (which steps a status change unblocks, say), so that its writes would
    not line up with those it made before. Outside a tool call, ``op`` is done
    as it comes. The journals of earlier invocations, whose calls no longer
    run, are dropped.
    """
    ctx = running()
    await SCHEMA.ensure(ctx.db)
    call = CALL.get()
    if call is None:
        if not writes:
            return await op(ctx.db)
        async with ctx.db.transaction() as tx:
            return await op(tx)
    n, call.steps = call.steps, call.steps + 1
    invocation = ctx.request.id
    done = await ctx.db.get(
        f"SELECT result FROM {JOURNAL} WHERE invocation = ? AND call = ? AND n = ?", [invocation, call.id, n]
    )
    if done is not None:
        return load(str(done["result"]))
    async with ctx.db.transaction() as tx:
        result = await op(tx)
        await tx.execute(f"DELETE FROM {JOURNAL} WHERE invocation != ?", [invocation])
        await tx.execute(
            f"INSERT INTO {JOURNAL} (invocation, call, n, result) VALUES (?, ?, ?, ?)",
            [invocation, call.id, n, dump(result)],
        )
    return result


_ITEMS: TypeAdapter[list[PlanItem]] = TypeAdapter(list[PlanItem])


def _dump_item(item: PlanItem | None) -> str:
    return item.model_dump_json() if item is not None else "null"


def _load_item(stored: str) -> PlanItem | None:
    return None if stored == "null" else PlanItem.model_validate_json(stored)


def _item(stored: object) -> PlanItem:
    return PlanItem.model_validate_json(str(stored))


class KeyPlanStore:
    """``Planning``'s ``PlanStore`` over the key's database: the plan lasts across turns. See ``plan_store``."""

    async def _items(self, db: Any) -> list[PlanItem]:
        return [_item(item) for [item] in await db.values(f"SELECT item FROM {PLAN} ORDER BY position")]

    async def get_items(self) -> list[PlanItem]:
        return await _journaled(
            self._items, lambda items: _ITEMS.dump_json(items).decode(), _ITEMS.validate_json, writes=False
        )

    async def get_item(self, item_id: str) -> PlanItem | None:
        async def read(db: Any) -> PlanItem | None:
            row = await db.get(f"SELECT item FROM {PLAN} WHERE id = ?", [item_id])
            return _item(row["item"]) if row is not None else None

        return await _journaled(read, _dump_item, _load_item, writes=False)

    async def set_items(self, items: list[PlanItem]) -> None:
        async def write(tx: Any) -> None:
            await tx.execute(f"DELETE FROM {PLAN}")
            for position, item in enumerate(items):
                await tx.execute(
                    f"INSERT INTO {PLAN} (position, id, item) VALUES (?, ?, ?)",
                    [position, item.id, item.model_dump_json()],
                )

        await _journaled(write, lambda _: "null", lambda _: None, writes=True)

    async def add_item(self, item: PlanItem) -> PlanItem:
        async def write(tx: Any) -> PlanItem:
            if await tx.get(f"SELECT 1 FROM {PLAN} WHERE id = ?", [item.id]) is not None:
                raise ValueError(f"A step with id {item.id!r} is already in this plan.")
            [[last]] = await tx.values(f"SELECT MAX(position) FROM {PLAN}")
            position = 0 if last is None else int(typing.cast(int, last)) + 1
            await tx.execute(
                f"INSERT INTO {PLAN} (position, id, item) VALUES (?, ?, ?)", [position, item.id, item.model_dump_json()]
            )
            return item.model_copy(deep=True)

        return await _journaled(write, PlanItem.model_dump_json, PlanItem.model_validate_json, writes=True)

    async def update_item(
        self,
        item_id: str,
        *,
        content: str | None = None,
        status: TaskStatus | None = None,
        active_form: str | None = None,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> PlanItem | None:
        changes: dict[str, object] = {"content": content, "status": status, "active_form": active_form}
        changes |= {"parent_id": parent_id, "depends_on": list(depends_on) if depends_on is not None else None}

        async def write(tx: Any) -> PlanItem | None:
            row = await tx.get(f"SELECT item FROM {PLAN} WHERE id = ?", [item_id])
            if row is None:
                return None
            item = _item(row["item"]).model_copy(update={k: v for k, v in changes.items() if v is not None})
            await tx.execute(f"UPDATE {PLAN} SET item = ? WHERE id = ?", [item.model_dump_json(), item_id])
            return item

        return await _journaled(write, _dump_item, _load_item, writes=True)

    async def remove_item(self, item_id: str) -> bool:
        async def write(tx: Any) -> bool:
            existed = await tx.get(f"SELECT 1 FROM {PLAN} WHERE id = ?", [item_id]) is not None
            await tx.execute(f"DELETE FROM {PLAN} WHERE id = ?", [item_id])
            return existed

        return await _journaled(
            write, lambda existed: "true" if existed else "false", lambda stored: stored == "true", writes=True
        )


def plan_store() -> KeyPlanStore:
    """Return a store for ``Planning`` whose plan is kept in the key's database, so that it lasts across turns::

        desk = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store())])

    ``clear`` empties it with the conversation. A tool call that runs again
    (after a crash) reads the plan as it read it before, and each of its
    writes is done once.
    """
    return KeyPlanStore()


class KeyOverflowStore:
    """``ToolOutputLimits``' ``OverflowStore`` over the key's database. See ``overflow_store``."""

    async def write(self, key: str, data: bytes) -> str:
        # The harness's key is ``<run id>/<call id>.<retry><suffix>``: the run's id is the attempt's, so the handle
        # leaves it out, and a call that runs again (after a crash) writes over its own spill.
        handle = key.split("/", 1)[1] if "/" in key else key
        ctx = running()
        await SCHEMA.ensure(ctx.db)
        await ctx.db.execute(
            f"INSERT OR REPLACE INTO {SPILLS} (handle, data) VALUES (?, ?)",
            [f"{ctx.request.id}:{handle}", base64.b64encode(data).decode()],
        )
        return f"{ctx.request.id}:{handle}"

    async def read(self, handle: str) -> bytes:
        row = await (await _db()).get(f"SELECT data FROM {SPILLS} WHERE handle = ?", [handle])
        if row is None:
            raise FileNotFoundError(f"no spilled tool result {handle!r}")
        return base64.b64decode(str(row["data"]))


def overflow_store() -> KeyOverflowStore:
    """Return a store for ``ToolOutputLimits`` that spills large tool results to the key's database::

        desk = Agent(model, capabilities=[ToolOutputLimits(store=pydantic_ai.overflow_store())])

    The model reads a spilled result with ``read_tool_result``, in the same
    turn or a later one; ``clear`` empties it with the conversation.
    """
    return KeyOverflowStore()


class TurnsHistorySource:
    """``ConversationSearch``'s ``HistorySource`` over the conversation's turns. See ``history_source``."""

    def __init__(self, items: Items) -> None:
        self.items = items

    async def list_runs(self) -> list[RunRecord]:
        ctx = running()
        db = await _turns(ctx)
        definition = definition_of(ctx)
        conversation = conversation_id_of(ctx)
        rows = await db.values(f"SELECT prompt, asked_at FROM {TURNS} ORDER BY n")
        return [
            RunRecord(
                run_id=str(prompt),
                conversation_id=conversation,
                agent_name=definition.name if definition is not None else None,
                started_at=datetime.fromtimestamp(int(typing.cast(int, asked)) / 1000, UTC),
            )
            for prompt, asked in rows
        ]

    async def run_history(self, *, run_id: str) -> list[ModelMessage]:
        db = await _turns(running())
        row = await db.get(f"SELECT n, item FROM {TURNS} WHERE prompt = ?", [run_id])
        if row is None:
            return []
        n = int(typing.cast(int, row["n"]))
        following = await db.get(f"SELECT MIN(n) AS n FROM {TURNS} WHERE n > ?", [n])
        end = following["n"] if following is not None and following["n"] is not None else None
        if end is None:
            steps = await db.values(f"SELECT item FROM {STEPS} WHERE n > ? ORDER BY n", [n])
        else:
            steps = await db.values(f"SELECT item FROM {STEPS} WHERE n > ? AND n < ? ORDER BY n", [n, end])
        stored = [str(row["item"])] + [str(item) for [item] in steps]
        loaded: list[Item] = [load_item(self.items, item) for item in stored]
        return typing.cast(list[ModelMessage], loaded)


async def _turns(ctx: SessionContext) -> Any:
    from restate.agents._conversation import SCHEMA as TURNS_SCHEMA  # pylint: disable=import-outside-toplevel

    await TURNS_SCHEMA.ensure(ctx.db)
    return ctx.db
