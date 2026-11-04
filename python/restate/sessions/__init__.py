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
Session objects: virtual objects with a SQL database per key, kept in the
object's state (``ctx.db``). The Python variant of ``@restatedev/sessions``::

    import restate
    from restate import sessions

    notes = sessions.SessionObject("notes", schema=["CREATE TABLE notes (text TEXT NOT NULL)"])


    @notes.handler()
    async def add(ctx: sessions.SessionContext, text: str) -> None:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])


    @notes.view()
    async def list(ctx: sessions.ViewContext) -> list[str]:
        return [row["text"] for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]


    app = restate.app([notes])

Session handlers don't replay a journal: a retried attempt runs again from its
first line, against what the attempts before it committed. A session object
serves beside the SDK's services, objects and workflows, which it calls with
``ctx.rpc`` for journaled work.
"""

from restate.sessions._calls import CallFuture, Rpc, Send
from restate.sessions._database import Row, Rows, SqlError, SqlParam, SqlValue
from restate.sessions._object import Cancel, SessionContext, SessionObject, ViewContext
from restate.sessions._schema import Schema, Step
from restate.sessions._session import (
    Database,
    DatabaseReader,
    DatabaseWriter,
    Transaction,
    database,
    reset,
)

__all__ = [
    "CallFuture",
    "Cancel",
    "Database",
    "DatabaseReader",
    "DatabaseWriter",
    "Row",
    "Rows",
    "Rpc",
    "Schema",
    "Send",
    "SessionContext",
    "SessionObject",
    "SqlError",
    "SqlParam",
    "SqlValue",
    "Step",
    "Transaction",
    "ViewContext",
    "database",
    "reset",
]
