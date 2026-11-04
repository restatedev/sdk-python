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
"""Session objects: Restate virtual objects with a SQL database per key.

Each key's database (``ctx.db``) is kept in the object's state in Restate,
so there is no database to run::

    from restate import sessions

    notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT NOT NULL)"])


    @notes.handler
    async def add(ctx: sessions.SessionContext, text: str) -> None:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])


    @notes.view(name="list")
    async def list_notes(ctx: sessions.ViewContext) -> list[str]:
        return [row["text"] for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]


    if __name__ == "__main__":
        sessions.serve(notes)

Handlers run one at a time per key; views run beside them and read what was
committed. A handler is not replayed from a journal: a retried attempt runs
again from the start, against what earlier attempts committed.

The types a handler gets (``SessionContext``, ``Database``, ``Transaction``,
...) are protocols, and a decorated handler is still the plain function, so a
test can call it with a fake context. Session objects are served beside the
SDK's services, objects and workflows, and call them with ``ctx.call``.
``restate.sessions.internal`` has what libraries built on session objects need.
"""

from restate.sessions._api import (
    Caller,
    Database,
    DatabaseReader,
    DatabaseWriter,
    Sender,
    SessionContext,
    Transaction,
    ViewContext,
)
from restate.sessions._calls import CallFuture
from restate.sessions._database import Row, Rows, SqlError, SqlParam, SqlValue
from restate.sessions._object import SessionObject
from restate.sessions._schema import Migration
from restate.sessions._serve import app, serve

__all__ = [
    "CallFuture",
    "Caller",
    "Database",
    "DatabaseReader",
    "DatabaseWriter",
    "Migration",
    "Row",
    "Rows",
    "Sender",
    "SessionContext",
    "SessionObject",
    "SqlError",
    "SqlParam",
    "SqlValue",
    "Transaction",
    "ViewContext",
    "app",
    "serve",
]
