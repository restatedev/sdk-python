"""
06 · Retries

When a handler fails partway, because the process crashed or a connection
dropped, Restate runs it again from the first line. Whatever the failed attempt
committed is still in the database, so every handler must be safe to run twice.

The way to do that is an id that is the same on every attempt. A deposit now
comes with an id from the caller, and ``INSERT OR IGNORE`` skips a deposit whose
id is already there: running it twice, or sending it twice, counts it once.
Spending has no id from the caller, so it uses ``ctx.request.id``, the id
Restate gives each request, which also stays the same across attempts.

To show it, this lesson fails the first attempt of every deposit right after
the insert, the way a crash would. Restate retries it, and the balance still
counts the deposit once::

    uv run examples-agents/tutorial/06_retries.py                                                   # and register it (see 01)
    curl localhost:8080/wallet/alice/deposit --json '{"id": "d1", "amount": 10, "note": "refund"}'   # counted once, though the first attempt failed
    curl localhost:8080/wallet/alice/deposit --json '{"id": "d1", "amount": 10, "note": "refund"}'   # sent again: the balance stays the same
"""

from pydantic import BaseModel

import restate
from restate import sessions

wallet = sessions.SessionObject(
    "wallet",
    migrations=[
        "CREATE TABLE entries (amount INTEGER NOT NULL)",
        "ALTER TABLE entries ADD COLUMN note TEXT",
        # New: an id per entry. Entries from earlier lessons have none, which a unique index allows.
        "ALTER TABLE entries ADD COLUMN id TEXT",
        "CREATE UNIQUE INDEX entries_by_id ON entries (id)",
    ],
)


class Deposit(BaseModel):
    id: str
    amount: int
    note: str | None = None


class Money(BaseModel):
    amount: int
    note: str | None = None


failed: set[str] = set()


def fail_first_attempt(ctx: sessions.SessionContext) -> None:
    """Fail the first attempt of a request, as a crash would. Restate then retries it."""
    if ctx.request.id in failed:
        return
    failed.add(ctx.request.id)
    raise RuntimeError("Simulated crash, after the deposit was committed")


async def balance_of(db: sessions.DatabaseReader) -> int:
    row = await db.get("SELECT coalesce(sum(amount), 0) AS balance FROM entries")
    return int(row["balance"] or 0) if row else 0


@wallet.handler
async def deposit(ctx: sessions.SessionContext, money: Deposit) -> int:
    await ctx.db.execute(
        "INSERT OR IGNORE INTO entries (id, amount, note) VALUES (?, ?, ?)", [money.id, money.amount, money.note]
    )
    fail_first_attempt(ctx)
    return await balance_of(ctx.db)


@wallet.handler
async def spend(ctx: sessions.SessionContext, money: Money) -> int:
    async with ctx.db.transaction() as tx:
        await tx.execute(
            "INSERT OR IGNORE INTO entries (id, amount, note) VALUES (?, ?, ?)",
            [ctx.request.id, -money.amount, money.note],
        )
        balance = await balance_of(tx)
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
    sessions.serve(wallet)
