"""
04 · Migrations: changing the tables

Alice's database already exists, so editing its CREATE TABLE statement would
change nothing for her. That's why ``migrations`` is a list of steps: to change
the tables, add a step at the end. Each key's database runs the steps it hasn't
run yet, the next time it is used. Never edit or remove a step that may have
run somewhere; add a new one instead.

Here, deposits get a note::

    uv run examples-agents/tutorial/04_migrations.py                                    # and register it (see 01)
    curl localhost:8080/wallet/alice/deposit --json '{"amount": 50, "note": "birthday"}'
    curl localhost:8080/wallet/alice/history                                            # the earlier deposits have no note
"""

import asyncio

from pydantic import BaseModel

import restate
from restate import sessions

wallet = sessions.SessionObject(
    "wallet",
    migrations=[
        "CREATE TABLE entries (amount INTEGER NOT NULL)",  # from lesson 02
        "ALTER TABLE entries ADD COLUMN note TEXT",  # new: runs once on each wallet's database
    ],
)


class Deposit(BaseModel):
    amount: int
    note: str | None = None


async def balance_of(db: sessions.DatabaseReader) -> int:
    row = await db.get("SELECT coalesce(sum(amount), 0) AS balance FROM entries")
    return int(row["balance"] or 0) if row else 0


@wallet.handler
async def deposit(ctx: sessions.SessionContext, money: Deposit) -> int:
    await ctx.db.execute("INSERT INTO entries (amount, note) VALUES (?, ?)", [money.amount, money.note])
    return await balance_of(ctx.db)


@wallet.view
async def balance(ctx: sessions.ViewContext) -> int:
    return await balance_of(ctx.db)


@wallet.view
async def history(ctx: sessions.ViewContext) -> list[sessions.Row]:
    return await ctx.db.query("SELECT amount, note FROM entries ORDER BY rowid")


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(wallet)))
