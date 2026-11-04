"""
Notes: SQL in a virtual object, with ``ctx.db``, and no agent.

Each key (``alice``, ``bob``) has a SQLite-compatible database of its own, kept
in the object's state: no database to run. ``add`` writes, one at a time per
key; ``list`` is a view, which reads while ``add`` writes::

    uv run examples-agents/basic/notes.py                                           # on port 9080
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/notes/alice/add --json '"Buy milk"'
    curl localhost:8080/notes/alice/list                                            # ["Buy milk"]
"""

import asyncio

import restate
from restate import sessions

# The schema of each key's database: its steps, applied once per key (a step added later runs then).
notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT NOT NULL)"])


@notes.handler
async def add(ctx: sessions.SessionContext, note: str) -> None:
    await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [note])


@notes.view(name="list")
async def list_notes(ctx: sessions.ViewContext) -> list[str]:
    """A view: it reads while ``add`` writes; a key with no notes yet has none."""
    return [str(row["text"]) for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(notes)))
