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
"""A LangGraph checkpointer that stores checkpoints in a session object's database.

``SqlCheckpointSaver`` works on any database session; in a handler, that is
``ctx.db``. It uses three tables, and stores values as LangGraph's serializer
writes them:

- ``restate_langgraph_checkpoints`` holds each checkpoint, without its
  channel values.
- ``restate_langgraph_channel_values`` holds each channel's value at each
  version, written when a step changes it. A checkpoint's values are the ones
  at its channel versions, so a step stores only what it changed rather than
  the whole state (an agent's message list, for example). Versions are unique,
  made of a counter and a random part: a thread forked from an earlier
  checkpoint reuses the same counter values, and its values must not
  overwrite those of the branch it forked from.
- ``restate_langgraph_writes`` holds the pending writes of each checkpoint's
  tasks.

Each ``aput`` and ``aput_writes`` runs in one SQL transaction, so a step is
stored whole, in order with everything else the handler does. Operations run
one at a time, in the order they are called, even when LangGraph writes the
results of parallel tasks at once. The saver is async-only, because ``ctx.db``
is.

By default a thread keeps every checkpoint, which LangGraph needs for
``alist``, time travel and forks. With ``history="latest"``, each ``aput``
deletes the thread's earlier checkpoints, their writes, and the channel values
that only they referenced. A thread then stores its state once instead of once
per step; otherwise a message list stored again at each step grows with the
square of its length. A subgraph's run is stored in a namespace of its own,
named after its task: each ``aput`` also deletes the subgraph runs that the
graph's earlier steps started, which nothing reads again.
"""

from __future__ import annotations

import asyncio
import typing
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.types import INTERRUPT, RESUME

from restate.sessions import Database, DatabaseWriter, SqlParam, SqlValue
from restate.sessions.internal import Schema

SCHEMA = Schema(
    "restate_agents.langgraph",
    [
        """CREATE TABLE restate_langgraph_checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT NOT NULL,
            checkpoint BLOB NOT NULL,
            metadata BLOB NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )""",
        """CREATE TABLE restate_langgraph_channel_values (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL,
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            value BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )""",
        """CREATE TABLE restate_langgraph_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_path TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT NOT NULL,
            value BLOB NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )""",
    ],
)
"""The checkpointer's tables, a schema of their own that is applied on first use."""

_TABLES = ("restate_langgraph_checkpoints", "restate_langgraph_channel_values", "restate_langgraph_writes")

_COLUMNS = "thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata"

_EMPTY = "empty"
"""The type of a channel a step emptied."""

_TASK = ":"
"""What separates a node from its task id in a checkpoint namespace (``node:task_id``), as LangGraph writes it."""


