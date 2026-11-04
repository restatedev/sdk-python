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
What the frameworks share: their options (``Configure``, the model), the ids of
an invocation's prompts, a model provider's refusal, and the ``chat`` message.
As sdk-sessions' ``common.ts``.
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

Configure: typing.TypeAlias = T | Callable[[SessionContext], T | Awaitable[T]]
"""
A value, or a function of the invocation's context that gives it: the same for
every message, or each user's (from the object's database, say)::

    langgraph.agent("assistant", model="anthropic/claude-sonnet-5-5")
    langgraph.agent("assistant", model=lambda ctx: user_model(ctx))
"""


async def configured(value: Configure[T], ctx: SessionContext) -> T:
    """``value`` for the invocation of ``ctx``."""
    if not callable(value):
        return value
    made = typing.cast(Callable[[SessionContext], T | Awaitable[T]], value)(ctx)
    return await made if inspect.isawaitable(made) else typing.cast(T, made)


_PROMPTS: ScopeKey[int] = ScopeKey("next_prompt_id")


def next_prompt_id(ctx: SessionContext) -> str:
    """
    The id of the next prompt of the invocation of ``ctx``: the invocation's
    id, then ``<id>:2``, ``<id>:3``... A retried attempt makes its prompts in
    the same order, so each gets the same id again, and its answer.
    """
    n = (_PROMPTS.get(ctx) or 0) + 1
    _PROMPTS.set(ctx, n)
    return ctx.request.id if n == 1 else f"{ctx.request.id}:{n}"


DEFAULT_MODEL = "openai/gpt-6-sol"
"""The default model of the frameworks that take any provider's, with OPENAI_API_KEY."""

CLAUDE_MODEL = "anthropic/claude-sonnet-5-5"
"""An Anthropic model, as a framework that takes only Anthropic's defaults to (with ANTHROPIC_API_KEY)."""


def model_of(model: str | None, fallback: str = DEFAULT_MODEL) -> str:
    """The model, as ``provider/model-id``: ``model``, or else MODEL, or else the framework's default."""
    return model or os.environ.get("MODEL") or fallback


def provider_of(model: str) -> tuple[str, str]:
    """Split ``"provider/model-id"`` (or the frameworks' ``"provider:model-id"``) into its provider and model id.

    It splits at the first ``/``, else at the first ``:``, so model ids that
    contain a colon (``"openai/ft:gpt-4o:acme"``) keep it. A name with neither
    has no provider: ``("", model)``.
    """
    for separator in ("/", ":"):
        provider, found, model_id = model.partition(separator)
        if found:
            return provider, model_id
    return "", model


REFUSED = 424
"""
The error code of a message the model's provider refused (a missing or wrong
API key, an unknown model): Failed Dependency, as its HTTP status.
"""

CHECK_MODEL = "check the agent's model (its `model` option, or MODEL) and its provider's API key"
"""What to check when a model's provider refuses a request."""


class ModelError(TerminalError):
    """
    An agent's model that it cannot take: not ``provider/model-id``, another
    provider's, or one its framework does not know. Terminal (``REFUSED``),
    wherever it is raised: a retry takes the same model.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=REFUSED)


def check_model_name(framework: str, model: str | None) -> None:
    """Raise ``ModelError`` if ``model``, given explicitly to ``framework``'s agent, names no provider."""
    if model is not None and not re.fullmatch(r"[^/:\s]+[/:]\S+", model):
        raise ModelError(f'{framework}: the model "{model}" is not provider/model-id, such as {CLAUDE_MODEL}')


def model_id_of(model: str, provider: str) -> str:
    """The model id of ``model``, for a framework with only ``provider``'s models: ``provider/model-id``, or the id alone."""
    named, model_id = provider_of(model) if "/" in model or ":" in model else (provider, model)
    if named != provider or not model_id:
        from_env = " (from MODEL)" if os.environ.get("MODEL") == model else ""
        raise ModelError(f'the model "{model}"{from_env} is not {provider}\'s: this agent takes {provider}/<model-id>')
    return model_id


class MessageSerde(Serde[str]):
    """A chat message: the request's body as text, or the string it is as JSON (``"Hi"``)."""

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
"""What a cancelled turn answers, and each tool call it cut off: the model sees it in the conversation that goes on."""

UNANSWERED = "Unanswered: the model's provider refused the request."
"""
What a prompt answers that the model's provider refused for good: the prompt is
over, and the model sees it in the conversation that goes on, rather than
answer it with the next message.
"""


class ToolError(Exception):
    """A tool's error, which fails the attempt (Restate retries it): the error of tool ``tool``."""

    def __init__(self, tool: str, cause: BaseException) -> None:
        super().__init__(f"the tool {tool} failed: {cause}")
        self.tool: str = tool
        self.__cause__ = cause


_TOOL_ERRORS: ScopeKey[weakref.WeakSet[BaseException]] = ScopeKey("from_tool")
"""The errors the attempt's framework tools (not ``tool()``'s) failed with: see ``from_tool``."""


def from_tool(ctx: SessionContext, error: BaseException) -> None:
    """
    ``error`` is a tool's, in the attempt of ``ctx``: a framework's own tool
    that failed, whose error is never taken for the model's provider refusing
    (a tool that calls a model API may fail as one does).
    """
    _TOOL_ERRORS.of(ctx, weakref.WeakSet[BaseException]).add(error)


_FOR_GOOD = frozenset({"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"})
"""The model SDKs' errors (Anthropic's, OpenAI's) of a request that fails again however often it is made."""

_REFUSED_TEXT = re.compile(
    r"API key not found|no API key|Could not resolve authentication method|Provider is not configured"
    r'|"code":"(AUTH|MISSING_CREDENTIAL)"',
    re.IGNORECASE,
)


_FOR_GOOD_STATUS = frozenset({400, 401, 403, 404})
"""The HTTP statuses of a model request that fails again however often it is made (Pydantic AI's ``ModelHTTPError``'s)."""


def _refused(error: BaseException) -> bool:
    """Whether ``error`` is a model provider's refusal that a retry does not change: a missing key, an unknown model..."""
    if isinstance(error, ToolError):
        return False
    if type(error).__name__ in _FOR_GOOD:
        return True
    if type(error).__name__ == "ModelHTTPError" and getattr(error, "status_code", None) in _FOR_GOOD_STATUS:
        return True
    return bool(_REFUSED_TEXT.search(str(error)))


def refusal(error: BaseException) -> TerminalError | None:
    """``error`` as a terminal error (``REFUSED``), if a retry does not change it: a model the agent cannot take, or its provider's refusal."""
    if isinstance(error, ModelError):
        return error
    if isinstance(error, TerminalError) or not _refused(error):
        return None
    terminal = TerminalError(f"the model's provider refused the request: {CHECK_MODEL}. {error}", status_code=REFUSED)
    terminal.__cause__ = error
    return terminal


def refusal_of(ctx: SessionContext, error: BaseException) -> TerminalError | None:
    """``refusal``, but not of an error a framework's tool failed with in the attempt of ``ctx`` (``from_tool``)."""
    failed_tool = _TOOL_ERRORS.get(ctx)
    if failed_tool is not None and error in failed_tool:
        return None
    return refusal(error)


def stored_answer(answer: str) -> str:
    """A prompt's stored answer, as the prompt returns it: ``UNANSWERED`` fails as the prompt did then (``REFUSED``)."""
    if answer != UNANSWERED:
        return answer
    raise TerminalError(f"the model's provider refused the request: {CHECK_MODEL}", status_code=REFUSED)
