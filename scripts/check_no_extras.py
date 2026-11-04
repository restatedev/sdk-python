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
"""Check the SDK as an ordinary user installs it: ``pip install restate-sdk``, without extras.

Run it with the interpreter of a fresh environment where only the built wheel
is installed, from outside the checkout, so that it imports the installed
package rather than the sources::

    uv venv /tmp/plain && uv pip install --python /tmp/plain/bin/python dist/restate_sdk-*.whl
    cd /tmp && /tmp/plain/bin/python -I "$OLDPWD/scripts/check_no_extras.py"

It checks that:

- the package requires nothing beyond its extras, and nothing else is installed;
- ``import restate`` loads neither ``restate.sessions`` nor ``restate.agents``;
- an ordinary ``restate.app`` serves its discovery;
- ``restate.sessions`` and ``restate.agents`` import, and their apps serve too;
- each optional part (``restate.serve`` included) fails only when used, with an ``ImportError`` that names
  what to install.
"""

import asyncio
import importlib.metadata
import json
import sys
from collections.abc import Callable
from typing import Any

ALLOWED = {"restate-sdk", "pip", "setuptools", "wheel"}
"""What a fresh environment may hold beside the SDK."""


def check(what: str, condition: bool, detail: object = "") -> None:
    if not condition:
        sys.exit(f"FAILED {what}: {detail}")
    print(f"ok     {what}")


async def discover(app: Any) -> dict[str, Any]:
    """Ask ``app`` for its endpoint manifest, as Restate does on registration."""
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "http_version": "2",
        "method": "GET",
        "path": "/discover",
        "headers": [(b"accept", b"application/vnd.restate.endpointmanifest.v5+json")],
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    check("discovery answers 200", sent[0]["status"] == 200, sent[:1])
    return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


def refused(what: str, action: Callable[[], object], names: str) -> None:
    """Check that ``action`` raises an ``ImportError`` that names ``names``."""
    try:
        action()
    except ImportError as error:
        check(f"{what} needs {names}", names in str(error), error)
        return
    sys.exit(f"FAILED {what} worked without {names}")


def main() -> None:
    requirements = importlib.metadata.requires("restate-sdk") or []
    unconditional = [req for req in requirements if "extra ==" not in req]
    check("no requirement beyond the extras", not unconditional, unconditional)
    installed = {dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()}
    check("nothing installed beside restate-sdk", installed <= ALLOWED, sorted(installed - ALLOWED))

    import restate

    loaded = [name for name in ("restate.sessions", "restate.agents") if name in sys.modules]
    check("import restate loads neither sessions nor agents", not loaded, loaded)

    greeter = restate.Service("greeter")

    @greeter.handler()
    async def greet(ctx: restate.Context, name: str) -> str:
        return f"Hello {name}"

    manifest = asyncio.run(discover(restate.app([greeter])))
    check("an ordinary restate.app serves", [s["name"] for s in manifest["services"]] == ["greeter"], manifest)

    import restate.agents
    import restate.agents.internal
    import restate.sessions.internal
    from restate import sessions

    notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT)"])

    @notes.handler
    async def add(ctx: sessions.SessionContext, text: str) -> None:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])

    manifest = asyncio.run(discover(sessions.app(notes, restate.agents.AgentObject("assistant"))))
    served = sorted(s["name"] for s in manifest["services"])
    check("sessions and agents serve", served == ["assistant", "notes", "signals"], served)

    refused("restate.agents.tool", lambda: restate.agents.tool(lambda city: city), "pydantic")
    refused("restate.agents.langgraph", lambda: __import__("restate.agents.langgraph"), "restate-sdk[langgraph]")
    refused("restate.agents.pydantic_ai", lambda: __import__("restate.agents.pydantic_ai"), "restate-sdk[pydantic_ai]")
    refused("restate.agents.anthropic", lambda: __import__("restate.agents.anthropic"), "restate-sdk[anthropic]")
    refused("restate.serve", lambda: asyncio.run(restate.serve(sessions.app(notes))), "restate-sdk[serve]")


if __name__ == "__main__":
    main()
