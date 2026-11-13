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
"""The shapes an agent's handlers and clients are written to.

The contexts come first: ``AgentContext`` and ``AgentViewContext``, which
name the layer a handler or a view belongs to. The rest are the JSON types
an agent's clients send and receive, which cover the conversation's messages
and pages of them, the agent's status, and signals. Some keys are camelCase
(``invocationId``, ``timeoutSeconds``): they are wire-format names, shared
with Restate's other SDKs so that the same client can follow an agent
written in any of them.
"""

from __future__ import annotations

import typing
from typing import Any, Literal, Protocol, TypedDict

from restate.sessions import SessionContext, ViewContext


class AgentContext(SessionContext, Protocol):
    """The context of an agent's handler: a session context, from an object that has a companion.

    The agent's own surface takes it: the ``answer`` of ``chat()``, the
    agent's handlers, and what only an agent does with a context
    (``chat_turn``, ``publish``). The layer a handler belongs to is then in
    its signature, where the person writing it reads it, rather than found
    out in the middle of a turn, after a model round-trip, looking like a
    tool failure. It is a signpost, not a fence: nothing of it exists at
    runtime, the handler is passed the context a session object always
    passes, and ``NeedsAgent`` stays the guard that stops a wait from a
    session object.

    The tier below keeps ``SessionContext``: the tools, which are values a
    framework may run from either, and the conversation a handler of one's
    own prompts with (``answer_once``, and a framework's ``prompt``), which
    a plain session object drives too.
    """

    @property
    def _agent(self) -> None:
        """A marker, never present at runtime and never read: it is what makes a plain session context
        fail to be an ``AgentContext`` for a type checker."""


class AgentViewContext(ViewContext, Protocol):
    """The context of an agent's view: a view context, from an object that has a companion.

    What ``AgentContext`` is to a handler, for the views that read what the
    agent is doing, or tell its companion to (``status``, ``pending``,
    ``signal``, ``steer``, ``watch``). Its marker is the same fiction.
    """

    @property
    def _agent(self) -> None:
        """A marker, never present at runtime and never read: it is what makes a plain view context
        fail to be an ``AgentViewContext`` for a type checker."""


Role = Literal["user", "assistant"]
"""Who wrote a message."""


class TextPart(TypedDict):
    """A message's text."""

    type: Literal["text"]
    text: str


class _ToolCall(TypedDict):
    type: Literal["tool"]
    id: str
    name: str
    input: Any


class ToolPart(_ToolCall, total=False):
    """A tool call in a message, with its output once the tool has run.

    ``error`` is ``True`` if the output is an error, such as the
    ``"Error: <message>"`` of a ``restate.TerminalError``.
    """

    output: str
    error: Literal[True]


Part = TextPart | ToolPart
"""A part of a message: text, or a tool call."""


class Said(TypedDict):
    """A message as a framework's transcript produces it: its role and parts, without an id."""

    role: Role
    parts: list[Part]


class ChatMessage(Said):
    """A message of a conversation, as an agent's ``messages`` view returns it."""

    id: int
    """The message's id, which increases through the conversation and stays the same when the message changes."""


View = Literal["full", "summary"]
"""How much of each turn the ``messages`` view shows.

``"full"``: every message, the tool calls with their outputs. ``"summary"``:
the user's message and the answer, one each per turn, with the same ids as in
the full view.
"""


class MessagesRequest(TypedDict, total=False):
    """The request of the ``messages`` view.

    For a page of the conversation, pass ``limit`` and optionally ``before``.
    To follow the conversation, pass the ``cursor`` of the previous response.
    """

    limit: int
    """The maximum number of messages to return (default 50)."""
    before: int
    """Return the messages before the one with this ``id``, for scrolling back."""
    cursor: int
    """Return the messages that are new or changed since this cursor, in the order they changed."""
    view: View
    """How much of each turn to show: ``"full"`` (the default) or ``"summary"``."""


class _Messages(TypedDict):
    messages: list[ChatMessage]
    more: bool
    cursor: int


