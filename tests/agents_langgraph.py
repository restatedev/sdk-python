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
"""A LangGraph agent on a scripted chat model, against a real Restate server: each point of _agent.py's docstring."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import sys
import types
import typing
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx2
import pytest

pytest.importorskip("langgraph")

from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool as langchain_tool  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import tool  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402
from restate.agents import langgraph  # noqa: E402
from restate.agents._api import Messages  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As a model SDK names the error of a missing or wrong API key."""


asked: collections.Counter[str] = collections.Counter()
"""How often the model was asked, by the first word of the conversation's first message (each test's own)."""


def reply(messages: Sequence[BaseMessage]) -> AIMessage:
    """
    The scripted model: it answers the last prompt from the conversation alone,
    so that a retried attempt that asks again gets the same reply.
    """
    prompt = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    text = str(prompt.content)
    after = messages[list(messages).index(prompt) + 1 :]
    results = [m for m in after if isinstance(m, ToolMessage)]
    word, _, rest = text.partition(" ")
    if word == "system":
        system = next((m for m in messages if isinstance(m, SystemMessage)), None)
        return AIMessage(f"system: {system.content if system else None}")
    if word == "refuse":
        raise AuthenticationError("invalid x-api-key")
    if word == "loop":
        # A model that keeps calling tools, never answering.
        return AIMessage(
            "", tool_calls=[{"id": f"call-{rest}-{len(results)}", "name": "get_weather", "args": {"city": rest}}]
        )
    if word in ("weather", "crash", "wait", "wrong", "unknown", "forbidden") and not results:
        name = {
            "weather": "get_weather",
            "crash": "flaky",
            "wait": "blocking",
            "wrong": "get_weather",
            "forbidden": "forbidden",
        }.get(word, "nonexistent")
        args: dict[str, Any] = {"town": 3} if word == "wrong" else {"city": rest}
        return AIMessage("", tool_calls=[{"id": f"call-{rest}", "name": name, "args": args}])
    if results:
        return AIMessage(f"tool said: {results[-1].content}")
    return AIMessage(f"echo: {text}")


