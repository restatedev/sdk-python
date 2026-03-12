#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
Restate-aware event summarizer for ADK compaction.

Wraps the LlmEventSummarizer so the summarization call is journaled
through ctx.run, making it deterministic on replay.
"""

import restate

from datetime import timedelta
from typing import Optional

from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm

from restate.extensions import current_context


class RestateEventSummarizer(BaseEventsSummarizer):
    """Event summarizer that journals the LLM call through Restate ctx.run.

    Creates an LlmEventSummarizer internally and wraps its call in ctx.run_typed
    so the result is persisted in the Restate journal and replayed deterministically.
    """

    def __init__(
        self,
        llm: BaseLlm,
        prompt_template: Optional[str] = None,
        max_retries: int = 10,
    ):
        self._inner = LlmEventSummarizer(llm=llm, prompt_template=prompt_template)
        self._max_retries = max_retries

    async def maybe_summarize_events(
        self, *, events: list[Event]
    ) -> Optional[Event]:
        if not events:
            return None

        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "No Restate context found. "
                "RestateEventSummarizer must be used from within a Restate handler."
            )

        inner = self._inner

        async def call_inner() -> Optional[Event]:
            return await inner.maybe_summarize_events(events=events)

        return await ctx.run_typed(
            "compaction LLM call",
            call_inner,
            restate.RunOptions(
                max_attempts=self._max_retries,
                initial_retry_interval=timedelta(seconds=1),
            ),
        )