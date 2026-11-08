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

A thread keeps every checkpoint, which LangGraph needs for ``alist``, time
travel and forks, until ``aprune`` (LangGraph's API) deletes them: with
``strategy="keep_latest"``, all but the latest checkpoint of each namespace
(the thread's own, and each subgraph run's), with their writes and the channel
values that only they referenced. A graph with a ``DeltaChannel`` keeps the
checkpoints that LangGraph rebuilds that channel from too. A message list stored again at each step
grows with the square of its length: prune a thread between its runs, as
``langgraph.agent`` does after each turn.
"""

from __future__ import annotations

import asyncio
import typing
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

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


class SqlCheckpointSaver(BaseCheckpointSaver[str]):
    """A LangGraph checkpoint saver that stores checkpoints in a database session.

    Pass it to ``compile`` to keep a graph of your own in ``ctx.db``::

        app = graph.compile(checkpointer=SqlCheckpointSaver(ctx.db))
        await app.ainvoke(inputs, {"configurable": {"thread_id": ctx.key}})

    See the module docstring for how checkpoints are stored.

    Args:
        db: The database to store checkpoints in, usually ``ctx.db``.
        serde: LangGraph's serializer for checkpoints and values; LangGraph's
            default if omitted.
    """

    def __init__(
        self,
        db: Database,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self.db = db
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
            return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

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

    async def adelete_thread(self, thread_id: str) -> None:
        await self.aprune([thread_id], strategy="delete")

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        """Delete checkpoints of ``thread_ids``: all but the latest of each namespace, or (``"delete"``) all.

        With ``"keep_latest"``, the writes and channel values that only the
        deleted checkpoints referenced go with them; the latest checkpoint
        keeps its pending writes, so a paused run still resumes. A namespace
        is the thread's own (``""``) or a subgraph run's, which keeps its
        latest checkpoint too.

        A graph with a ``DeltaChannel`` (LangGraph's beta) stores only the
        writes of such a channel at most steps, and its whole value now and
        then (a snapshot): LangGraph rebuilds the value from the latest
        snapshot and the writes after it. The latest checkpoint of such a
        graph keeps its ancestors back to the snapshot of each of its delta
        channels, with their writes (see ``_kept``).
        """
        if strategy not in ("keep_latest", "delete"):
            raise ValueError(f'unknown strategy {strategy!r}: use "keep_latest" or "delete"')
        async with self._lock:
            await self._setup()
            async with self.db.transaction() as tx:
                for thread_id in thread_ids:
                    if strategy == "delete":
                        for table in _TABLES:
                            await tx.execute(f"DELETE FROM {table} WHERE thread_id = ?", [thread_id])
                        continue
                    latest = await tx.values(
                        "SELECT c.checkpoint_ns, c.checkpoint_id FROM restate_langgraph_checkpoints c "
                        "WHERE c.thread_id = ? AND c.checkpoint_id = ("
                        "SELECT max(l.checkpoint_id) FROM restate_langgraph_checkpoints l "
                        "WHERE l.thread_id = c.thread_id AND l.checkpoint_ns = c.checkpoint_ns)",
                        [thread_id],
                    )
                    for ns, checkpoint_id in latest:
                        kept = await self._kept(tx, thread_id, str(ns), str(checkpoint_id))
                        await _drop_earlier(tx, thread_id, str(ns), kept)

    async def _kept(self, tx: DatabaseWriter, thread_id: str, ns: str, checkpoint_id: str) -> dict[str, Checkpoint]:
        """Return the checkpoints of namespace ``ns`` that its latest, ``checkpoint_id``, needs, by id.

        That is the latest itself, and the ancestors that LangGraph reads to
        rebuild its delta channels (``get_delta_channel_history``): back to
        the snapshot of each. The metadata of a checkpoint counts, for each
        delta channel, the steps since its snapshot
        (``counters_since_delta_snapshot``); a channel that has no count
        there was snapshotted at that checkpoint, or not written yet.
        """
        kept: dict[str, Checkpoint] = {}
        waiting: set[str] | None = None
        at: SqlValue = checkpoint_id
        while at is not None and waiting != set():
            rows = await tx.values(
                "SELECT parent_checkpoint_id, type, checkpoint, metadata FROM restate_langgraph_checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                [thread_id, ns, at],
            )
            if not rows:
                break
            parent, type_, stored, metadata = rows[0]
            kept[str(at)] = typing.cast(Checkpoint, self._load(type_, stored))
            loaded = typing.cast(dict[str, Any], self._load(type_, metadata))
            counted = set(typing.cast(dict[str, Any], loaded.get("counters_since_delta_snapshot") or {}))
            waiting = counted if waiting is None else waiting & counted
            at = parent
        return kept


def _thread(config: RunnableConfig) -> tuple[str, str]:
    """Return the thread id and checkpoint namespace of ``config``."""
    configurable = _configurable(config)
    return str(configurable["thread_id"]), str(configurable.get("checkpoint_ns", ""))


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable", {}))


def _bytes(value: SqlValue) -> bytes:
    return value if isinstance(value, bytes) else b"" if value is None else str(value).encode()


def _matches(actual: object, expected: object) -> bool:
    """Return whether ``actual`` has every field of ``expected``, recursively, for metadata filters."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        fields = typing.cast(dict[Any, Any], actual)
        return all(_matches(fields.get(key), value) for key, value in typing.cast(dict[Any, Any], expected).items())
    return actual == expected


async def _drop_earlier(tx: DatabaseWriter, thread_id: str, ns: str, kept: dict[str, Checkpoint]) -> None:
    """Delete the checkpoints, writes and channel values of namespace ``ns`` that its ``kept`` checkpoints do not
    use."""
    ids = ", ".join("?" for _ in kept)
    for table in ("restate_langgraph_checkpoints", "restate_langgraph_writes"):
        await tx.execute(
            f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id NOT IN ({ids})",
            [thread_id, ns, *kept],
        )
    versions = {(channel, str(version)) for c in kept.values() for channel, version in c["channel_versions"].items()}
    used = " OR ".join("(channel = ? AND version = ?)" for _ in versions) or "0"
    await tx.execute(
        f"DELETE FROM restate_langgraph_channel_values WHERE thread_id = ? AND checkpoint_ns = ? AND NOT ({used})",
        [thread_id, ns, *(item for pair in versions for item in pair)],
    )
