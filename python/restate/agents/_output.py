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
"""An agent's answer as data, with ``output=`` (every framework's option).

``output`` is a Pydantic model. The agent's reply is its answer, as JSON. The system prompt
asks for it, with the type's JSON schema, and each prompt asks again at its
end. The reply is then validated, and the prompt (and so ``chat``) answers
with the value::

    planner = langgraph.agent("planner", output=Plan)  # chat answers with a Plan

The JSON may be in a code fence, or come after a sentence. A reply that does
not validate is answered once with what is wrong with it, so that the agent
can answer again. If that reply does not validate either, the prompt fails
with a terminal error. A retried attempt resumes the conversation, so it
reads the same replies.

The conversation stores the replies as the agent wrote them, and each
attempt validates them again.
"""

from __future__ import annotations

import json
import re
import typing
from collections.abc import Awaitable, Callable
from typing import Any

from restate.exceptions import TerminalError

if typing.TYPE_CHECKING:
    from pydantic import TypeAdapter

O = typing.TypeVar("O")

REMINDER = "\n\nWhen you are done, reply with your answer as JSON only, of the schema you were given: no other text."
"""What each prompt ends with, with ``output``.

It repeats the system prompt's ask where the agent reads it last: after a
long run of tool calls, an agent may drift from its system prompt and reply
with a summary in prose.
"""


def _adapter(output: type[Any]) -> TypeAdapter[Any]:
    from pydantic import TypeAdapter  # pylint: disable=import-outside-toplevel

    return TypeAdapter(output)


def instructions(output: type[Any]) -> str:
    """Return what the system prompt asks for with ``output``: JSON of its schema, and nothing else."""
    schema = json.dumps(_adapter(output).json_schema())
    return f"Your final reply is your answer, as JSON only (no other text): a value of this JSON schema.\n{schema}"


_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)


def _json_of(reply: str) -> object:
    """Return the JSON that ``reply`` holds, or ``...`` if it holds none.

    That is all of ``reply``, else what a code fence holds, else the object
    from the first ``{`` to the last ``}``: a model that ends a long run often
    says a sentence first ("Done. Here is the summary: {...}").
    """
    fenced = _FENCED.search(reply)
    candidates = [reply, fenced.group(1) if fenced else "", reply[reply.find("{") : reply.rfind("}") + 1]]
    for text in candidates:
        if text.strip():
            try:
                return json.loads(text)
            except ValueError:
                pass
    return ...


def _excerpt(reply: str) -> str:
    """Return the start of ``reply``, for an error."""
    return reply if len(reply) <= 300 else f"{reply[:300]}…"


class Wrong(typing.NamedTuple):
    """What is wrong with a reply that is not the answer asked for."""

    reason: str


def parsed(output: type[O], reply: str) -> O | Wrong:
    """Return ``reply`` as a value of ``output``, or else what is wrong with it."""
    from pydantic import ValidationError  # pylint: disable=import-outside-toplevel

    if not reply.strip():
        return Wrong("it is empty")
    found = _json_of(reply)
    if found is ...:
        return Wrong(f"it is not JSON: {json.dumps(_excerpt(reply))}")
    try:
        return typing.cast(O, _adapter(output).validate_python(found))
    except ValidationError as error:
        return Wrong(
            "; ".join(
                f"{'.'.join(str(part) for part in e['loc']) or 'the answer'}: {e['msg']}"
                for e in error.errors(include_url=False)
            )
        )


async def answer_with(output: type[O], text: str, prompt: Callable[[str], Awaitable[str]]) -> O:
    """Answer ``text`` with a value of ``output``: the reply of ``prompt``, validated.

    A reply that does not validate is answered once with what is wrong with
    it, and the reply to that must validate.

    Args:
        output: The type of the answer.
        text: The prompt.
        prompt: Asks the agent a prompt, exactly once, and returns its reply,
            such as a framework's ``prompt``.

    Returns:
        The answer.

    Raises:
        TerminalError: If the second reply does not validate either.
    """
    answer = parsed(output, await prompt(text))
    if isinstance(answer, Wrong):
        again = f"Your reply is not the answer asked for: {answer.reason}\nReply again, with the JSON only."
        answer = parsed(output, await prompt(again))
    if isinstance(answer, Wrong):
        raise TerminalError(f"the agent's reply is not the answer asked for: {answer.reason}")
    return answer


async def with_output(
    output: type[O],
    text: str,
    system: str | None,
    prompt: Callable[[str, str | None], Awaitable[str]],
    *,
    own: bool = False,
) -> O:
    """Answer ``text`` with a value of ``output``, asking for it with ``prompt(text, system)``.

    The system prompt asks for the JSON, with its schema, and ``text`` ends
    with ``REMINDER``. An agent of your own (``own``) keeps its instructions,
    so ``text`` ends with the full ask instead.

    Args:
        output: The type of the answer.
        text: The prompt.
        system: The system prompt, or ``None``.
        prompt: A framework's prompt, which takes the text and the system prompt.
        own: Whether the agent is your own, with its own instructions.

    Returns:
        The answer.
    """
    asked = instructions(output)
    if own:
        return await answer_with(output, f"{text}\n\n{asked}", lambda t: prompt(t, system))
    full = f"{system}\n\n{asked}" if system else asked
    return await answer_with(output, text + REMINDER, lambda t: prompt(t, full))
