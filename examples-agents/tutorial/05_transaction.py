"""
05 · Transactions

Each statement on ``ctx.db`` commits on its own. When several changes must
happen together or not at all, put them in ``ctx.db.transaction()``: it commits
when its body ends, and rolls everything back if it raises.

Spending records the payment first, then checks the balance. If the balance
went below zero, it raises, and the payment is rolled back as if it never
happened.

It raises a ``TerminalError``, which tells Restate not to retry: the request
fails, and the caller gets the error. Restate retries any other error, which
suits failures that may go away, such as a lost connection::

    uv run examples-agents/tutorial/05_transaction.py                                    # and register it (see 01)
    curl localhost:8080/wallet/alice/deposit --json '{"amount": 30, "note": "salary"}'
    curl localhost:8080/wallet/alice/spend --json '{"amount": 12, "note": "lunch"}'      # the balance after lunch
    curl localhost:8080/wallet/alice/spend --json '{"amount": 1000, "note": "a boat"}'   # fails: not enough money
    curl localhost:8080/wallet/alice/history                                            # lunch, but no boat
"""

import asyncio

from pydantic import BaseModel

import restate
from restate import sessions

wallet = sessions.SessionObject(
    "wallet",
    migrations=["CREATE TABLE entries (amount INTEGER NOT NULL)", "ALTER TABLE entries ADD COLUMN note TEXT"],
)


class Money(BaseModel):
    amount: int
    note: str | None = None


async def balance_of(db: sessions.DatabaseReader) -> int:
    row = await db.get("SELECT coalesce(sum(amount), 0) AS balance FROM entries")
    return int(row["balance"] or 0) if row else 0


@wallet.handler
async def deposit(ctx: sessions.SessionContext, money: Money) -> int:
    await ctx.db.execute("INSERT INTO entries (amount, note) VALUES (?, ?)", [money.amount, money.note])
    return await balance_of(ctx.db)


@wallet.handler
async def spend(ctx: sessions.SessionContext, money: Money) -> int:
    async with ctx.db.transaction() as tx:
        await tx.execute("INSERT INTO entries (amount, note) VALUES (?, ?)", [-money.amount, money.note])
        balance = await balance_of(tx)
        # Raising rolls back the insert above.
        if balance < 0:
            raise restate.TerminalError("Not enough money")
        return balance


@wallet.view
async def balance(ctx: sessions.ViewContext) -> int:
    return await balance_of(ctx.db)


@wallet.view
async def history(ctx: sessions.ViewContext) -> list[sessions.Row]:
    return await ctx.db.query("SELECT amount, note FROM entries ORDER BY rowid")


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([wallet])))
