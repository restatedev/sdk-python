"""
07 · Calling another object

Friends should be able to pay alice without knowing her wallet's key, so she
picks a handle, such as @ace. Handles are a second session object: each handle
is a key with its own database, which records the wallet that owns it. The
first wallet to claim a handle gets it.

``ctx.call(claim_handle, wallet, key=name)`` calls that object through Restate,
and waits for the answer. A retried handler (06) makes its calls again, so a
call that changes something gets an idempotency key that is the same on every
attempt. Restate then answers a repeated call with the first call's result,
instead of running it again::

    uv run examples-agents/tutorial/07_calls.py               # and register it (see 01)
    curl localhost:8080/wallet/alice/claim --json '"ace"'     # "alice is now @ace"
    curl localhost:8080/wallet/bob/claim --json '"ace"'       # fails: @ace belongs to alice
    curl localhost:8080/handle/ace/owner                      # "alice"
"""

from pydantic import BaseModel

import restate
from restate import sessions

handle = sessions.SessionObject("handle", schema=["CREATE TABLE owner (wallet TEXT NOT NULL)"])


@handle.handler(name="claim")
async def claim_handle(ctx: sessions.SessionContext, wallet: str) -> str:
    """Give the handle to ``wallet``, unless another wallet has it, and answer with its owner."""
    await ctx.db.execute("INSERT INTO owner (wallet) SELECT ? WHERE NOT EXISTS (SELECT 1 FROM owner)", [wallet])
    owner = await ctx.db.get("SELECT wallet FROM owner")
    assert owner is not None  # the insert above made sure there is one
    return str(owner["wallet"])


@handle.view
async def owner(ctx: sessions.ViewContext) -> str | None:
    row = await ctx.db.get("SELECT wallet FROM owner")
    return str(row["wallet"]) if row else None


wallet = sessions.SessionObject(
    "wallet",
    schema=[
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
async def claim(ctx: sessions.SessionContext, name: str) -> str:
    owner = await ctx.call(claim_handle, ctx.key, key=name, idempotency_key=f"{ctx.request.id}:claim")
    if owner != ctx.key:
        raise restate.TerminalError(f"@{name} belongs to {owner}")
    return f"{ctx.key} is now @{name}"


@wallet.view
async def balance(ctx: sessions.ViewContext) -> int:
    return await balance_of(ctx.db)


if __name__ == "__main__":
    sessions.serve(wallet, handle)
