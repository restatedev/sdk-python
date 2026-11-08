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
"""Pydantic AI Harness's ``Memory`` on a session object: memory that outlives a conversation.

``memory`` makes the session object, one key per memory namespace (a user,
say), whose exclusive handlers make each compare-and-set atomic; its
database holds the files, their versions, and the receipts of the
operations. ``memory_store`` gives ``Memory`` a store of one of its keys,
which a Pydantic AI agent's run calls: each write of a tool call has the
idempotency key of the call and its order in it, so a call that runs again
(after a crash) is applied once. The memory is not the conversation's:
``clear`` leaves it.
"""

from __future__ import annotations

import typing
import weakref
from collections.abc import Callable
from typing import Any

from pydantic_ai_harness.memory import (
    MemoryConflictError,
    MemoryFile,
    MemoryMutation,
    MemoryOperation,
    MemoryOperationConflictError,
)

from restate import sessions
from restate.agents.pydantic_ai._stores import CALL, running
from restate.sessions import SessionContext
from restate.sessions.internal import Schema

FILES = "restate_pydantic_ai_memory"
"""The memory's files: their content, version, and the operation that wrote them."""
RECEIPTS = "restate_pydantic_ai_memory_receipts"
"""The results of the operations that changed the memory, by the operation's id."""
GENERATION = "restate_pydantic_ai_memory_generation"
"""The memory's generation: each change takes the next, which is the version it gives a file."""

SCHEMA = Schema(
    "restate_agents.pydantic_ai.memory",
    [
        f"""CREATE TABLE {FILES} (
            path TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            version TEXT NOT NULL,
            operation TEXT
        )""",
        f"""CREATE TABLE {RECEIPTS} (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            version TEXT,
            existed INTEGER NOT NULL
        )""",
        f"CREATE TABLE {GENERATION} (row INTEGER PRIMARY KEY CHECK (row = 0), n INTEGER NOT NULL)",
    ],
)

_HANDLERS: weakref.WeakKeyDictionary[sessions.SessionObject, dict[str, Any]] = weakref.WeakKeyDictionary()
"""The handlers of each object that ``memory`` made."""

Mutation = dict[str, Any]
"""A ``MemoryMutation`` as JSON, or ``{"conflict": ...}`` (``"version"`` or ``"operation"``) and why."""


async def _receipt(db: Any, operation: dict[str, str] | None) -> Mutation | None:
    if operation is None:
        return None
    row = await db.get(f"SELECT fingerprint, version, existed FROM {RECEIPTS} WHERE id = ?", [operation["id"]])
    if row is None:
        return None
    if row["fingerprint"] != operation["fingerprint"]:
        return {
            "conflict": "operation",
            "message": f"operation id {operation['id']!r} was reused with different arguments",
        }
    return {"version": row["version"], "replayed": True, "existed": bool(row["existed"])}


async def _change(
    ctx: SessionContext, path: str, content: str | None, expected: str | None, operation: dict[str, str] | None
) -> Mutation:
    """Write ``content`` to ``path`` (or delete it, for ``None``) if its version is ``expected``, in one transaction."""
    await SCHEMA.ensure(ctx.db)
    async with ctx.db.transaction() as tx:
        if (receipt := await _receipt(tx, operation)) is not None:
            return receipt
        current = await tx.get(f"SELECT version FROM {FILES} WHERE path = ?", [path])
        version = str(current["version"]) if current is not None else None
        if version != expected:
            action = "written" if content is not None else "deleted"
            return {"conflict": "version", "message": f"memory path {path!r} changed before it could be {action}"}
        row = await tx.get(f"SELECT n FROM {GENERATION} WHERE row = 0")
        n = (int(typing.cast(int, row["n"])) if row is not None else 0) + 1
        await tx.execute(f"INSERT OR REPLACE INTO {GENERATION} (row, n) VALUES (0, ?)", [n])
        if content is None:
            await tx.execute(f"DELETE FROM {FILES} WHERE path = ?", [path])
            written = None
        else:
            written = str(n)
            await tx.execute(
                f"INSERT OR REPLACE INTO {FILES} (path, content, version, operation) VALUES (?, ?, ?, ?)",
                [path, content, written, operation["id"] if operation is not None else None],
            )
        mutation: Mutation = {"version": written, "replayed": False, "existed": current is not None}
        if operation is not None:
            await tx.execute(
                f"INSERT INTO {RECEIPTS} (id, fingerprint, version, existed) VALUES (?, ?, ?, ?)",
                [operation["id"], operation["fingerprint"], written, int(current is not None)],
            )
        return mutation


