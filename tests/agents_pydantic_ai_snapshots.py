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
"""A Pydantic AI agent's history from the latest snapshot, which the harness's compaction leaves, without a server.

Each test runs an agent's ``chat`` in this process, turn after turn, on a
scripted model whose replies are long enough to cross a small context window.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring,import-error

import json
import warnings
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.capabilities import ReinjectSystemPrompt  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    CompactionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.usage import RequestUsage  # noqa: E402
from pydantic_ai_harness.compaction import SlidingWindowCompaction, TieredCompaction  # noqa: E402
from sessions_database import Backend  # noqa: E402
from sessions_object import Host, request  # noqa: E402

from restate import sessions  # noqa: E402
from restate.agents import AgentObject, pydantic_ai  # noqa: E402
from restate.agents._conversation import SNAPSHOTS, STEPS, latest_snapshot  # noqa: E402
from restate.agents._turn import AGENT  # noqa: E402
from restate.agents.pydantic_ai import _agent  # noqa: E402
from restate.sessions.internal import invoke  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

ANSWERS = {"publish": b"null", "start": b"null", "close": b"null"}
"""What the signals object answers when a turn starts, publishes its progress and closes."""

SUMMARY = "SUMMARY of what was said"
LONG = "word " * 200
"""A long answer: 200 tokens, as the scripted model counts them."""


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]


class Script:
    """A scripted model: it calls ``lookup`` for each prompt, then answers at length; it summarizes when asked to.

    It keeps every request it was given, and the summaries it wrote.
    """

    def __init__(self, tools: bool = True) -> None:
        self.tools = tools
        self.requests: list[list[ModelMessage]] = []
        self.summaries = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        prompts = _prompts([last])
        if len(messages) == 1 and prompts and "summar" in prompts[0].lower() and not prompts[0].startswith("q"):
            # The harness's summary run: its own request, of the history to summarize.
            self.summaries += 1
            return ModelResponse(parts=[TextPart(SUMMARY)])
        self.requests.append(list(messages))
        if self.tools and prompts and prompts[-1].startswith("q"):
            word = prompts[-1].split(" ")[0]
            return ModelResponse(parts=[ToolCallPart("lookup", {"about": word}, tool_call_id=f"call-{word}")])
        return ModelResponse(parts=[TextPart(f"answer {LONG}")])

    def model(self) -> FunctionModel:
        return FunctionModel(self)


def lookup(about: str) -> str:
    """Look something up."""
    return f"found {about}: {'fact ' * 50}"


class Chat:
    """An agent's ``chat``, invoked in this process: each call is a turn, each with an invocation of its own."""

    def __init__(self, obj: AgentObject) -> None:
        self.obj = obj
        self.backend = Backend()
        self.name = obj.name
        self.sends: list[str] = []

        @obj.handler
        async def read(ctx: sessions.SessionContext, _: Any = None) -> Any:
            snapshot = await latest_snapshot(ctx.db, _agent.ITEMS)
            [[count]] = await ctx.db.values(f"SELECT COUNT(*) FROM {SNAPSHOTS}")
            steps = [str(item) for [item] in await ctx.db.values(f"SELECT item FROM {STEPS} ORDER BY n")]
            return {
                "snapshots": count,
                "items": [_agent.ITEMS.dump(i) for i in snapshot.items] if snapshot is not None else None,
                "steps": steps,
            }

    def host(self, view: bool = False) -> Host:
        host = Host(self.backend, service=self.name, answers=ANSWERS)
        return host.view() if view else host

    async def __call__(self, text: str, invocation: Any = None, compact: bool = True) -> Any:
        host = self.host()
        answer = await invoke(self.obj, "chat", host, invocation or request(), text)
        self.sends.extend(send.handler for send in host.sends)
        if compact and any(send.handler == "compact" for send in host.sends):
            # As Restate runs the send: before the key's next message.
            await invoke(self.obj, "compact", self.host(), request(), None)
        return answer

    async def read(self) -> dict[str, Any]:
        return await invoke(self.obj, "read", self.host(), request(), None)

    async def messages(self) -> list[tuple[str, str]]:
        page = await invoke(self.obj, "messages", self.host(view=True), request(), {"limit": 1000})
        return [
            (m["role"], " ".join(str(p.get("text", p.get("output", ""))) for p in m["parts"])) for m in page["messages"]
        ]


