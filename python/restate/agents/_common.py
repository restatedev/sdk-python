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
"""Helpers shared by the framework integrations.

This covers per-invocation options (``PerSession``) and model names, the ids
of an invocation's prompts, recognising a model provider's refusal, and how
the ``chat`` handler reads its message.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import typing
import weakref
from collections.abc import Awaitable, Callable

from restate.exceptions import TerminalError
from restate.serde import Serde
from restate.sessions import SessionContext
from restate.sessions.internal import ScopeKey

T = typing.TypeVar("T")

PerSession: typing.TypeAlias = T | Callable[[SessionContext], T | Awaitable[T]]
"""A value, or a function (sync or async) that takes the invocation's context and returns it.

Use a plain value for options that are the same for every message, and a
function for options that differ per user::

    langgraph.agent("assistant", model="anthropic/claude-sonnet-5-5")
    langgraph.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")
"""


async def configured(value: PerSession[T], ctx: SessionContext) -> T:
    """Return ``value`` for the invocation of ``ctx``, calling and awaiting it if it is a function."""
    if not callable(value):
        return value
    made = typing.cast(Callable[[SessionContext], T | Awaitable[T]], value)(ctx)
    return await made if inspect.isawaitable(made) else typing.cast(T, made)


_PROMPTS: ScopeKey[int] = ScopeKey("next_prompt_id")


def next_prompt_id(ctx: SessionContext) -> str:
    """Return the id of the invocation's next prompt.

    The first prompt's id is the invocation id, the next ones are ``<id>:2``,
    ``<id>:3``, and so on. A retried attempt makes its prompts in the same
    order, so each prompt gets the same id again, and finds its stored answer.
    """
    n = (_PROMPTS.get(ctx) or 0) + 1
    _PROMPTS.set(ctx, n)
    return ctx.request.id if n == 1 else f"{ctx.request.id}:{n}"


MODEL_ENV = "RESTATE_AGENT_MODEL"
"""The environment variable that names the model agents use when none is given."""

DEFAULT_MODEL = "openai/gpt-6-sol"
"""The default model of the frameworks that accept any provider; it needs ``OPENAI_API_KEY``."""

CLAUDE_MODEL = "anthropic/claude-opus-5-5"
"""The default model of the frameworks that accept only Anthropic models; it needs ``ANTHROPIC_API_KEY``."""


def model_of(model: str | None, fallback: str = DEFAULT_MODEL) -> str:
    """Return ``model``, else ``$RESTATE_AGENT_MODEL``, else the framework's default (``fallback``)."""
    return model or os.environ.get(MODEL_ENV) or fallback


def provider_of(model: str) -> tuple[str, str]:
    """Split ``"provider/model-id"`` (or ``"provider:model-id"``) into its provider and model id.

    It splits at the first ``/``, else at the first ``:``, so a model id that
    contains a colon (``"openai/ft:gpt-4o:acme"``) keeps it. A name with
    neither has no provider, and returns ``("", model)``.
    """
    for separator in ("/", ":"):
        provider, found, model_id = model.partition(separator)
        if found:
            return provider, model_id
    return "", model


REFUSED = 424
"""The error code of a message that the model's provider refused, such as for a wrong API key or an unknown model.

It is the HTTP status 424 Failed Dependency.
"""

CHECK_MODEL = f"check the agent's model (its `model` option, or {MODEL_ENV}) and its provider's API key"
"""The advice in the error message when a model's provider refuses a request."""


