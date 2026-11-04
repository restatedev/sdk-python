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
What a framework's integration needs beside ``restate.agents``: its turn's
transcript kept in the log (``transcribing``, ``Transcript``), its stop
(``stop_signal_of``), its tool calls (``run_tool``), its model (``model_of``
and the rest), and a model provider's refusal (``refusal_of``, ``from_tool``).
"""

from restate.agents._common import (
    CANCELLED,
    CHECK_MODEL,
    CLAUDE_MODEL,
    DEFAULT_MODEL,
    MODEL_ENV,
    REFUSED,
    UNANSWERED,
    MessageSerde,
    ModelError,
    ToolError,
    check_model_name,
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
    "CLAUDE_MODEL",
    "DEFAULT_MODEL",
    "MODEL_ENV",
    "REFUSED",
    "UNANSWERED",
    "AttemptEnded",
    "Companion",
    "MessageSerde",
    "ModelError",
    "Read",
    "Stop",
    "ToolError",
    "Transcript",
    "check_model_name",
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
