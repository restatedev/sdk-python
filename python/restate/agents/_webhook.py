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
"""Webhooks into agents: another system's webhook (Linear's, GitHub's...) as messages to agents.

``webhook`` makes a Restate service whose one handler, ``webhook``, takes a
delivery, checks its signature, and sends it to agents as a message saying
where it comes from. The other system posts to Restate's ingress, at
``/<name>/webhook``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import typing
from collections.abc import Callable, Sequence
from typing import Any

import restate
from restate.agents._signals import deliver, has_companion
from restate.exceptions import TerminalError
from restate.serde import BytesSerde

Event = dict[str, Any]
"""A delivery's body: the JSON object the other system sent, as it is."""


def webhook_message(name: str, delivery: str, event: object) -> str:
    """Return the message an agent gets for a delivery: its body as JSON, in a ``<webhook>`` element.

    For example::

        <webhook name="linear" delivery="5d2…">
        {"action": "create", "type": "Issue", ...}
        </webhook>

    Args:
        name: The webhook's name.
        delivery: The delivery's id.
        event: What of the delivery the agent reads.

    Returns:
        The message.
    """
    return f'<webhook name="{name}" delivery="{delivery}">\n{json.dumps(event, indent=2)}\n</webhook>'


def _signed(body: bytes, signature: str | None, secret: str) -> bool:
    """Return whether ``signature`` is the HMAC-SHA256 of ``body`` with ``secret``, in hex (GitHub's ``sha256=`` prefix too)."""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    try:
        given = bytes.fromhex((signature or "").removeprefix("sha256="))
    except ValueError:
        return False
    return hmac.compare_digest(given, expected)


def _parsed(body: bytes) -> Event:
    """Return ``body`` as a JSON object, or raise a terminal error (400) if it is not one."""
    try:
        event = json.loads(body)
    except ValueError:
        raise TerminalError("The webhook is not JSON", status_code=400) from None
    if not isinstance(event, dict):
        raise TerminalError("The webhook is not a JSON object", status_code=400)
    return typing.cast(Event, event)


def webhook(
    name: str,
    *,
    route: Sequence[object],
    key: Callable[[Event], str | None] | None = None,
    message: Callable[[Event], object] | None = None,
    secret: str | None = None,
    signature_header: str | None = None,
    idempotency_header: str | None = None,
    description: str | None = None,
) -> restate.Service:
    """Create a webhook into agents: a service whose ``webhook`` handler delivers another system's webhooks.

    Each delivery goes to each agent of ``route``, under the key that ``key``
    gives, as a ``webhook_message``: a message of its own, which the agent
    answers after the messages before it. The handler answers once the
    message is sent, not answered, so a system that waits only seconds for an
    answer (Linear, GitHub) gets one at once::

        issues = webhook(
            "linear",
            route=[triage],
            key=lambda event: event["data"]["identifier"],
            secret=os.environ["LINEAR_WEBHOOK_SECRET"],
            signature_header="linear-signature",
            idempotency_header="linear-delivery",
        )
        asyncio.run(restate.serve(sessions.app(issues, triage)))

    The handler's metadata says what it is (its routes, its signature and its
    idempotency header), which Restate's admin API shows.

    Args:
        name: The service's name; the other system posts to ``/<name>/webhook``.
        route: The agents each delivery goes to (``restate.agents`` agents).
        key: The key of the agents' objects that a delivery goes to, from its
            body (the issue, say: one conversation per issue); ``None`` drops
            the delivery. Defaults to the delivery's id: a conversation per delivery.
        message: What of a delivery the agents read, from its body. Defaults to
            all of it; some systems send much more than an agent needs.
        secret: The webhook's signing secret: a delivery without the
            HMAC-SHA256 of its body with it, in hex, in ``signature_header``,
            is refused (401). Without it, any delivery is taken.
        signature_header: The header with the signature, such as
            ``linear-signature`` or ``X-Hub-Signature-256``, in any case.
        idempotency_header: The header with the delivery's id, which a
            delivery sent again has too (such as ``linear-delivery``): it
            reaches the agents once. Without it, each request is a delivery of its own.
        description: What the webhook is, for Restate's UI.

    Returns:
        The service, to serve beside the agents.

    Raises:
        ValueError: If ``route`` is empty, or only one of ``secret`` and
            ``signature_header`` is given (or ``secret`` is empty: unset in the environment?).
        TypeError: If ``route`` has something that is not an agent of ``restate.agents``.
    """
    if not route:
        raise ValueError(f"webhook {name}: route it to at least one agent")
    for agent in route:
        if not has_companion(agent):
            raise TypeError(f"webhook {name}: {agent!r} is not an agent of restate.agents")
    if (secret is not None or signature_header is not None) and not (secret and signature_header):
        raise ValueError(
            f"webhook {name}: give both secret and signature_header, or neither (is the secret unset in the environment?)"
        )
    names = [str(getattr(agent, "name")) for agent in route]  # noqa: B009 (an agent's definition)
    what = description or f"A webhook into {', '.join(names)}"
    metadata = {
        "restate-agents.kind": "webhook",
        "restate-agents.routes": ",".join(names),
        "restate-agents.auth": f"hmac-sha256:{signature_header}" if signature_header else "none",
    }
    if idempotency_header:
        metadata["restate-agents.idempotency-header"] = idempotency_header
    service = restate.Service(name, description=what)

    async def receive(ctx: restate.Context, body: bytes) -> None:
        request = ctx.request()
        headers = {header.lower(): value for header, value in request.headers.items()}
        if secret and signature_header and not _signed(body, headers.get(signature_header.lower()), secret):
            raise TerminalError(f"Not signed: no valid {signature_header}", status_code=401)
        event = _parsed(body)
        delivery = (idempotency_header and headers.get(idempotency_header.lower())) or request.id
        to = key(event) if key is not None else delivery
        if to is None:
            return
        text = webhook_message(name, delivery, message(event) if message is not None else event)
        for agent in route:
            deliver(ctx, agent, to, text, idempotency_key=f"webhook:{name}:{delivery}")

    service.handler("webhook", accept="*/*", input_serde=BytesSerde(), metadata=metadata)(receive)
    return service
