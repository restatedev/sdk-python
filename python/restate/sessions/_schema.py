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
"""Migrations for the databases of session objects.

Every key of an object has its own database, built by the same list of
migrations, applied in order. Each key's database is migrated the first time
a handler opens it, and records how many migrations it has applied (in a
record named ``schema``, stored outside its tables, and committed together
with the migrations). A migration added later, such as a new column or index,
therefore runs once on each key's database, the next time that key is used::

    notes = sessions.SessionObject(
        "notes",
        migrations=[
            "CREATE TABLE notes (text TEXT NOT NULL)",
            "ALTER TABLE notes ADD COLUMN at TEXT",
        ],
    )

A migration is a SQL statement, or an async function that takes the
transaction (to move data, for example). Never change or remove a migration
once it has run; add a new one instead.

A view applies any missing migrations in a session of its own, which stores
nothing, before it reads, so a key that was never written reads as empty
tables. Libraries such as ``restate.agents`` keep their tables in a named
``Schema`` of their own and apply it with ``await schema.ensure(ctx.db)``
wherever they use them, in any object.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from restate.sessions._database import Connection, ConnectionTransaction

RECORD = "schema"
"""The name of the record that stores how many migrations of each schema were applied.

It is a single record for all schemas, so that ``reset`` clears all of them,
including those of libraries that this process never loaded.
"""

Migration = str | Callable[[Any], Awaitable[None]]
"""One migration: a SQL statement, or an async function that takes the transaction."""

_ENSURE = "_restate_sessions_ensure"
"""The attribute through which this package's databases apply schemas (see ``Schema.ensure``)."""


def steps_of(steps: Sequence[Migration]) -> tuple[Migration, ...]:
    """Return ``steps`` as a tuple, rejecting a lone string (which is a sequence of characters)."""
    if isinstance(steps, str):
        shown = f"{steps[:40]!r}{'...' if len(steps) > 40 else ''}"
        raise TypeError(f"migrations takes a list of steps, not a string: migrations=[{shown}]")
    return tuple(steps)


class Schema:
    """A named part of a database's schema, built by its own list of migrations.

    Libraries use this for the tables they own, so that their migrations are
    tracked separately from the object's own ``migrations``::

        AUDIT = Schema("mylib.audit", ["CREATE TABLE mylib_audit (at TEXT NOT NULL, what TEXT NOT NULL)"])

    Args:
        name: The schema's name, unique within a database.
        steps: Its migrations, in order.

    Raises:
        TypeError: If ``steps`` is a string rather than a list of migrations.
    """

    def __init__(self, name: str, steps: Sequence[Migration]) -> None:
        self.name: str = name
        self.steps: tuple[Migration, ...] = steps_of(steps)

    async def ensure(self, db: Any) -> None:
        """Apply the migrations of this schema that ``db`` has not applied yet.

        On a handler's ``ctx.db``, the migrations are committed and checked at
        most once per attempt. On a view's ``ctx.db``, they are applied to the
        view's own session and discarded with it.

        Args:
            db: A session object's ``ctx.db``, or a database from ``database``.
        """
        ensure = ensure_of(db)
        await (ensure([self]) if ensure is not None else apply(db, [self]))


Ensure = Callable[[Sequence[Schema]], Awaitable[None]]
"""A function that applies schemas to a database (a handler's ``ctx.db``, a view's, or one from ``as_database``)."""


def ensuring(db: Any, ensure: Ensure) -> Any:
    """Make ``db`` apply schemas with ``ensure``, and return it."""
    object.__setattr__(db, _ENSURE, ensure)
    return db


def ensure_of(db: Any) -> Ensure | None:
    """Return the function ``db`` applies schemas with, or ``None`` if it is not one of this package's databases."""
    return getattr(db, _ENSURE, None)


async def _applied_in(db: Connection | ConnectionTransaction) -> dict[str, int]:
    """Return how many migrations of each schema the database has applied, by schema name."""
    record = await db.record(RECORD)
    return json.loads(record.decode()) if record else {}


def _behind(applied: dict[str, int], schemas: Sequence[Schema]) -> list[Schema]:
    """Return the schemas that have migrations missing from ``applied``."""
    return [schema for schema in schemas if applied.get(schema.name, 0) < len(schema.steps)]


async def apply(db: Connection, schemas: Sequence[Schema], *, reader: bool = False) -> None:
    """Apply the missing migrations of ``schemas`` to ``db`` in one transaction, and record them."""
    if not _behind(await _applied_in(db), schemas):
        return
    # Check again inside the transaction, which holds the session: another call
    # may have applied them in the meantime.
    async with db.transaction() as tx:
        if not reader:
            await apply_in(tx, schemas)
            return
        # A reader's session is query-only, except while it applies schemas.
        await tx.execute("PRAGMA query_only = 0")
        try:
            await apply_in(tx, schemas)
        finally:
            await tx.execute("PRAGMA query_only = 1")


async def apply_in(tx: ConnectionTransaction, schemas: Sequence[Schema]) -> None:
    """Apply the missing migrations of ``schemas`` inside the transaction ``tx``, and record them."""
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
    """Empty the database inside the transaction ``tx``, then apply ``schemas`` again.

    Drops every table, view, index and trigger and forgets every schema; other
    records, such as the outbox, are kept. The change commits with ``tx``, so
    whatever ``tx`` writes afterwards becomes the new database's first rows.
    """
    made = await tx.values("SELECT type, name FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'")
    # Triggers and views may refer to tables, so drop them first; indexes go with their tables.
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
    """Order ``tables`` so that each comes after every table whose foreign keys refer to it.

    With foreign keys enforced, dropping a parent table deletes its rows, which
    its children's rows may not outlive. Tables in a cycle keep their order.
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
