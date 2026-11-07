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
"""A client for agents, from any Python code: send messages, read and follow the conversation, resolve waits.

It talks to an agent through Restate's ingress, with ``restate.client``
(``pip install "restate-sdk[client]"``), so it needs none of the agent's
code, and works with an agent of any framework, in Python or TypeScript::

    from restate.client import create_client
    from restate.agents.client import AgentClient, as_text

    async with create_client("http://localhost:8080") as restate:
        alice = AgentClient(restate, "assistant", "alice")
        print(await alice.chat("What is the weather in Lisbon?"))
        async for change in alice.follow():
            for message in change.messages:
                print(as_text(message))

``python -m restate.agents chat assistant/alice`` is a terminal chat built on it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

try:
    import httpx2
except ImportError as error:
    raise ImportError(
        "restate.agents.client needs Restate's client: install the client extra, pip install restate-sdk[client]"
    ) from error

from restate.agents._api import ChatMessage, Messages, MessagesRequest, Signal, Status, View, Waiting
from restate.agents._object import Synced, SyncRequest
from restate.client_types import HttpError, RestateClient

_JSON = {"Content-Type": "application/json"}


@dataclass(frozen=True)
class Followed:
    """A change of the conversation, as ``AgentClient.follow`` gives it."""

    status: Status
    """What the agent is doing now."""
    messages: list[ChatMessage]
    """The messages new or changed since the last change, in the order they changed.

    A message comes again whenever it changes (a tool call gets its output,
    say), with the same ``id``: keep messages by their ids, and order them by
    them.
    """
    first: int
    """The id of the conversation's first message: ``clear`` forgot those before it, so drop them."""
    cursor: int
    """Where this change leaves off: ``follow(cursor=...)`` carries on from here, later or elsewhere."""

    @property
    def waiting(self) -> list[Waiting]:
        """What the agent's tools wait for now (``status["waiting"]``)."""
        return self.status["waiting"]