def agent(script: Script, name: str, *, context_window: int = 8000, **options: Any) -> Chat:
    return Chat(pydantic_ai.agent(name, model=script.model(), tools=[lookup], context_window=context_window, **options))


def _sent(messages: list[ModelMessage]) -> list[Any]:
    """Return ``messages`` as a model gets them, to compare: consecutive requests are one, without times."""
    return _agent._prints(messages)  # pyright: ignore[reportPrivateUsage]


def _has_summary(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(isinstance(p, CompactionPart) for p in message.parts)


async def until_snapshot(chat: Chat, limit: int = 60, compact: bool = True) -> int:
    """Ask until a snapshot is stored; return how many turns that took."""
    for n in range(limit):
        await chat(f"q{n} " + "talk " * 50, compact=compact)
        if (await chat.read())["snapshots"]:
            return n + 1
    raise AssertionError("no snapshot")


async def until_crash(chat: Chat) -> tuple[str, Any]:
    """Ask until an attempt crashes; return the prompt and the invocation, to retry."""
    for n in range(60):
        text, invocation = f"q{n} " + "talk " * 50, request()
        try:
            await chat(text, invocation=invocation, compact=False)
        except RuntimeError:
            return text, invocation
    raise AssertionError("no crash")


async def test_a_long_conversation_compacts_once_and_the_next_turn_starts_from_the_snapshot():
    script = Script()
    chat = agent(script, "pai_long")
    turns = await until_snapshot(chat, compact=False)
    stored = await chat.read()
    assert stored["snapshots"] == 1 and script.summaries == 1
    # The snapshot is the summary, then the tail it kept; no system prompt.
    snapshot = [_agent.ITEMS.load(i) for i in stored["items"]]
    assert any(_has_summary(m) for m in snapshot[:2])
    await chat(f"q{turns} next", compact=False)
    first = next(r for r in reversed(script.requests) if _prompts(r)[-1].startswith(f"q{turns} "))
    # The next turn's first request is the snapshot (the summary and the kept tail), then what came after it.
    assert _sent(first)[: len(_sent(snapshot))] == _sent(snapshot)
    assert script.summaries == 1


async def test_after_a_turn_over_the_target_the_agent_compacts_before_the_next_message():
    """Compacting off the answer's path: the next turn starts compacted, and asks for no summary itself."""
    script = Script(tools=False)
    chat = agent(script, "pai_after")
    turns = await until_snapshot(chat)
    assert chat.sends.count("compact") >= 1 and script.summaries == 1
    stored = await chat.read()
    snapshot = [_agent.ITEMS.load(i) for i in stored["items"]]
    await chat(f"q{turns} next")
    first = script.requests[-1]
    assert _prompts(first)[-1].startswith(f"q{turns} ")
    assert _sent(first)[: len(_sent(snapshot))] == _sent(snapshot)
    assert script.summaries == 1


def _starts(request: list[ModelMessage], earlier: list[ModelMessage]) -> bool:
    """Whether ``request`` starts with ``earlier``, as a provider's prompt cache compares them."""
    before = _sent(earlier)
    return _sent(request)[: len(before)] == before


async def test_between_compactions_each_turns_request_starts_with_the_previous_turns_request():
    """The cache oracle, across turns with tool calls, which the turn projection (a window of 1) would drop."""
    script = Script()
    chat = agent(script, "pai_oracle", context_window=1_000_000)
    for n in range(4):
        await chat(f"q{n} hello")
    assert (await chat.read())["snapshots"] == 0
    # Each request starts with the request before it: within a turn, and from one turn to the next.
    assert len(script.requests) == 8
    for earlier, later in zip(script.requests, script.requests[1:], strict=False):
        assert _starts(later, earlier)


class Cached(Script):
    """A scripted model that reports a provider's prompt cache: what of each request the previous one started with."""

    def __init__(self) -> None:
        super().__init__()
        self.previous: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = super().__call__(messages, info)
        dumped = [json.dumps(m) for m in _sent(messages)]
        common = 0
        while common < min(len(dumped), len(self.previous)) and dumped[common] == self.previous[common]:
            common += 1
        self.previous = dumped
        # 1,000 tokens a message, so that a miss is past WarnOnCacheBusts' thresholds.
        response.usage = RequestUsage(
            input_tokens=1000 * len(dumped),
            cache_read_tokens=1000 * common,
            cache_write_tokens=1000 * (len(dumped) - common),
        )
        response.provider_name = "cached"
        return response


async def test_warn_on_cache_busts_stays_quiet_across_turns():
    from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning  # pylint: disable=import-outside-toplevel
    from pydantic_ai_harness import WarnOnCacheBusts  # pylint: disable=import-outside-toplevel

    script = Cached()
    mine: Any = Agent(script.model(), tools=[lookup], capabilities=[WarnOnCacheBusts()])
    chat = Chat(pydantic_ai.agent("pai_cached", agent=mine, context_window=100_000_000))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for n in range(4):
            await chat(f"q{n} hello")
    assert not [w for w in caught if issubclass(w.category, CacheBustWarning)]


async def test_a_reply_asked_for_again_is_kept_for_the_next_turns_request():
    """Pydantic AI's feedback on an answer that did not validate is stored: the next turn's request starts with the
    request that answered it."""
    from pydantic import BaseModel  # pylint: disable=import-outside-toplevel

    class Plan(BaseModel):
        title: str

    requests: list[list[ModelMessage]] = []

    def planner(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(list(messages))
        last = messages[-1]
        retried = isinstance(last, ModelRequest) and any(
            isinstance(p, UserPromptPart) and "automated feedback" in str(p.content) for p in last.parts
        )
        return ModelResponse(parts=[TextPart('{"title": "t"}' if retried else "not json")])

    chat = Chat(pydantic_ai.agent("pai_feedback", model=FunctionModel(planner), output=Plan))
    assert await chat("plan one") == Plan(title="t")
    await chat("plan two")
    later = next(r for r in requests if _prompts(r)[-1] == "plan two")
    # What the model was given last in the first turn (the feedback), then its answer, start the second turn's.
    previous = requests[1]
    assert len(later) > len(previous)
    assert [m.kind for m in later[: len(previous)]] == [m.kind for m in previous]
    assert _prompts(later)[: len(_prompts(previous))] == _prompts(previous)


async def test_a_crash_after_the_snapshot_is_stored_does_not_summarize_again(monkeypatch: pytest.MonkeyPatch):
    script = Script()
    chat = agent(script, "pai_crash_after")
    snapshot = _agent._Snapshots.before_model_request  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def store_then_crash(self: Any, ctx: Any, request_context: Any) -> Any:
        before = self.conversation.snapshot
        returned = await snapshot(self, ctx, request_context)
        if self.conversation.snapshot is not before and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the snapshot")
        return returned

    monkeypatch.setattr(_agent._Snapshots, "before_model_request", store_then_crash)  # pyright: ignore[reportPrivateUsage]
    text, invocation = await until_crash(chat)
    assert crashed and script.summaries == 1
    # The retry (the same invocation) carries on from the snapshot: no summary again.
    await chat(text, invocation=invocation, compact=False)
    assert script.summaries == 1 and (await chat.read())["snapshots"] == 1


async def test_a_crash_before_the_snapshot_is_stored_summarizes_once_more(monkeypatch: pytest.MonkeyPatch):
    script = Script()
    chat = agent(script, "pai_crash_before")
    snapshot = _agent._Snapshots.before_model_request  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def crash_before_storing(self: Any, ctx: Any, request_context: Any) -> Any:
        if script.summaries and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed before storing the snapshot")
        return await snapshot(self, ctx, request_context)

    monkeypatch.setattr(_agent._Snapshots, "before_model_request", crash_before_storing)  # pyright: ignore[reportPrivateUsage]
    text, invocation = await until_crash(chat)
    assert crashed and script.summaries == 1 and (await chat.read())["snapshots"] == 0
    await chat(text, invocation=invocation, compact=False)
    assert script.summaries == 2 and (await chat.read())["snapshots"] == 1


async def test_the_views_show_the_original_steps_and_no_snapshot():
    script = Script()
    chat = agent(script, "pai_views")
    turns = await until_snapshot(chat)
    thread = await chat.messages()
    users = [text for role, text in thread if role == "user"]
    assert users == [f"q{n} " + "talk " * 50 for n in range(turns)]
    assert all(SUMMARY not in text for _, text in thread)
    assert ("assistant", f"found q0: {'fact ' * 50}") in thread


async def test_tool_results_inside_a_snapshot_are_not_stored_again():
    script = Script()
    chat = agent(script, "pai_known")
    turns = await until_snapshot(chat)
    for n in range(turns, turns + 2):
        await chat(f"q{n} more")
    steps = [json.loads(step) for step in (await chat.read())["steps"]]
    returned = [p["tool_call_id"] for s in steps for p in s.get("parts", []) if p.get("part_kind") == "tool-return"]
    assert sorted(returned) == sorted(f"call-q{n}" for n in range(turns + 2))


def kinds(own: Any) -> list[type]:
    return [type(c) for c in _agent._capabilities(own, None)]  # pyright: ignore[reportPrivateUsage]


def test_the_built_in_agent_and_an_agent_of_your_own_without_compaction_get_the_default():
    """Each reinjects the system prompt too, which the conversation does not store."""
    assert kinds(None) == [TieredCompaction, ReinjectSystemPrompt]
    plain: Any = Agent(FunctionModel(Script()))
    assert kinds(plain) == [TieredCompaction, ReinjectSystemPrompt]
    default = _agent.compaction(1234)
    assert default.target_fraction == 0.8 and default.context_window == 1234
    assert [type(tier).__name__ for tier in default.tiers] == [
        "ClampOversizedMessages",
        "ClearToolResults",
        "SummarizingCompaction",
    ]


def test_an_agent_of_your_own_with_a_compaction_runs_as_configured():
    from pydantic_ai.models.openai import OpenAICompaction  # pylint: disable=import-outside-toplevel

    sliding: Any = Agent(FunctionModel(Script()), capabilities=[SlidingWindowCompaction(max_messages=10)])
    assert kinds(sliding) == [ReinjectSystemPrompt]
    provider: Any = Agent(FunctionModel(Script()), capabilities=[OpenAICompaction(token_threshold=100_000)])
    assert kinds(provider) == [ReinjectSystemPrompt]
    reinjecting: Any = Agent(FunctionModel(Script()), capabilities=[ReinjectSystemPrompt(replace_existing=True)])
    assert kinds(reinjecting) == [TieredCompaction]
    # Its agent compacts in its runs only: no compaction between turns.
    obj = pydantic_ai.agent("pai_sliding", agent=sliding)
    settings = AGENT.of(obj)
    assert settings is not None and settings.snapshots and settings.compact_history is None


async def test_an_agent_of_your_own_without_compaction_compacts_its_history():
    script = Script()
    mine: Any = Agent(script.model(), tools=[lookup])
    chat = Chat(pydantic_ai.agent("pai_own_default", agent=mine, context_window=8000))
    await until_snapshot(chat)
    assert script.summaries == 1


async def test_no_system_prompt_is_stored_in_a_snapshot_and_each_turn_gets_it_as_it_is_now():
    """``ReinjectSystemPrompt`` gives each run the agent's system prompt, unless the history has one: a snapshot that
    kept the one it was given would keep it for good."""
    from pydantic_ai_harness.compaction import ClearToolResults  # pylint: disable=import-outside-toplevel

    script = Script()
    current = ["Be terse."]
    # A compaction of its own that keeps every message, the first request's system prompt with it.
    mine: Any = Agent(script.model(), tools=[lookup], capabilities=[ClearToolResults(max_tokens=600, keep_pairs=1)])

    @mine.system_prompt
    def system() -> str:
        return current[0]

    chat = Chat(pydantic_ai.agent("pai_system", agent=mine))
    turns = await until_snapshot(chat)
    stored = await chat.read()
    assert all('"system-prompt"' not in item for item in stored["items"])
    current[0] = "Be kind."
    await chat(f"q{turns} next")
    for requested in script.requests[-2:]:
        first = requested[0]
        assert isinstance(first, ModelRequest)
        assert [p.content for p in first.parts if isinstance(p, SystemPromptPart)] == ["Be kind."]


def test_a_rewrite_is_told_from_an_extension():
    """The run's first request adds the system prompt to the history it was given: no rewrite. A compaction is."""
    prompt = ModelRequest(parts=[UserPromptPart("q0")])
    history: list[ModelMessage] = [
        prompt,
        ModelResponse(parts=[TextPart("a0")]),
        ModelRequest(parts=[UserPromptPart("q1")]),
    ]
    prints = _sent(history)
    sent = [ModelRequest(parts=[SystemPromptPart("sys"), UserPromptPart("q0")]), *history[1:]]
    assert _sent(sent) == prints
    # A run merges consecutive requests: the history's shape changes, not what it says.
    split: list[ModelMessage] = [prompt, ModelRequest(parts=[CompactionPart(SUMMARY)]), *history[1:]]
    merged: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("q0"), CompactionPart(SUMMARY)]), *history[1:]]
    assert _sent(split) == _sent(merged)
    compacted = [ModelRequest(parts=[CompactionPart(SUMMARY)]), history[-1]]
    assert _sent(compacted) != prints[: len(_sent(compacted))]
    cleared = [*history[:1], ModelResponse(parts=[TextPart("")]), history[-1]]
    assert _sent(cleared) != prints


