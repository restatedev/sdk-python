"""
02 · A database per key

Each key of a session object has its own SQLite database, ``ctx.db``. It is
stored in Restate together with the object's state, so there is no database
server to run. ``schema`` lists the statements that create the tables; they
run once per key, the first time that key is used.

The wallet records each deposit as a row, and the balance is their sum. Bob's
wallet is another key, so it has a database of its own::

    uv run examples-agents/tutorial/02_database.py       # and register it (see 01)
    curl localhost:8080/wallet/alice/deposit --json 10   # 10
    curl localhost:8080/wallet/alice/deposit --json 5    # 15
    curl localhost:8080/wallet/bob/deposit --json 7      # 7
"""

from restate import sessions

wallet = sessions.SessionObject("wallet", schema=["CREATE TABLE entries (amount INTEGER NOT NULL)"])


@wallet.handler
async def deposit(ctx: sessions.SessionContext, amount: int) -> int:
    await ctx.db.execute("INSERT INTO entries (amount) VALUES (?)", [amount])
    # `get` returns the first row as a dict, with a key per column.
    row = await ctx.db.get("SELECT sum(amount) AS balance FROM entries")
    return int(row["balance"] or 0) if row else 0


if __name__ == "__main__":
    sessions.serve(wallet)