class SqlCheckpointSaver(BaseCheckpointSaver[str]):
    """A LangGraph checkpoint saver that stores checkpoints in a database session.

    Pass it to ``compile`` to keep a graph of your own in ``ctx.db``::

        app = graph.compile(checkpointer=SqlCheckpointSaver(ctx.db))
        await app.ainvoke(inputs, {"configurable": {"thread_id": ctx.key}})

    See the module docstring for how checkpoints are stored.

    Args:
        db: The database to store checkpoints in, usually ``ctx.db``.
        history: Which checkpoints a thread keeps: ``"all"`` (the default),
            or only the ``"latest"``.
        serde: LangGraph's serializer for checkpoints and values; LangGraph's
            default if omitted.
    """

    def __init__(
        self,
        db: Database,
        *,
        history: Literal["all", "latest"] = "all",
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self.db = db
        self.latest = history == "latest"
        self._lock = asyncio.Lock()
        self._ready = False

    def get_next_version(self, current: str | None, channel: None = None) -> str:
        """Return the channel version that follows ``current``.

        A version is the counter of ``current`` plus one, zero-padded so that
        versions sort in counter order, followed by a random part so that two
        branches of a thread never share a version.
        """
        if current is None:
            count = 0
        elif isinstance(current, int):  # pyright: ignore[reportUnnecessaryIsInstance]  # LangGraph's own start at 1
            count = current
        else:
            count = int(current.split(".")[0])
        return f"{count + 1:032}.{uuid.uuid4().hex}"

    async def _setup(self) -> None:
        if not self._ready:
            await SCHEMA.ensure(self.db)
            self._ready = True

    def _load(self, type_: SqlValue, value: SqlValue) -> Any:
        return self.serde.loads_typed((str(type_), _bytes(value)))

    async def _channel_values(self, thread_id: str, ns: str, checkpoint: Checkpoint) -> dict[str, Any]:
        """Return the values of ``checkpoint``'s channels at its channel versions."""
        versions = list(checkpoint.get("channel_versions", {}).items())
        if not versions:
            return {}
        rows = await self.db.values(
            "SELECT channel, type, value FROM restate_langgraph_channel_values "
            "WHERE thread_id = ? AND checkpoint_ns = ? AND ("
            + " OR ".join("(channel = ? AND version = ?)" for _ in versions)
            + ")",
            [thread_id, ns, *(item for channel, version in versions for item in (channel, str(version)))],
        )
        return {str(channel): self._load(type_, value) for channel, type_, value in rows if type_ != _EMPTY}

    async def _tuple(self, row: list[SqlValue]) -> CheckpointTuple:
        thread_id, ns, checkpoint_id, parent_id, type_, stored, metadata = (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            row[3],
            row[4],
            row[5],
            row[6],
        )
        checkpoint = typing.cast(Checkpoint, self._load(type_, stored))
        checkpoint = typing.cast(
            Checkpoint, {**checkpoint, "channel_values": await self._channel_values(thread_id, ns, checkpoint)}
        )
        writes = await self.db.values(
            "SELECT task_id, channel, type, value FROM restate_langgraph_writes "
            "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? ORDER BY task_path, task_id, idx",
            [thread_id, ns, checkpoint_id],
        )

        def config(id_: str) -> RunnableConfig:
            return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": id_}}

        return CheckpointTuple(
            config=config(checkpoint_id),
            checkpoint=checkpoint,
            metadata=typing.cast(CheckpointMetadata, self._load(type_, metadata)),
            parent_config=None if parent_id is None else config(str(parent_id)),
            pending_writes=[
                (str(task_id), str(channel), self._load(write_type, value))
                for task_id, channel, write_type, value in writes
            ],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        async with self._lock:
            await self._setup()
            thread_id, ns = _thread(config)
            checkpoint_id = get_checkpoint_id(config)
            if checkpoint_id:
                rows = await self.db.values(
                    f"SELECT {_COLUMNS} FROM restate_langgraph_checkpoints "
                    "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                    [thread_id, ns, checkpoint_id],
                )
            else:
                rows = await self.db.values(
                    f"SELECT {_COLUMNS} FROM restate_langgraph_checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    [thread_id, ns],
                )
            return await self._tuple(rows[0]) if rows else None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # LangGraph's name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async with self._lock:
            await self._setup()
            configurable: dict[str, Any] = dict((config or {}).get("configurable", {}))
            conditions: list[tuple[str, Any]] = [
                ("thread_id = ?", configurable.get("thread_id")),
                ("checkpoint_ns = ?", configurable.get("checkpoint_ns")),
                ("checkpoint_id = ?", configurable.get("checkpoint_id")),
                ("checkpoint_id < ?", get_checkpoint_id(before) if before else None),
            ]
            where = [condition for condition, value in conditions if value is not None]
            params: list[SqlParam] = [value for _, value in conditions if value is not None]
            rows = await self.db.values(
                f"SELECT {_COLUMNS} FROM restate_langgraph_checkpoints "
                f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY checkpoint_id DESC",
                params,
            )
            found: list[CheckpointTuple] = []
            for row in rows:
                if limit is not None and len(found) >= limit:
                    break
                checkpoint = await self._tuple(row)
                if all(_matches(checkpoint.metadata.get(key), value) for key, value in (filter or {}).items()):
                    found.append(checkpoint)
        for checkpoint in found:
            yield checkpoint

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with self._lock:
            await self._setup()
            thread_id, ns = _thread(config)
            parent_id: str | None = _configurable(config).get("checkpoint_id")
            stored: dict[str, Any] = dict(checkpoint)
            values: dict[str, Any] = stored.pop("channel_values")
            type_, serialized = self.serde.dumps_typed(stored)
            metadata_type, metadata_value = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
            if type_ != metadata_type:
                raise ValueError("Failed to serialize checkpoint and metadata to the same type.")
            # The channels this step changed, at their new versions.
            changed: list[list[SqlParam]] = [
                [
                    channel,
                    str(version),
                    *(self.serde.dumps_typed(values[channel]) if channel in values else (_EMPTY, None)),
                ]
                for channel, version in new_versions.items()
            ]
            async with self.db.transaction() as tx:
                for row in changed:
                    await tx.execute(
                        "INSERT OR REPLACE INTO restate_langgraph_channel_values "
                        "(thread_id, checkpoint_ns, channel, version, type, value) VALUES (?, ?, ?, ?, ?, ?)",
                        [thread_id, ns, *row],
                    )
                await tx.execute(
                    f"INSERT OR REPLACE INTO restate_langgraph_checkpoints ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [thread_id, ns, checkpoint["id"], parent_id, type_, serialized, metadata_value],
                )
                if self.latest:
                    await _drop_earlier(tx, thread_id, ns, checkpoint)
                    await self._drop_done_runs(tx, thread_id, ns, checkpoint["id"])
            return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    async def _drop_done_runs(self, tx: DatabaseWriter, thread_id: str, ns: str, checkpoint_id: str) -> None:
        """Delete the subgraph runs that the steps of graph ``ns`` before its checkpoint ``checkpoint_id`` started.

        LangGraph stores each run of a subgraph in a namespace of its own, below
        the namespace of the graph whose step ran it and named after the step's
        task (``node:task_id``, ``outer|node:task_id``). Each of the run's
        checkpoints names the checkpoint of every graph above it that it ran
        from (``parents``, by namespace). A step before ``checkpoint_id`` is done
        (or its tasks were dropped for new input), so nothing reads its runs
        again, nor what ran below them.

        Kept are the runs of the steps from ``checkpoint_id`` on: with
        ``durability="async"`` this checkpoint may be stored after a later step
        has started its runs, and with ``"exit"`` a paused run names the very
        checkpoint being stored. Checkpoint ids sort in time order. Kept too are
        the runs below a subgraph that keeps its own history
        (``checkpointer=True``, a namespace without a task id): its own
        checkpoints decide them, as it may resume a paused step after the graph
        above it moved on.
        """
        below = f"{ns}|" if ns else ""
        rows = await tx.values(
            "SELECT checkpoint_ns, type, metadata FROM restate_langgraph_checkpoints "
            "WHERE thread_id = ? AND checkpoint_ns <> ? AND substr(checkpoint_ns, 1, ?) = ?",
            [thread_id, ns, len(below), below],
        )
        done: set[str] = set()
        for found, type_, metadata in rows:
            run = str(found)[len(below) :].split("|")[0]
            started = _parents(self._load(type_, metadata)).get(ns)
            if _TASK in run and started is not None and str(started) < checkpoint_id:
                done.add(str(found))
        for found in sorted(done):
            for table in _TABLES:
                await tx.execute(f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_ns = ?", [thread_id, found])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with self._lock:
            await self._setup()
            thread_id, ns = _thread(config)
            checkpoint_id = str(_configurable(config)["checkpoint_id"])
            # Special writes (errors, interrupts) replace; others keep the first.
            verb = (
                "INSERT OR REPLACE" if all(channel in WRITES_IDX_MAP for channel, _ in writes) else "INSERT OR IGNORE"
            )
            rows: list[list[SqlParam]] = [
                [
                    thread_id,
                    ns,
                    checkpoint_id,
                    task_id,
                    task_path,
                    WRITES_IDX_MAP.get(channel, idx),
                    channel,
                    *self.serde.dumps_typed(value),
                ]
                for idx, (channel, value) in enumerate(writes)
            ]
            async with self.db.transaction() as tx:
                for row in rows:
                    await tx.execute(
                        f"{verb} INTO restate_langgraph_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, "
                        "task_path, idx, channel, type, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        row,
                    )

    async def _resumes(self, thread_id: str) -> dict[str, int]:
        """Return, by interrupt id, how many resume values the task that raised each interrupt has.

        A task that raises ``interrupt()`` again after it was resumed raises
        one of the same id; the count tells the times apart. A task's
        interrupt and its resume values are pending writes of the same
        checkpoint, in the namespace of the graph that ran it: the thread's
        own, or a subgraph's (``node:task_id``, or ``node:task_id|1`` for a
        node's second subgraph call, which LangGraph's subgraph snapshots
        do not show). This reads them in every namespace. Each graph above
        the raiser lists the interrupt again, on its own task with that
        task's own resume values: the raiser's is the deepest namespace.
        """
        async with self._lock:
            await self._setup()
            rows = await self.db.values(
                "SELECT checkpoint_ns, checkpoint_id, task_id, channel, type, value FROM restate_langgraph_writes "
                "WHERE thread_id = ? AND channel IN (?, ?)",
                [thread_id, INTERRUPT, RESUME],
            )
        raised: dict[tuple[str, str, str], list[str]] = {}
        resumed: dict[tuple[str, str, str], int] = {}
        for ns, checkpoint_id, task_id, channel, type_, value in rows:
            task = (str(ns), str(checkpoint_id), str(task_id))
            loaded = self._load(type_, value)
            if channel == INTERRUPT:
                interrupts = typing.cast(Sequence[Any], loaded) if isinstance(loaded, list | tuple) else ()
                raised[task] = [str(i.id) for i in interrupts if getattr(i, "id", None) is not None]
            else:
                resumed[task] = len(typing.cast(list[Any], loaded)) if isinstance(loaded, list) else 1
        # Each graph above the task that raised an interrupt lists it again, on its own task, with that task's own
        # resume values (a node that interrupts itself, then runs a subgraph that interrupts): the count is the
        # raiser's, in the deepest namespace that lists the interrupt.
        counts: dict[str, tuple[int, int]] = {}
        for task, ids in raised.items():
            found = (len(task[0]), resumed.get(task, 0))
            for interrupt_id in ids:
                counts[interrupt_id] = max(counts.get(interrupt_id, (-1, 0)), found)
        return {interrupt_id: count for interrupt_id, (_, count) in counts.items()}

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._lock:
            await self._setup()
            async with self.db.transaction() as tx:
                for table in _TABLES:
                    await tx.execute(f"DELETE FROM {table} WHERE thread_id = ?", [thread_id])


def _thread(config: RunnableConfig) -> tuple[str, str]:
    """Return the thread id and checkpoint namespace of ``config``."""
    configurable = _configurable(config)
    return str(configurable["thread_id"]), str(configurable.get("checkpoint_ns", ""))


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable", {}))


def _bytes(value: SqlValue) -> bytes:
    return value if isinstance(value, bytes) else b"" if value is None else str(value).encode()


def _parents(metadata: object) -> dict[str, Any]:
    """Return the checkpoints that a subgraph's checkpoint ran from, of each graph above it by namespace."""
    parents = typing.cast(dict[str, Any], metadata).get("parents") if isinstance(metadata, dict) else None
    return typing.cast(dict[str, Any], parents) if isinstance(parents, dict) else {}


def _matches(actual: object, expected: object) -> bool:
    """Return whether ``actual`` has every field of ``expected``, recursively, for metadata filters."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        fields = typing.cast(dict[Any, Any], actual)
        return all(_matches(fields.get(key), value) for key, value in typing.cast(dict[Any, Any], expected).items())
    return actual == expected


async def _drop_earlier(tx: DatabaseWriter, thread_id: str, ns: str, checkpoint: Checkpoint) -> None:
    """Delete the thread's checkpoints, writes and channel values that ``checkpoint`` does not use."""
    for table in ("restate_langgraph_checkpoints", "restate_langgraph_writes"):
        await tx.execute(
            f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id <> ?",
            [thread_id, ns, checkpoint["id"]],
        )
    versions = list(checkpoint.get("channel_versions", {}).items())
    kept = " OR ".join("(channel = ? AND version = ?)" for _ in versions) or "0"
    await tx.execute(
        f"DELETE FROM restate_langgraph_channel_values WHERE thread_id = ? AND checkpoint_ns = ? AND NOT ({kept})",
        [thread_id, ns, *(item for channel, version in versions for item in (channel, str(version)))],
    )
