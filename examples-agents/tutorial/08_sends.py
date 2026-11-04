"""
08 · Sends that commit with the transaction

A transfer takes money out of alice's wallet and puts it into bob's. Alice's
wallet doesn't need an answer from bob's, so instead of calling it (07), it
sends a message, which doesn't wait. Waiting could even get stuck: if bob paid
alice at the same moment, each wallet would wait for the other, since each
runs one request at a time.

A send made with a transaction's ``tx.send`` belongs to that transaction:
Restate delivers it once the transaction commits, and never if it rolls back.
So the money leaves alice and arrives at bob together, or not at all::

    uv run examples-agents/tutorial/08_sends.py                                         # and register it (see 01)
    curl localhost:8080/wallet/alice/deposit --json '{"id": "d2", "amount": 100}'
    curl localhost:8080/wallet/alice/transfer --json '{"to": "bob", "amount": 30}'      # alice's balance after the transfer
    curl localhost:8080/wallet/bob/history                                              # ends with 30, from alice
    curl localhost:8080/wallet/alice/transfer --json '{"to": "bob", "amount": 5000}'    # fails, and bob gets nothing
"""

import asyncio

from pydantic import BaseModel

import restate
from restate import sessions

wallet = sessions.SessionObject(
    "wallet",
    migrations=[
        "CREATE TABLE entries (amount INTEGER NOT NULL)",
        "ALTER TABLE entries ADD COLUMN note TEXT",
        "ALTER TABLE entries ADD COLUMN id TEXT",
        "CREATE UNIQUE INDEX entries_by_id ON entries (id)",
    ],
)


class Deposit(BaseModel):
    id: str
    amount: int
    note: str | None = None


class Transfer(BaseModel):
    to: str
    amount: int


async def balance_of(db: sessions.DatabaseReader) -> int:
    row = await db.get("SELECT coalesce(sum(amount), 0) AS balance FROM entries")
    return int(row["balance"] or 0) if row else 0


@wallet.handler
async def deposit(ctx: sessions.SessionContext, money: Deposit) -> int:
    await ctx.db.execute(
        "INSERT OR IGNORE INTO entries (id, amount, note) VALUES (?, ?, ?)", [money.id, money.amount, money.note]
    )
    return await balance_of(ctx.db)


@wallet.handler
async def transfer(ctx: sessions.SessionContext, money: Transfer) -> int:
    async with ctx.db.transaction() as tx:
        # The transfer's id, the same on every attempt (06). Bob's wallet records it too.
        transfer_id = ctx.request.id
        # A retry finds the transfer already committed, with its send: it does nothing more.
        if await tx.get("SELECT 1 FROM entries WHERE id = ?", [transfer_id]) is None:
            await tx.execute(
                "INSERT INTO entries (id, amount, note) VALUES (?, ?, ?)",
                [transfer_id, -money.amount, f"to {money.to}"],
            )
            if await balance_of(tx) < 0:
                raise restate.TerminalError("Not enough money")
            # Delivered once this transaction commits, and never if it rolls back.
            tx.send(deposit, Deposit(id=transfer_id, amount=money.amount, note=f"from {ctx.key}"), key=money.to)
        return await balance_of(tx)


@wallet.view
async def balance(ctx: sessions.ViewContext) -> int:
    return await balance_of(ctx.db)


@wallet.view
async def history(ctx: sessions.ViewContext) -> list[sessions.Row]:
    return await ctx.db.query("SELECT amount, note FROM entries ORDER BY rowid")


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(wallet)))
