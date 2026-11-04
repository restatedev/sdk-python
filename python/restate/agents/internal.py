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
"""Building blocks for writing a framework integration of ``restate.agents``.

An integration needs, beyond the public API:

- ``Conversation`` and ``answer_once``: the conversation every integration
  stores the same way, and the run of a prompt on it (answered once, an
  unfinished run finished first, a stopped, refused or failed run closed). An
  integration gives its ``Items`` (what its messages are: ``Call`` and
  ``Result``) and its own run, which appends each step as it goes.
- ``transcribing`` and ``Transcript``, to keep the conversation's log in step
  with a transcript of the integration's own (``answer_once`` does this).
- ``stop_signal_of``, to stop a turn when it is cancelled or its attempt ends.
- ``run_tool``, to run a ``restate.agents`` tool for a tool call.
- ``model_of``, ``provider_of`` and the related helpers, to resolve the model.
- ``refusal_of`` and ``from_tool``, to tell a model provider's refusal from a
  tool's error.
- ``companion_of`` and ``deliver``, to see what an agent is doing and to send
  it a message from another session object's handler.
"""

from restate.agents._common import (
    CANCELLED,
    CHECK_MODEL,
    DEFAULT_MODEL,
    MODEL_ENV,
    REFUSED,
    UNANSWERED,
    MessageSerde,
    ModelError,
    ToolError,
    configured,
    from_tool,
    model_id_of,
    model_of,
    next_prompt_id,
    provider_of,
    refusal,
    refusal_of,
    stored_answer,
)
from restate.agents._conversation import Call, Conversation, Entry, Items, Parsed, Result, answer_once, close
from restate.agents._signals import Companion, companion_of, deliver, signals
from restate.agents._tools import run_tool
from restate.agents._transcript import Transcript
from restate.agents._turn import (
    AttemptEnded,
    Read,
    Stop,
    publish,
    stop_signal_of,
    tool_call_ended,
    tool_call_started,
    transcribing,
)

__all__ = [
    "CANCELLED",
    "CHECK_MODEL",
    "DEFAULT_MODEL",
    "MODEL_ENV",
    "REFUSED",
    "UNANSWERED",
    "AttemptEnded",
    "Call",
    "Companion",
    "Conversation",
    "Entry",
    "Items",
    "MessageSerde",
    "ModelError",
    "Parsed",
    "Read",
    "Result",
    "Stop",
    "ToolError",
    "Transcript",
    "answer_once",
    "close",
    "companion_of",
    "configured",
    "deliver",
    "from_tool",
    "model_id_of",
    "model_of",
    "next_prompt_id",
    "provider_of",
    "publish",
    "refusal",
    "refusal_of",
    "run_tool",
    "signals",
    "stop_signal_of",
    "stored_answer",
    "tool_call_ended",
    "tool_call_started",
    "transcribing",
]