class Messages(_Messages, total=False):
    """The response of the ``messages`` view: a page of messages.

    Without a ``cursor`` in the request, the messages are oldest first. With
    one, they are the messages that are new or changed since it, in the order
    they changed. Store messages by ``id``: a message comes again whenever it
    changes, for example when its tool call's output arrives.

    ``more`` says whether there are more messages (older ones, or with a
    ``cursor``, newer ones), and ``cursor`` is the cursor to read on from.
    """

    first: int
    """The id of the first message after the conversation was cleared; a client drops the messages it kept from before."""


class Answering(TypedDict):
    """The message a ``chat`` handler is answering: its invocation and when it started.

    Its wire format is shared with Restate's other SDKs. The conversation's turns (one per prompt) are
    ``restate.agents.internal.Turn``; an invocation of a handler of your own can hold several.
    """

    invocationId: str
    """The invocation's id; ``/restate/invocation/<id>/attach`` waits for its answer."""
    since: str
    """When it started, as an ISO 8601 timestamp."""


def text_part(text: str) -> TextPart:
    """Return a text part with ``text``."""
    return {"type": "text", "text": text}


def is_tool_part(part: Part) -> typing.TypeGuard[ToolPart]:
    """Return whether ``part`` is a tool call."""
    return part["type"] == "tool"


class _Waiting(TypedDict):
    id: str
    name: str
    since: str


class Waiting(_Waiting, total=False):
    """A wait for a signal, as the ``pending`` view lists it.

    ``id`` identifies the wait, so that ``signal`` can resolve this wait and
    no other; ``name`` is the signal it waits for (such as ``"approval"``);
    and ``since`` is when it started, as an ISO 8601 timestamp.
    """

    input: Any
    """What it waits for, as the model described it (such as the refund to approve), if the tool takes an input."""


class Signal(TypedDict, total=False):
    """The request of the ``signal`` view: which wait to resolve, and with what value."""

    id: str
    """Resolve the wait with this id, as ``pending`` lists it."""
    name: str
    """Without an ``id``, resolve the oldest wait for the signal of this name."""
    value: Any
    """The value, which the tool returns to the model."""
    keep: bool
    """If nothing waits for ``name`` yet, keep the signal for the next wait instead of dropping it."""


class Resolved(TypedDict):
    """The response of the ``signal`` view: the id of the wait it resolved, or ``None`` if nothing was waiting."""

    resolved: str | None


class Notifications(TypedDict):
    """The revision of an agent's conversation, which increases with every change a client can read."""

    revision: int


class Steer(TypedDict):
    """A message steered to the turn being answered (``steer``): its id (the ``steer`` request's) and its text."""

    id: str
    text: str


class Published(Notifications, total=False):
    """What the companion's ``publish`` returns to the turn that published: the new revision, and the messages
    steered to that turn that it has not taken yet, if any, oldest first."""

    steering: list[Steer]


class Steering(TypedDict):
    """A message steered to the turn being answered that it has not taken yet, as ``status`` shows it."""

    id: str
    since: str
    """When it was sent, as an ISO 8601 timestamp."""


class Steered(TypedDict):
    """The response of the ``steer`` view.

    ``id`` is the message's (the request's id). ``turn`` is the invocation of
    the turn being answered, which takes the message at its next step if it
    reaches one; ``None`` if no turn takes it. Either way the message is
    answered once: in that turn, or else as a turn of its own, in the order
    it was sent.
    """

    id: str
    turn: str | None


class _Watch(TypedDict):
    revision: int


class Watch(_Watch, total=False):
    """The request of the ``watch`` view: wait for a revision newer than ``revision``, for at most ``timeoutSeconds`` (default 50, at most 60)."""

    timeoutSeconds: float


class _Status(TypedDict):
    answering: Answering | None
    waiting: list[Waiting]


class Status(_Status, total=False):
    """What an agent is doing: the message it is answering, if any, and what its tools wait for, oldest first."""

    steering: list[Steering]
    """The messages steered to the turn being answered that it has not taken yet, oldest first; absent if none."""


class Stopped(TypedDict):
    """The response of the ``stop`` view: the invocation it cancelled, or ``None`` if no message was being answered."""

    stopped: str | None