class AgentClient:
    """An agent's conversation with one key, through Restate's ingress.

    Args:
        client: Restate's client, from ``restate.client.create_client``.
        agent: The agent's name, such as ``"assistant"``.
        key: The conversation's key, such as ``"alice"``.
    """

    def __init__(self, client: RestateClient, agent: str, key: str) -> None:
        self.client = client
        self.agent = agent
        self.key = key

    async def _call(self, handler: str, body: object = None, *, idempotency_key: str | None = None) -> Any:
        sent = b"" if body is None else json.dumps(body).encode()
        answer = await self.client.generic_call(
            self.agent, handler, sent, key=self.key, idempotency_key=idempotency_key, headers=dict(_JSON)
        )
        return json.loads(answer) if answer else None

    async def chat(self, text: str, *, idempotency_key: str | None = None) -> Any:
        """Send ``text`` and wait for the answer: its text, or the JSON of an agent's ``output`` (a ``dict``).

        With an ``idempotency_key``, sending it again (after a lost
        connection, say) returns the same answer instead of asking again.
        """
        return await self._call("chat", text, idempotency_key=idempotency_key)

    async def send(self, text: str, *, idempotency_key: str | None = None) -> str:
        """Send ``text`` without waiting for the answer, and return the invocation's id.

        The agent answers it after the messages before it; ``follow`` shows
        the answer as it comes.
        """
        sent = await self.client.generic_send(
            self.agent,
            "chat",
            json.dumps(text).encode(),
            key=self.key,
            idempotency_key=idempotency_key,
            headers=dict(_JSON),
        )
        return sent.invocation_id

    async def messages(
        self,
        *,
        limit: int | None = None,
        before: int | None = None,
        cursor: int | None = None,
        view: View | None = None,
    ) -> Messages:
        """Return a page of the conversation: the latest ``limit`` messages (before id ``before``), or what changed since ``cursor``.

        ``view`` is how much of each turn to show: ``"full"`` (the default), or ``"summary"``, the user's
        message and the answer.
        """
        request: MessagesRequest = {}
        if view is not None:
            request["view"] = view
        if limit is not None:
            request["limit"] = limit
        if before is not None:
            request["before"] = before
        if cursor is not None:
            request["cursor"] = cursor
        return cast(Messages, await self._call("messages", request))

    async def status(self) -> Status:
        """Return what the agent is doing: the message it is answering, if any, and what its tools wait for."""
        return cast(Status, await self._call("status"))

    async def pending(self) -> list[Waiting]:
        """Return what the agent's tools wait for, oldest first."""
        return cast(list[Waiting], await self._call("pending"))

    async def signal(
        self, name: str | None = None, value: object = None, *, id: str | None = None, keep: bool = False
    ) -> str | None:
        """Resolve a wait with ``value``: the one with ``id``, or the oldest one for the signal ``name``.

        Returns the id of the wait it resolved, or ``None`` if nothing waited;
        with ``keep``, the signal is then kept for the next wait for ``name``.
        """
        request: Signal = {"value": value}
        if name is not None:
            request["name"] = name
        if id is not None:
            request["id"] = id
        if keep:
            request["keep"] = True
        return cast("str | None", (await self._call("signal", request))["resolved"])

    async def stop(self) -> str | None:
        """Cancel the message being answered, and return its invocation's id, or ``None`` if there was none."""
        return cast("str | None", (await self._call("stop"))["stopped"])

    async def clear(self) -> str | None:
        """Forget the conversation (stopping the message being answered, whose id it returns)."""
        return cast("str | None", (await self._call("clear"))["stopped"])

    async def follow(
        self, *, cursor: int = 0, wait: float = 50, view: View | None = None
    ) -> AsyncGenerator[Followed, None]:
        """Follow the conversation: what the agent does and the messages so far, then each change, as it comes.

        Each change is one ``sync`` that waits in Restate (for at most
        ``wait`` seconds) for the next. A lost connection, or Restate
        restarting, does not end it: it tries again a second later, with the
        same idempotency key, so the retry joins the sync already waiting.

        Args:
            cursor: Start after this cursor (a page's, or a change's); by default, from the start.
            wait: How long one wait for a change lasts at most, in seconds.
            view: How much of each turn to show: ``"full"`` (the default), or ``"summary"``.
        """
        revision: int | None = None
        while True:
            request: SyncRequest = {"cursor": cursor, "timeoutSeconds": wait}
            if view is not None:
                request["view"] = view
            if revision is not None:
                request["revision"] = revision
            synced = await self._synced(request)
            if synced["revision"] != revision or synced["messages"]:
                yield Followed(
                    status=synced["status"],
                    messages=synced["messages"],
                    first=synced.get("first", 0),
                    cursor=synced["cursor"],
                )
            cursor = synced["cursor"]
            # With more to read, at once; else once something changes.
            revision = None if synced["more"] else synced["revision"]

    async def _synced(self, request: SyncRequest) -> Synced:
        idempotency_key = str(uuid.uuid4())
        while True:
            try:
                return cast(Synced, await self._call("sync", request, idempotency_key=idempotency_key))
            except HttpError as error:
                if error.status_code < 500 and error.status_code != 429:
                    raise
            except httpx2.TransportError:
                pass
            await asyncio.sleep(1)


def as_text(message: ChatMessage, *, agent: str = "assistant") -> str:
    """Return ``message`` as a terminal shows it: its text after who said it, and each tool call on a line, with its output."""
    lines: list[str] = []
    for part in message["parts"]:
        if part["type"] == "text":
            lines.append(f"{'you' if message['role'] == 'user' else agent}: {part['text']}")
        else:
            output = part.get("output")
            lines.append(f"  → {part['name']}({json.dumps(part['input'])}) ← {'…' if output is None else output}")
    return "\n".join(lines)