class ModelError(TerminalError):
    """An error for a model name that an agent cannot use.

    The name is not ``provider/model-id``, belongs to another provider, or is
    unknown to the framework. It is a terminal error with status
    ``REFUSED``, since a retry would use the same model.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=REFUSED)


def check_model_name(framework: str, model: str | None) -> None:
    """Raise ``ModelError`` if ``model``, passed explicitly to a ``framework`` agent, does not name a provider."""
    if model is not None and not re.fullmatch(r"[^/:\s]+[/:]\S+", model):
        raise ModelError(f'{framework}: the model "{model}" is not provider/model-id, such as {CLAUDE_MODEL}')


def model_id_of(model: str, provider: str) -> str:
    """Return the model id of ``model`` for a framework that only takes ``provider``'s models.

    ``model`` is either ``provider/model-id`` or the bare model id.

    Raises:
        ModelError: If ``model`` names another provider, or no model id.
    """
    named, model_id = provider_of(model) if "/" in model or ":" in model else (provider, model)
    if named != provider or not model_id:
        from_env = f" (from {MODEL_ENV})" if os.environ.get(MODEL_ENV) == model else ""
        raise ModelError(f'the model "{model}"{from_env} is not {provider}\'s: this agent takes {provider}/<model-id>')
    return model_id


class MessageSerde(Serde[str]):
    """The serde of a chat message: the request body as plain text, or a JSON string (``"Hi"``)."""

    def deserialize(self, buf: bytes) -> str | None:
        text = buf.decode()
        try:
            value = json.loads(text)
        except ValueError:
            return text
        return value if isinstance(value, str) else text

    def serialize(self, obj: str | None) -> bytes:
        return (obj or "").encode()


CANCELLED = "Cancelled: the turn was stopped before it finished."
"""The answer of a cancelled turn, and the result of each tool call it cut off, which the model sees later in the conversation."""

RETURNED_UNRECORDED = (
    "Done, but the turn was stopped before this call's result was recorded: it did happen, so check its "
    "effect before doing it again."
)
"""The result of a tool call cut off by a stopped turn after its Restate call had returned successfully."""


def cut_off_result(outcomes: dict[str, str | None], call_id: str, text: str) -> str:
    """Return what a stopped turn records as the result of the tool call ``call_id``, which has none.

    A call whose Restate call returned is not reported as ``text`` (cancelled, say): the model would take
    it for undone, and repeat its effect. It gets the error it returned, or ``RETURNED_UNRECORDED``.
    """
    if call_id not in outcomes:
        return text
    failed = outcomes[call_id]
    return failed if failed is not None else RETURNED_UNRECORDED


UNANSWERED = "Unanswered: the model's provider refused the request."
"""The stored answer of a prompt that the model's provider refused for good.

It ends the prompt, so that the model sees it in the rest of the conversation
instead of answering the refused prompt along with the next message.
"""


class ToolError(Exception):
    """An error raised by tool ``tool``, which fails the attempt so that Restate retries it."""

    def __init__(self, tool: str, cause: BaseException) -> None:
        super().__init__(f"the tool {tool} failed: {cause}")
        self.tool: str = tool
        self.__cause__ = cause


_TOOL_ERRORS: ScopeKey[weakref.WeakSet[BaseException]] = ScopeKey("from_tool")
"""The errors that framework tools (not those made by ``tool()``) raised in the attempt; see ``from_tool``."""


def from_tool(ctx: SessionContext, error: BaseException) -> None:
    """Record that a framework's own tool raised ``error`` in the attempt of ``ctx``.

    Such an error is never mistaken for a refusal by the model's provider,
    even if it looks like one (a tool that calls a model API can fail the same
    way).
    """
    _TOOL_ERRORS.of(ctx, weakref.WeakSet[BaseException]).add(error)


_FOR_GOOD = frozenset({"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"})
"""The names of the model SDKs' errors (Anthropic's, OpenAI's) for requests that would fail again on retry."""

_REFUSED_TEXT = re.compile(
    r"API key not found|no API key|Could not resolve authentication method|Provider is not configured"
    r'|"code":"(AUTH|MISSING_CREDENTIAL)"',
    re.IGNORECASE,
)


_FOR_GOOD_STATUS = frozenset({400, 401, 403, 404})
"""The HTTP statuses of Pydantic AI's ``ModelHTTPError`` for requests that would fail again on retry."""


def _refused(error: BaseException) -> bool:
    """Return whether ``error`` is a model provider's refusal that a retry would not change, such as a missing key."""
    if isinstance(error, ToolError):
        return False
    if type(error).__name__ in _FOR_GOOD:
        return True
    if type(error).__name__ == "ModelHTTPError" and getattr(error, "status_code", None) in _FOR_GOOD_STATUS:
        return True
    return bool(_REFUSED_TEXT.search(str(error)))


def refusal(error: BaseException) -> TerminalError | None:
    """Return ``error`` as a terminal error with status ``REFUSED`` if retrying would not help, else ``None``.

    That is the case for a model the agent cannot use, and for a refusal by
    the model's provider.
    """
    if isinstance(error, ModelError):
        return error
    if isinstance(error, TerminalError) or not _refused(error):
        return None
    terminal = TerminalError(f"the model's provider refused the request: {CHECK_MODEL}. {error}", status_code=REFUSED)
    terminal.__cause__ = error
    return terminal


def refusal_of(ctx: SessionContext, error: BaseException) -> TerminalError | None:
    """Return ``refusal(error)``, except for errors that a framework's tool raised in the attempt (see ``from_tool``)."""
    failed_tool = _TOOL_ERRORS.get(ctx)
    if failed_tool is not None and error in failed_tool:
        return None
    return refusal(error)


def stored_answer(answer: str) -> str:
    """Return a prompt's stored answer, or raise the refusal again if it is ``UNANSWERED``."""
    if answer != UNANSWERED:
        return answer
    raise TerminalError(f"the model's provider refused the request: {CHECK_MODEL}", status_code=REFUSED)
