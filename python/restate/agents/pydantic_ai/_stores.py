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
tool call runs again after a crash, and the harness's keys are made of the
run's id, which every attempt has its own of: so a write is done once per
tool call (by the invocation's id and the call's), however often it runs.
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import typing
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai_harness.planning import PlanItem, TaskStatus
from pydantic_ai_harness.step_persistence import RunRecord

from restate.agents._conversation import STEPS, TURNS, Item, Items
from restate.agents._tools import current_call
from restate.sessions import SessionContext
from restate.sessions.internal import Schema, definition_of

PLAN = "restate_pydantic_ai_plan"
"""``Planning``'s plan: its steps, in order."""
SPILLS = "restate_pydantic_ai_spills"
"""``ToolOutputLimits``' spilled tool results, by handle."""
WRITES = "restate_pydantic_ai_writes"
"""The writes of the stores that tool calls made, by the call and their order in it: each is done once."""

SCHEMA = Schema(
    "restate_agents.pydantic_ai",
    [
        f"CREATE TABLE {PLAN} (position INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, item TEXT NOT NULL)",
        f"CREATE TABLE {SPILLS} (handle TEXT PRIMARY KEY, data TEXT NOT NULL)",
        f"CREATE TABLE {WRITES} (call TEXT NOT NULL, n INTEGER NOT NULL, result TEXT, PRIMARY KEY (call, n))",
    ],
)


@dataclasses.dataclass
class _Call:
    """The tool call running, and how many writes of the stores it made so far."""

    id: str
    writes: int = 0


CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar("restate_pydantic_ai_call", default=None)


def calling(call_id: str | None) -> None:
    """Mark tool call ``call_id`` as running (``None``: none is): ``_Restate`` does, as each starts and ends."""
    CALL.set(_Call(call_id) if call_id is not None else None)


def running() -> SessionContext:
    """Return the handler's context of the run in progress (``tools_in``)."""
    return current_call("").ctx


async def _db() -> Any:
    ctx = running()
    await SCHEMA.ensure(ctx.db)
    return ctx.db


T = typing.TypeVar("T")


async def _once(write: Callable[[Any], Awaitable[T]], dump: Callable[[T], str], load: Callable[[str], T]) -> T:
    """Do ``write`` (with a transaction) once for the tool call running, and return its result.

    The call's writes are told apart by their order in it, as a call that
    runs again (after a crash) writes in the same order; the result of one
    done already is read back. Outside a tool call, ``write`` is done as it
    comes.
    """
    ctx = running()
    await SCHEMA.ensure(ctx.db)
    call = CALL.get()
    if call is None:
        async with ctx.db.transaction() as tx:
            return await write(tx)
    n, call.writes = call.writes, call.writes + 1
    key = f"{ctx.request.id}:{call.id}"
    done = await ctx.db.get(f"SELECT result FROM {WRITES} WHERE call = ? AND n = ?", [key, n])
    if done is not None:
        return load(str(done["result"]))
    async with ctx.db.transaction() as tx:
        result = await write(tx)
        await tx.execute(f"INSERT INTO {WRITES} (call, n, result) VALUES (?, ?, ?)", [key, n, dump(result)])
    return result


def _item(stored: object) -> PlanItem:
    return PlanItem.model_validate_json(str(stored))


class KeyPlanStore:
    """``Planning``'s ``PlanStore`` over the key's database: the plan lasts across turns. See ``plan_store``."""

    async def _items(self, db: Any) -> list[PlanItem]:
        return [_item(item) for [item] in await db.values(f"SELECT item FROM {PLAN} ORDER BY position")]

    async def get_items(self) -> list[PlanItem]:
        return await self._items(await _db())

    async def get_item(self, item_id: str) -> PlanItem | None:
        row = await (await _db()).get(f"SELECT item FROM {PLAN} WHERE id = ?", [item_id])
        return _item(row["item"]) if row is not None else None

    async def set_items(self, items: list[PlanItem]) -> None:
        async def write(tx: Any) -> None:
            await tx.execute(f"DELETE FROM {PLAN}")
            for position, item in enumerate(items):
                await tx.execute(
                    f"INSERT INTO {PLAN} (position, id, item) VALUES (?, ?, ?)",
                    [position, item.id, item.model_dump_json()],
                )

        await _once(write, lambda _: "null", lambda _: None)

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

        return await _once(write, PlanItem.model_dump_json, PlanItem.model_validate_json)

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

        def dump(item: PlanItem | None) -> str:
            return item.model_dump_json() if item is not None else "null"

        def load(stored: str) -> PlanItem | None:
            return None if stored == "null" else PlanItem.model_validate_json(stored)

        return await _once(write, dump, load)

    async def remove_item(self, item_id: str) -> bool:
        async def write(tx: Any) -> bool:
            existed = await tx.get(f"SELECT 1 FROM {PLAN} WHERE id = ?", [item_id]) is not None
            await tx.execute(f"DELETE FROM {PLAN} WHERE id = ?", [item_id])
            return existed

        return await _once(write, lambda existed: "true" if existed else "false", lambda stored: stored == "true")


def plan_store() -> KeyPlanStore:
    """Return a store for ``Planning`` whose plan is kept in the key's database, so that it lasts across turns::

        desk = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store())])

    ``clear`` empties it with the conversation. Each write of a tool call is
    done once, however often the call runs.
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
        conversation = f"{definition.name}/{ctx.key}" if definition is not None else ctx.key
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
        loaded: list[Item] = [self.items.load(str(row["item"]))] + [self.items.load(str(item)) for [item] in steps]
        return typing.cast(list[ModelMessage], loaded)


async def _turns(ctx: SessionContext) -> Any:
    from restate.agents._conversation import SCHEMA as TURNS_SCHEMA  # pylint: disable=import-outside-toplevel

    await TURNS_SCHEMA.ensure(ctx.db)
    return ctx.db