class Scripted(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        first = next(m for m in messages if isinstance(m, HumanMessage))
        asked[str(first.content).split(" ")[-1]] += 1
        return ChatResult(generations=[ChatGeneration(message=reply(messages))])


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"sunny in {city}"


crashes: collections.Counter[str] = collections.Counter()
calls_seen: list[str] = []


@tool
async def flaky(city: str, call_id: str) -> str:
    """Fails its first run, as a crash would."""
    calls_seen.append(call_id)
    crashes[city] += 1
    if crashes[city] == 1:
        raise RuntimeError("the process crashed")
    return f"recovered in {city}"


gate = asyncio.Event()
blocked: set[str] = set()


@tool
async def blocking(city: str) -> str:
    """Waits until it is stopped."""
    blocked.add(city)
    await gate.wait()
    return "never"


@langchain_tool
async def forbidden(city: str) -> str:
    """A LangChain tool of the agent's that fails for good."""
    raise restate.TerminalError(f"no access to {city}")


assistant = langgraph.agent(
    "lg_assistant",
    model=Scripted(),
    system="Answer briefly.",
    tools=[get_weather, flaky, blocking, forbidden],
)


@assistant.handler
async def twice(ctx: sessions.SessionContext, text: str) -> list[str]:
    """Two prompts of one invocation: each its own."""
    model = Scripted()
    return [await langgraph.prompt(ctx, text, model=model), await langgraph.prompt(ctx, f"again {text}", model=model)]


async def tools_of(ctx: sessions.SessionContext) -> list[Any]:
    """Each user's tools, as an async function of the context gives them."""
    return [get_weather]


per_user = langgraph.agent(
    "lg_per_user", model=lambda ctx: Scripted(), system=lambda ctx: f"You help {ctx.key}.", tools=tools_of
)


summarized_prompts: list[list[str]] = []
"""The prompts of each request of the agent with a small budget."""


class Remembering(Scripted):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        summarized_prompts.append([str(m.content) for m in messages if isinstance(m, HumanMessage)])
        return super()._generate(messages, stop, run_manager, **kwargs)


summarizing = langgraph.agent("lg_summarizing", model=Remembering(), budget=10)
"""An agent whose model sees 40 characters of the turns before the window: it summarizes the earlier ones."""


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([assistant, per_user, summarizing]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/lg_assistant/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, text: str) -> httpx2.Response:
    return await post(harness, f"{key}/chat", content=text, headers=TEXT)


async def messages(harness: HarnessEnvironment, key: str) -> list[tuple[str, str]]:
    page = typing.cast(Messages, (await post(harness, f"{key}/messages", json={})).json())
    return [
        (message["role"], " ".join(str(part.get("text", part.get("output", ""))) for part in message["parts"]))
        for message in page["messages"]
    ]


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never")


def key() -> str:
    return uuid.uuid4().hex


async def test_a_message_is_answered_with_the_system_prompt_and_kept_in_the_thread(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"
    assert (await chat(harness, k, f"system {k}")).json() == "system: Answer briefly."
    assert await messages(harness, k) == [
        ("user", f"hi {k}"),
        ("assistant", f"echo: hi {k}"),
        ("user", f"system {k}"),
        ("assistant", "system: Answer briefly."),
    ]


async def test_a_tool_call_is_made_and_its_result_answered(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"weather {k}")).json() == f"tool said: sunny in {k}"
    assert ("assistant", f"sunny in {k}") in await messages(harness, k)


async def test_a_crash_in_a_tool_resumes_it_with_the_same_call_id_without_asking_the_model_again(
    harness: HarnessEnvironment,
):
    k = key()
    assert (await chat(harness, k, f"crash {k}")).json() == f"tool said: recovered in {k}"
    assert crashes[k] == 2
    assert calls_seen.count(f"call-{k}") == 2
    # Asked for the tool call once, then for the answer once: the retry resumed at the tools.
    assert asked[k] == 2


async def test_arguments_that_do_not_fit_and_an_unknown_tool_are_the_models_to_fix(harness: HarnessEnvironment):
    k = key()
    wrong = (await chat(harness, k, f"wrong {k}")).json()
    assert wrong.startswith("tool said: Error: the arguments do not fit")
    unknown = (await chat(harness, key(), f"unknown {k}")).json()
    assert "nonexistent is not a valid tool" in unknown


async def test_a_refused_message_fails_with_424_and_the_next_is_answered(harness: HarnessEnvironment):
    k = key()
    refused = await chat(harness, k, f"refuse {k}")
    assert refused.status_code == 424, refused.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_a_stopped_turn_ends_cancelled_and_the_next_message_starts_afresh(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(
            f"{harness.ingress_url}/lg_assistant/{k}/chat/send", content=f"wait {k}", headers=TEXT
        )
        invocation = sent.json()["invocationId"]

        async def in_the_tool() -> bool:
            return k in blocked

        await until(in_the_tool)
        await post(harness, f"{k}/stop", json=None)
        ended = await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation}/attach")
    assert ended.status_code == 409, ended.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"
    thread = await messages(harness, k)
    assert ("assistant", CANCELLED) in thread
    assert thread[-1] == ("assistant", f"echo: hi {k}")


async def test_each_prompt_of_an_invocation_is_its_own(harness: HarnessEnvironment):
    k = key()
    answered = await post(harness, f"{k}/twice", json=f"hi {k}")
    assert answered.json() == [f"echo: hi {k}", f"echo: again hi {k}"]


def test_an_agent_refuses_a_model_it_cannot_name():
    with pytest.raises(restate.TerminalError, match="neither openai/<model-id> nor anthropic/<model-id>"):
        langgraph.agent("bad", model="mistral/large")
    with pytest.raises(restate.TerminalError, match="neither"):
        langgraph.agent("bad2", model="gpt")


def test_a_named_model_whose_provider_package_is_missing_says_which_to_install(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.langgraph import _agent  # pylint: disable=import-outside-toplevel

    monkeypatch.setitem(sys.modules, "langchain_anthropic", None)
    with pytest.raises(restate.TerminalError, match="need langchain-anthropic: install it beside") as refused:
        _agent._chat_model("anthropic/claude-sonnet-5-5")  # pyright: ignore[reportPrivateUsage]
    assert refused.value.status_code == 424


def test_an_openai_model_is_on_the_responses_api_where_reasoning_models_take_tools(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.langgraph import _agent  # pylint: disable=import-outside-toplevel

    made: list[dict[str, Any]] = []

    class ChatOpenAI:
        def __init__(self, **options: Any) -> None:
            made.append(options)

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=ChatOpenAI))
    _agent._chat_model("openai/gpt-6-sol")  # pyright: ignore[reportPrivateUsage]
    # On Chat Completions, OpenAI refuses function tools to a reasoning model ("use /v1/responses").
    assert made == [{"model": "gpt-6-sol", "use_responses_api": True}]


async def test_a_langchain_tools_terminal_error_ends_the_turn_and_the_next_message_is_answered(
    harness: HarnessEnvironment,
):
    k = key()
    failed = await chat(harness, k, f"forbidden {k}")
    assert failed.status_code == 500 and f"no access to {k}" in failed.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"
    assert ("assistant", f"Error: no access to {k}") in await messages(harness, k)


async def test_a_model_that_keeps_calling_tools_ends_the_turn_at_the_recursion_limit(
    harness: HarnessEnvironment, monkeypatch: pytest.MonkeyPatch
):
    """A retry would carry on with a fresh limit, and ask the model again: the turn ends for good after one attempt."""
    # LangGraph's default limit is about ten thousand steps; the test lowers it, where LangGraph reads it.
    monkeypatch.setattr("langgraph._internal._config.DEFAULT_RECURSION_LIMIT", 25)
    k = key()
    failed = await chat(harness, k, f"loop {k}")
    assert failed.status_code == 500 and "recursion limit" in failed.text, failed.text
    assert asked[k] == 13  # one attempt's 25 steps: the model, then its tools, and so on
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_options_may_be_functions_of_the_context(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        url = f"{harness.ingress_url}/lg_per_user/{k}/chat"
        assert (await ingress.post(url, content=f"system {k}", headers=TEXT)).json() == f"system: You help {k}."
        assert (await ingress.post(url, content=f"weather {k}", headers=TEXT)).json() == f"tool said: sunny in {k}"


# --- In process -----------------------------------------------------------------------------------


def asking(**options: Any) -> Callable[..., Awaitable[Any]]:
    """Return how to ask, in process, an object whose handler answers with ``langgraph.prompt`` (in one backend)."""
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    asker = sessions.SessionObject("asker")

    @asker.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await langgraph.prompt(ctx, text, **options)

    backend = Backend()

    async def asked(text: str) -> Any:
        return await invoke(asker, "ask", Host(backend, service="asker", answers={"publish": b"null"}), request(), text)

    return asked


async def test_the_built_in_agent_sees_the_latest_messages_that_fit_in_its_context_window():
    if "profile" not in BaseChatModel.model_fields:
        pytest.skip("chat models have a profile since langchain-core 1.1")
    seen: list[list[BaseMessage]] = []

    class Seeing(Scripted):
        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: CallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            seen.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    # A model whose profile has its context window, as LangChain's integrations give the models they know.
    ask = asking(model=Seeing(profile={"max_input_tokens": 80}))
    for n in range(1, 11):
        assert await ask(f"hi {n}") == f"echo: hi {n}"
    latest = seen[-1]
    # The conversation has 19 messages before the last reply: the model saw the latest of them, from a prompt.
    assert 1 < len(latest) < 19
    assert isinstance(latest[0], HumanMessage) and latest[-1].content == "hi 10"
    assert all(message.content != "hi 1" for message in latest)

    # A model without one sees all of the conversation.
    seen.clear()
    ask = asking(model=Seeing())
    for n in range(1, 11):
        assert await ask(f"hi {n}") == f"echo: hi {n}"
    assert len(seen[-1]) == 19


def test_a_prompt_longer_than_the_window_is_sent_whole():
    from restate.agents.langgraph import _agent  # pylint: disable=import-outside-toplevel

    messages: list[BaseMessage] = [HumanMessage("hi"), AIMessage("hello"), HumanMessage("x" * 1000)]
    latest = _agent._latest(messages, 10)  # pyright: ignore[reportPrivateUsage]
    assert latest == messages[-1:]
    assert _agent._latest(messages, None) == messages  # pyright: ignore[reportPrivateUsage]


async def test_turns_that_outgrow_the_budget_are_summarized_once_answered(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        for word in ("one", "two", "three", "four", "five"):
            answer = await ingress.post(
                f"{harness.ingress_url}/lg_summarizing/{k}/chat", content=f"hi {word}", headers=TEXT
            )
            assert answer.json() == f"echo: hi {word}"
    summaries = [prompts[-1] for prompts in summarized_prompts if prompts[-1].startswith("Summarize")]
    assert len(summaries) == 1 and "User: hi one" in summaries[0] and "hi four" not in summaries[0]
    latest = summarized_prompts[-1]
    assert latest[0].startswith("A summary of our conversation so far:") and latest[1:] == ["hi four", "hi five"]
