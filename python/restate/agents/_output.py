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
"""An agent's answer as data, with ``output=`` (every framework's option): a Pydantic model.

The prompt (and so ``chat``) answers with a value of the model::

    planner = langgraph.agent("planner", output=Plan)  # chat answers with a Plan

A framework that can ask for the model natively does so, and its run returns
the answer's JSON: Pydantic AI (``NativeOutput``, or its ``PromptedOutput``
for a model without a JSON schema mode), the OpenAI Agents SDK
(``output_type``) and ADK (``output_schema``). LangGraph asks for it in the
system prompt (``asking``). Either way, the reply is read as JSON (in a code
fence, or after a sentence, too) and validated. A reply that does not
validate (an agent handed off to answered in text, say) is answered once with
what is wrong with it and the schema, so that the agent can answer again. If
that reply does not validate either, the prompt fails with a terminal error.

The conversation stores the replies as the agent wrote them, and each
attempt validates them again. Nothing is added to the user's message: only a
re-ask is a message of its own.
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


def _adapter(output: type[Any]) -> TypeAdapter[Any]:
    from pydantic import TypeAdapter  # pylint: disable=import-outside-toplevel

    return TypeAdapter(output)


def _schema(output: type[Any]) -> str:
    return json.dumps(_adapter(output).json_schema())


def asking(system: str | None, output: type[Any]) -> str:
    """Return ``system`` with the ask for ``output``: the final reply as JSON of its schema, and nothing else.

    For a framework that cannot ask for ``output`` natively.
    """
    asked = (
        "Your final reply (after any tool calls) is your answer, as JSON only, with no other text: "
        f"a value of this JSON schema.\n{_schema(output)}"
    )
    return f"{system}\n\n{asked}" if system else asked


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
    it and the schema, and the reply to that must validate.

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
        again = (
            f"Your reply is not the answer asked for: {answer.reason}\n"
            f"Reply again, with JSON only: a value of this JSON schema.\n{_schema(output)}"
        )
        answer = parsed(output, await prompt(again))
    if isinstance(answer, Wrong):
        raise TerminalError(f"the agent's reply is not the answer asked for: {answer.reason}")
    return answer
