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
"""
The schema of a session object's databases: each key's database is made by
the same steps, in order, applied once per key, as its first exclusive attempt
opens it. A database keeps how far each schema got in its record ``schema``
(beside its tables, in none of them), committed with the steps, so a step
added later (a column, an index) runs on each key's database once, as that key
is next opened::

    notes = sessions.SessionObject("notes", schema=[
        "CREATE TABLE notes (text TEXT NOT NULL)",
        "ALTER TABLE notes ADD COLUMN at TEXT",
    ])

A step is a statement, or an async function of the transaction (to move data,
say). Steps are never changed or removed once they ran: a change is a step of
its own.

A view applies the steps its key's database lacks in its own session, which
stores nothing, then reads: a key never written reads as empty tables.
Libraries (``restate.agents``) keep their tables in a schema of their own
name, which ``schema.ensure(ctx.db)`` applies where they use them, in any
object.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from restate.sessions._database import Connection, ConnectionTransaction

RECORD = "schema"
"""
The database's record of its schemas: how many steps of each it has, by name.
One record, so that ``reset`` clears all of them, a library's this process
never loaded too.
"""

Step = str | Callable[[Any], Awaitable[None]]
"""A statement, or an async function of the transaction."""

_ENSURE = "_restate_sessions_ensure"
"""The attribute of a database of this package's: how it applies schemas (see ``Schema.ensure``)."""


class Schema:
    """A part of a database's schema: its ``name``, and the ``steps`` that make it, in order."""

    def __init__(self, name: str, steps: Sequence[Step]) -> None:
        self.name = name
        self.steps: tuple[Step, ...] = tuple(steps)

    async def ensure(self, db: Any) -> None:
        """
        Apply the steps ``db`` lacks: a library's tables, where it uses them, in
        any object. On a handler's ``ctx.db``, once per attempt; a view's are
        its own, vanishing with its session.
        """
        ensure = ensure_of(db)
        await (ensure([self]) if ensure is not None else apply(db, [self]))


Ensure = Callable[[Sequence[Schema]], Awaitable[None]]
"""How a database applies schemas: a handler's ``ctx.db``, its reader, or one of ``as_database``."""


def ensuring(db: Any, ensure: Ensure) -> Any:
    """``db``, which applies schemas with ``ensure`` (see ``Schema.ensure``)."""
    object.__setattr__(db, _ENSURE, ensure)
    return db


def ensure_of(db: Any) -> Ensure | None:
    """How ``db`` applies schemas, if it is a database of this package's."""
    return getattr(db, _ENSURE, None)


async def _applied_in(db: Connection | ConnectionTransaction) -> dict[str, int]:
    """How many steps of each schema the database has, by name."""
    record = await db.record(RECORD)
    return json.loads(record.decode()) if record else {}


def _behind(applied: dict[str, int], schemas: Sequence[Schema]) -> list[Schema]:
    """The schemas of ``schemas`` that ``applied`` lacks steps of."""
    return [schema for schema in schemas if applied.get(schema.name, 0) < len(schema.steps)]


async def apply(db: Connection, schemas: Sequence[Schema], *, reader: bool = False) -> None:
    """Apply the steps of ``schemas`` that ``db`` lacks, in one transaction, with how far each got."""
    if not _behind(await _applied_in(db), schemas):
        return
    # Again in the transaction, which holds the session: a call before it may
    # have applied them.
    async with db.transaction() as tx:
        if not reader:
            await apply_in(tx, schemas)
            return
        # A reader's session is query-only, but for its schemas.
        await tx.execute("PRAGMA query_only = 0")
        try:
            await apply_in(tx, schemas)
        finally:
            await tx.execute("PRAGMA query_only = 1")


async def apply_in(tx: ConnectionTransaction, schemas: Sequence[Schema]) -> None:
    """Apply the steps of ``schemas`` that the transaction ``tx`` lacks, and record how far each got."""
    applied = await _applied_in(tx)
    lacking = _behind(applied, schemas)
    if not lacking:
        return
    for schema in lacking:
        for step in schema.steps[applied.get(schema.name, 0) :]:
            await (tx.execute(step) if isinstance(step, str) else step(tx))
        applied[schema.name] = len(schema.steps)
    tx.set_record(RECORD, json.dumps(applied).encode())


async def drop_all(tx: ConnectionTransaction, schemas: Sequence[Schema]) -> None:
    """
    Empty the database in the transaction ``tx``: drop every table, view, index
    and trigger, and forget every schema, then apply ``schemas`` again. Its
    other records (the outbox) stay. It commits with ``tx``: what ``tx`` writes
    after it are the new database's first rows.
    """
    made = await tx.values("SELECT type, name FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'")
    # A trigger or view may name a table; an index goes with its table.
    for kind in ("trigger", "view"):
        for made_kind, name in made:
            if made_kind == kind:
                await _drop(tx, kind, typing.cast(str, name))
    tables = [typing.cast(str, name) for kind, name in made if kind == "table"]
    for name in await _children_first(tx, tables):
        await _drop(tx, "table", name)
    tx.set_record(RECORD, b"")
    await apply_in(tx, schemas)


async def _drop(tx: ConnectionTransaction, kind: str, name: str) -> None:
    quoted = name.replace('"', '""')
    await tx.execute(f'DROP {kind.upper()} IF EXISTS "{quoted}"')


async def _children_first(tx: ConnectionTransaction, tables: list[str]) -> list[str]:
    """
    ``tables``, each after every table whose foreign keys name it: with foreign
    keys on, dropping a table deletes its rows, which a child's rows may not
    outlive. Tables in a cycle stay in their order.
    """
    parents: dict[str, set[str]] = {}
    for name in tables:
        keys = await tx.values('SELECT "table" FROM pragma_foreign_key_list(?)', [name])
        # SQL names are case-insensitive.
        own = name.lower()
        parents[name] = {typing.cast(str, table).lower() for (table,) in keys} - {own}
    ordered: list[str] = []
    left: list[str] = tables
    while left:
        named = {parent for table in left for parent in parents[table]}
        unnamed = [table for table in left if table.lower() not in named]
        step = unnamed or left
        ordered.extend(step)
        left = [table for table in left if table not in step]
    return ordered
