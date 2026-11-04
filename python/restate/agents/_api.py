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
"""
What clients of an agent read and send, as JSON: its conversation's messages
and pages of them, and the message it answers. The field names are
sdk-sessions' (``invocationId``), so that a client follows an agent of either
SDK.
"""

from __future__ import annotations

import typing
from typing import Any, Literal, TypedDict

Role = Literal["user", "assistant"]
"""Who said a message."""


class TextPart(TypedDict):
    """Text a message says."""

    type: Literal["text"]
    text: str


class _ToolCall(TypedDict):
    type: Literal["tool"]
    id: str
    name: str
    input: Any


class ToolPart(_ToolCall, total=False):
    """
    A tool call a message makes, with its output once it ran, and ``error:
    true`` if the output is the tool's error (``Error: <message>`` of a
    ``restate.TerminalError``, say).
    """

    output: str
    error: Literal[True]


Part = TextPart | ToolPart
"""What a message says: text, or a tool call."""


class Said(TypedDict):
    """A message as a framework's transcript gives it: its role and parts, without an id."""

    role: Role
    parts: list[Part]


class ChatMessage(Said):
    """A message of a conversation, as an agent's ``messages`` view gives it."""

    id: int
    """Its id, from 0, in the order of the conversation; the same however it changes. ``before`` takes it."""


class MessagesRequest(TypedDict, total=False):
    """
    Which messages to read: the latest ``limit``, or those before a message,
    for a page of the conversation; or those new or changed since a
    ``cursor``, to follow it.
    """

    limit: int
    """At most this many messages (default 50)."""
    before: int
    """The messages before this one (its ``id``): older ones, such as for scrolling back."""
    cursor: int
    """The messages new or changed since this cursor (a page's ``cursor``), by when they changed."""


class _Messages(TypedDict):
    messages: list[ChatMessage]
    more: bool
    cursor: int


class Messages(_Messages, total=False):
    """
    A page of messages: oldest first; or, with a ``cursor``, those new or
    changed since it, by when they changed. Keep them by ``id``: a message
    comes again once it changed (its tool call's output came, say). ``more``
    says whether there are more (older ones, or with a ``cursor``, newer
    ones); ``cursor`` is the one to read on from.
    """

    first: int
    """The id of the conversation's first message, once it was cleared: a client drops the messages it kept from before."""


class Turn(TypedDict):
    """The message a ``chat`` handler is answering: its invocation, and when it started."""

    invocationId: str
    """The invocation's id: ``/restate/invocation/<id>/attach`` waits for its answer."""
    since: str
    """When it started, as an ISO date."""


def text_part(text: str) -> TextPart:
    """A text part."""
    return {"type": "text", "text": text}


def is_tool_part(part: Part) -> typing.TypeGuard[ToolPart]:
    """Whether ``part`` is a tool call's."""
    return part["type"] == "tool"
