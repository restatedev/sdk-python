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


def check_own(own: object, kind: str, **built_in: object) -> None:
    """Raise ``TypeError`` if a framework's own configured ``kind`` (``own``) is given with options of the built-in agent.

    An agent takes either the common options of the built-in agent
    (``model``, ``system``, ``tools``) or a ``kind`` of your own (an
    ``agent``, a ``graph``), configured as the framework does: its model,
    instructions and tools are its own.
    """
    if own is None:
        return
    # An empty list of tools is no tools.
    given = [
        name
        for name, value in built_in.items()
        if value is not None and not (isinstance(value, list | tuple) and not value)
    ]
    if given:
        raise TypeError(
            f"your own {kind} is configured with its own model, instructions and tools: "
            f"pass no {', '.join(given)} with it"
        )


def check_built_in(own: object, kind: str, **own_only: object) -> None:
    """Raise ``TypeError`` if options of a framework's own ``kind`` (its run's, such as ``deps``) are given without one.

    The built-in agent has none of its own, and would ignore them.
    """
    if own is not None:
        return
    given = [name for name, value in own_only.items() if value is not None]
    if given:
        options = f"{given[0]} is an option" if len(given) == 1 else f"{', '.join(given)} are options"
        raise TypeError(f"{options} of your own {kind}: pass {'it' if len(given) == 1 else 'them'} with {kind}=")


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


Window: typing.TypeAlias = "int | typing.Literal['all']"
"""How many of the latest answered turns the model sees whole (with their tool calls and results), or ``"all"``."""

WINDOW: Window = 1
"""The window by default: the previous turn's tool calls and results stay in view, for a follow-up."""

BUDGET = 32_000
"""How many tokens of the turns before the window the model sees, by default, counted as 4 characters each."""


Summarize: typing.TypeAlias = Callable[[SessionContext, str], Awaitable[str]]
"""How an agent summarizes earlier turns: from the request (``SUMMARIZE`` and the turns, as text) to the summary."""

CompactHistory: typing.TypeAlias = Callable[[SessionContext, list[typing.Any]], Awaitable[list[typing.Any] | None]]
"""How an agent that takes its history from snapshots compacts it between turns: from the history (the framework's
items) to the compacted history, which is stored as a snapshot, or ``None`` to leave it as it is."""

SUMMARIZE = (
    "Summarize the conversation below, for the assistant who carries it on: keep the facts, names, numbers, "
    "decisions, the user's preferences, and what is still open. Answer with the summary only.\n\n"
)
"""What a request to summarize earlier turns starts with, before the turns themselves."""


Check: typing.TypeAlias = Callable[[str, bool], "str | None"]
"""What a turn checks its answer with: ``None`` if the answer is what was asked for, else the message that asks
the model again, once, within the turn (``answer_once``'s ``check``). Its second argument, ``last``, says the model
was asked again already: a ``TerminalError`` the check raises then fails the turn, which is closed with it."""


def check_window(window: Window) -> Window:
    """Return ``window`` if it is one: a number of turns (0 or more), or ``"all"``."""
    given = typing.cast(object, window)
    if given != "all" and (isinstance(given, bool) or not isinstance(given, int) or given < 0):
        raise ValueError(f"window must be a number of turns (0 or more) or 'all', not {window!r}")
    return window


CANCELLATION = 409
"""The status code of Restate's cancellation error, which a stopped turn fails with."""


def is_cancellation(error: BaseException) -> bool:
    """Return whether ``error`` is Restate's cancellation error, which a stopped turn fails with too.

    Its status code alone does not tell: a call fails with 409 for other reasons, such as a workflow run a second
    time, which fail the turn rather than stop it.
    """
    return isinstance(error, TerminalError) and error.status_code == CANCELLATION and error.message == "cancelled"


CANCELLED = "Cancelled: the turn was stopped before it finished."
"""The answer of a cancelled turn, and the result of each tool call it cut off, which the model sees later in the conversation."""

ENDED = "Ended: the turn ended before it finished, and was not answered."
"""The answer of a turn whose invocation ended without answering it (killed, say), and the result of each tool call
it cut off. A newer message, or the companion noticing that the turn's invocation is gone, closes it with this."""

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


class ErrorSet:
    """A set of errors, by identity, that holds them for as long as it lives (an attempt, or a run).

    Not a ``WeakSet``: an error of a built-in type (``KeyError``,
    ``TypeError``, ...) cannot be weakly referenced, and adding one would
    raise in its place. Not a ``set``: an error type may define equality.
    """

    def __init__(self) -> None:
        self._errors: dict[int, BaseException] = {}

    def add(self, error: BaseException) -> None:
        """Add ``error``."""
        self._errors[id(error)] = error

    def __contains__(self, error: object) -> bool:
        return self._errors.get(id(error)) is error


_TOOL_ERRORS: ScopeKey[ErrorSet] = ScopeKey("from_tool")
"""The errors that framework tools (not those made by ``tool()``) raised in the attempt; see ``from_tool``."""


def from_tool(ctx: SessionContext, error: BaseException) -> None:
    """Record that a framework's own tool, or a graph of one's own, raised ``error`` in the attempt of ``ctx``.

    Such an error is never mistaken for a refusal by the model's provider,
    even if it looks like one (a tool that calls a model API can fail the same
    way).
    """
    _TOOL_ERRORS.of(ctx, ErrorSet).add(error)


_FOR_GOOD = frozenset({"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"})
"""The names of the model SDKs' errors (Anthropic's, OpenAI's) for requests that would fail again on retry."""

_REFUSED_TEXT = re.compile(
    r"API key not found|no API key|Could not resolve authentication method|Provider is not configured"
    r"|api_key client option must be set|Missing credentials\. Please pass an `api_key`"
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


def raised_by_tool(ctx: SessionContext, error: BaseException) -> bool:
    """Return whether ``error`` is one that ``from_tool`` recorded in the attempt of ``ctx``."""
    failed_tool = _TOOL_ERRORS.get(ctx)
    return failed_tool is not None and error in failed_tool


def refusal_of(ctx: SessionContext, error: BaseException) -> TerminalError | None:
    """Return ``refusal(error)``, except for errors that a framework's tool raised in the attempt (see ``from_tool``)."""
    if raised_by_tool(ctx, error):
        return None
    return refusal(error)


def stored_answer(answer: str, status: str | None = "answered") -> str:
    """Return a prompt's stored answer, or fail as the prompt did, by how its turn ended (``status``).

    A turn refused (``UNANSWERED``) fails with ``REFUSED``; one failed for
    good, with its error; one cancelled, with the cancellation; one that
    another invocation ended, with its note. So a retry after a crash right
    after the turn was closed fails as the prompt did, instead of answering
    with the note. A failed turn's error has the default status code: the
    turn keeps its message, not its code.
    """
    if status == "refused" or answer == UNANSWERED:
        raise TerminalError(f"the model's provider refused the request: {CHECK_MODEL}", status_code=REFUSED)
    if status == "cancelled":
        raise TerminalError("cancelled", status_code=CANCELLATION)
    if status == "failed":
        raise TerminalError(answer.removeprefix("Error: "))
    if status == "ended":
        raise TerminalError(answer)
    return answer