async def test_a_capability_loaded_in_a_turn_stays_loaded_in_later_turns_across_a_compaction():
    """The record of its load is in the history since the latest snapshot, or the harness records it again."""
    from pydantic_ai.capabilities import Toolset  # pylint: disable=import-outside-toplevel
    from pydantic_ai.toolsets import FunctionToolset  # pylint: disable=import-outside-toplevel

    loaded: list[bool] = []
    summaries: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts = _prompts([messages[-1]])
        if len(messages) == 1 and prompts and not prompts[0].startswith("q"):
            summaries.append(prompts[0])
            return ModelResponse(parts=[TextPart(SUMMARY)])
        if prompts and prompts[-1].startswith("q0"):
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "vault"}, tool_call_id="load-1")])
        if prompts:
            loaded.append(any(tool.name == "vault_secret" for tool in info.function_tools))
        return ModelResponse(parts=[TextPart(f"answer {LONG}")])

    def vault_secret() -> str:
        """Read the secret."""
        return "s3cret"

    vault = Toolset(FunctionToolset([vault_secret]), id="vault", description="The vault's secrets.", defer_loading=True)
    mine: Any = Agent(FunctionModel(model), capabilities=[vault])
    chat = Chat(pydantic_ai.agent("pai_vault", agent=mine, context_window=8000))
    turns = await until_snapshot(chat)
    assert summaries and turns > 5
    for n in range(turns, turns + 3):
        await chat(f"q{n} " + "talk " * 50)
    # Loaded in every request of the turns after the first: turn 5, and the turns after the compaction.
    assert len(loaded) >= turns + 2 and all(loaded)
    snapshot = "".join((await chat.read())["items"])
    assert "load-1" not in snapshot  # the model's load is summarized: the harness recorded it again


async def test_every_run_of_a_key_has_the_conversation_id_of_the_agent_and_key():
    """``ConversationSearch``'s scope, ``WarnOnCacheBusts``' marks: the same conversation in every turn, and in a
    prompt of a handler of one's own."""
    from pydantic_ai import RunContext  # pylint: disable=import-outside-toplevel
    from pydantic_ai.capabilities import AbstractCapability  # pylint: disable=import-outside-toplevel

    ids: list[str | None] = []

    class Ids(AbstractCapability[Any]):
        async def before_model_request(self, ctx: RunContext[Any], request_context: Any) -> Any:
            ids.append(ctx.conversation_id)
            return request_context

    mine: Any = Agent(FunctionModel(Script(tools=False)), capabilities=[Ids()])
    obj = pydantic_ai.agent("pai_ids", agent=mine)

    @obj.handler
    async def own(ctx: sessions.SessionContext, text: str) -> str:
        return await pydantic_ai.prompt(ctx, text, agent=mine)

    chat = Chat(obj)
    await chat("q0 hi")
    await chat("q1 hi")
    await invoke(obj, "own", chat.host(), request(), "q2 hi")
    assert ids == ["pai_ids/alice"] * 3
