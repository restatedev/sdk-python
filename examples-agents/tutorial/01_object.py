"""
01 · A virtual object

Throughout this tutorial we build a wallet. Every user has one, and Restate
finds it by key: ``wallet/alice`` and ``wallet/bob`` are two instances of the
same object. Restate runs the requests for one key one at a time, so a wallet
never handles two of them at once.

This process serves the wallet's handlers. Restate calls them over HTTP, and
you call them through Restate::

    uv run examples-agents/tutorial/01_object.py                                    # serves the wallet on port 9080
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080   # tells Restate where the wallet is
    curl localhost:8080/wallet/alice/hello                                          # "Hello, alice!"

Every lesson serves on port 9080, so stop this one before you start the next,
and register again.
"""

import asyncio

import restate
from restate import sessions

# A session object: a virtual object that gets a database in the next lesson.
wallet = sessions.SessionObject("wallet")


@wallet.handler
async def hello(ctx: sessions.SessionContext) -> str:
    # `ctx.key` is the key the request was sent to: the wallet's owner.
    return f"Hello, {ctx.key}!"


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(wallet)))