def memory(name: str = "memory") -> sessions.SessionObject:
    """Create the session object that keeps Pydantic AI Harness's ``Memory``, one key per memory namespace.

    Serve it beside the agents that use it, whose ``Memory`` takes a store of
    one of its keys (``memory_store``)::

        memories = pydantic_ai.memory()
        mine = Agent(model, capabilities=[Memory(store_resolver=lambda run: pydantic_ai.memory_store(memories, user))])
        app = restate.app([pydantic_ai.agent("desk", agent=mine), memories])

    Args:
        name: The object's name, as Restate serves it.

    Returns:
        The session object.
    """
    obj = sessions.SessionObject(name)

    @obj.handler
    async def write(ctx: SessionContext, request: dict[str, Any]) -> Mutation:
        return await _change(
            ctx, request["path"], request["content"], request.get("expected"), request.get("operation")
        )

    @obj.handler
    async def delete(ctx: SessionContext, request: dict[str, Any]) -> Mutation:
        return await _change(ctx, request["path"], None, request.get("expected"), request.get("operation"))

    @obj.handler
    async def read(ctx: SessionContext, path: str) -> dict[str, Any] | None:
        await SCHEMA.ensure(ctx.db)
        row = await ctx.db.get(f"SELECT content, version, operation FROM {FILES} WHERE path = ?", [path])
        if row is None:
            return None
        return {"content": row["content"], "version": row["version"], "operation": row["operation"]}

    @obj.handler
    async def operation(ctx: SessionContext, operation: dict[str, str]) -> Mutation | None:
        await SCHEMA.ensure(ctx.db)
        return await _receipt(ctx.db, operation)

    @obj.handler
    async def paths(ctx: SessionContext, prefix: str) -> list[str]:
        await SCHEMA.ensure(ctx.db)
        rows = await ctx.db.values(f"SELECT path FROM {FILES} ORDER BY path")
        return [str(path) for [path] in rows if str(path).startswith(prefix)]

    _HANDLERS[obj] = {"write": write, "delete": delete, "read": read, "operation": operation, "paths": paths}
    return obj


def _mutation(result: Mutation) -> MemoryMutation:
    if (conflict := result.get("conflict")) is not None:
        if conflict == "operation":
            raise MemoryOperationConflictError(result["message"])
        raise MemoryConflictError(result["message"])
    return MemoryMutation(version=result["version"], replayed=bool(result["replayed"]), existed=bool(result["existed"]))


def _operation(operation: MemoryOperation | None) -> dict[str, str] | None:
    return {"id": operation.id, "fingerprint": operation.fingerprint} if operation is not None else None


class SessionMemoryStore:
    """``Memory``'s ``MemoryStore`` over a key of the object ``memory`` makes. See ``memory_store``."""

    def __init__(self, obj: sessions.SessionObject, key: str | Callable[[SessionContext], str]) -> None:
        handlers = _HANDLERS.get(obj)
        if handlers is None:
            raise TypeError(f"{obj!r} is not a memory: make it with pydantic_ai.memory()")
        self.handlers: dict[str, Any] = handlers
        self.key = key

    def _call(self, name: str, arg: Any, *, once: bool = False) -> Any:
        ctx = running()
        key = self.key if isinstance(self.key, str) else self.key(ctx)
        idempotency_key = None
        call = CALL.get()
        if once and call is not None:
            # The n-th write of the tool call: a call that runs again writes the same, in the same order.
            n, call.writes = call.writes, call.writes + 1
            idempotency_key = f"{ctx.request.id}:{call.id}:memory:{n}"
        untyped: Any = ctx
        return untyped.call(self.handlers[name], arg, key=key, idempotency_key=idempotency_key)

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        found = await self._call("read", path)
        if found is None:
            return None
        content = str(found["content"])
        return MemoryFile(
            content=content[:max_chars],
            version=str(found["version"]),
            operation_id=found["operation"],
            truncated=len(content) > max_chars,
        )

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        result = await self._call("operation", _operation(operation))
        return _mutation(result) if result is not None else None

    async def write(
        self, path: str, content: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        request = {"path": path, "content": content, "expected": expected_version, "operation": _operation(operation)}
        return _mutation(await self._call("write", request, once=True))

    async def delete(
        self, path: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        request = {"path": path, "expected": expected_version, "operation": _operation(operation)}
        return _mutation(await self._call("delete", request, once=True))

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return list(await self._call("paths", prefix))[:limit]


def memory_store(obj: sessions.SessionObject, key: str | Callable[[SessionContext], str]) -> SessionMemoryStore:
    """Return a store for Pydantic AI Harness's ``Memory`` over key ``key`` of ``obj`` (made with ``memory``).

    ``key`` is the memory's namespace (a user, say), or a function of the
    handler's context that returns it; the conversations of every agent
    whose store names it share the memory::

        def user(ctx: SessionContext) -> str:
            return ctx.key.split(".")[0]  # alice.trip, alice.support: alice's

        mine = Agent(model, capabilities=[Memory(store=pydantic_ai.memory_store(memories, user))])

    A write that conflicts with another (its expected version is not the
    file's) is refused, and ``Memory`` reads the file again; a tool call's
    write is applied once, however often the call runs.
    """
    return SessionMemoryStore(obj, key)
