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
"""A Pydantic AI agent on a scripted model, against a real Restate server: each point of _agent.py's docstring."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import typing
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability, ReinjectSystemPrompt  # noqa: E402
from pydantic_ai.exceptions import UnexpectedModelBehavior  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import tool  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402
from restate.agents import pydantic_ai  # noqa: E402
from restate.agents._api import Messages  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As a model SDK names the error of a missing or wrong API key."""


asked: collections.Counter[str] = collections.Counter()
"""How often the model was asked, by the key word that ends each test's first prompt."""

garbled: collections.Counter[str] = collections.Counter()
"""How often the model was asked for a reply whose first response it could not read, by the prompt."""


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def _unanswered(messages: list[ModelMessage]) -> set[str]:
    """The ids of the tool calls in ``messages`` that have no result."""
    calls = {c.tool_call_id for m in messages if isinstance(m, ModelResponse) for c in m.tool_calls}
    answered = {
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart)
    }
    return calls - answered


def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The scripted model: it answers the last prompt from the conversation alone, as a retry would ask again."""
    if unanswered := _unanswered(messages):
        # As a model provider refuses a history with tool calls that have no results.
        raise RuntimeError(f"tool calls without results: {sorted(unanswered)}")
    text = _prompts(messages)[-1]
    asked[_prompts(messages)[0].split(" ")[-1]] += 1
    last = messages[-1]
    results = (
        [p for p in last.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
        if isinstance(last, ModelRequest)
        else []
    )
    word, _, rest = text.partition(" ")
    if word == "system":
        return ModelResponse(parts=[TextPart(f"system: {info.instructions}")])
    if word == "refuse":
        raise AuthenticationError("invalid x-api-key")
    if word == "garbled":
        garbled[text] += 1
        if garbled[text] == 1:
            # As OpenAI's model reports a response body that is not JSON.
            raise UnexpectedModelBehavior("Invalid response from openai chat completions endpoint, expected JSON data")
    if word == "sysweather" and not results:
        # Ask for the weather first, then report as sysprompt does: a turn of two model requests.
        return ModelResponse(
            parts=[ToolCallPart("get_weather", {"city": rest}, tool_call_id=f"call-{rest}-{len(messages)}")]
        )
    if word in ("sysprompt", "sysweather"):
        system = [
            p.content
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, SystemPromptPart)
        ]
        prompts = len(_prompts(messages))
        return ModelResponse(parts=[TextPart(f"system prompt: {', '.join(system)}; prompts: {prompts}")])
    if word in ("loop", "picky"):
        # A model that keeps calling a tool, never answering.
        name = "get_weather" if word == "loop" else "picky"
        return ModelResponse(parts=[ToolCallPart(name, {"city": rest}, tool_call_id=f"call-{rest}-{len(messages)}")])
    names = {"weather": "get_weather", "crash": "flaky", "wait": "blocking", "wrong": "get_weather"}
    names |= {"unknown": "nonexistent", "forbidden": "forbidden", "deferred": "refund_order"}
    if word in names and not results:
        args: dict[str, Any] = {"town": 3} if word == "wrong" else {"city": rest}
        return ModelResponse(parts=[ToolCallPart(names[word], args, tool_call_id=f"call-{rest}")])
    if results:
        result = results[-1]
        said = result.model_response() if isinstance(result, RetryPromptPart) else result.model_response_str()
        return ModelResponse(parts=[TextPart(f"tool said: {said}")])
    return ModelResponse(parts=[TextPart(f"echo: {text}")])


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


async def forbidden(city: str) -> str:
    """A Pydantic AI tool function of the agent's that fails for good."""
    raise restate.TerminalError(f"no access to {city}")


async def picky(city: str) -> str:
    """A Pydantic AI tool function that asks the model to call it again, every time."""
    raise ModelRetry(f"not like that, for {city}")


assistant = pydantic_ai.agent(
    "pai_assistant",
    model=FunctionModel(reply),
    system="Answer briefly.",
    tools=[get_weather, flaky, blocking, forbidden, picky],
)


@assistant.handler
async def twice(ctx: sessions.SessionContext, text: str) -> list[str]:
    """Two prompts of one invocation: each its own."""
    model = FunctionModel(reply)
    return [
        await pydantic_ai.prompt(ctx, text, model=model),
        await pydantic_ai.prompt(ctx, f"again {text}", model=model),
    ]


async def tools_of(ctx: sessions.SessionContext) -> list[Any]:
    """Each user's tools, as an async function of the context gives them."""
    return [get_weather]


per_user = pydantic_ai.agent(
    "pai_per_user", model=lambda ctx: FunctionModel(reply), system=lambda ctx: f"You help {ctx.key}.", tools=tools_of
)


@assistant.handler
async def own(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own, with its instructions and model, and this package's tools beside its own."""
    from pydantic_ai import Agent  # pylint: disable=import-outside-toplevel

    mine: Agent[None, str] = Agent(FunctionModel(reply), instructions="Mine.")
    return await pydantic_ai.prompt(ctx, text, agent=mine, tools=[get_weather])


@assistant.handler
async def own_system(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own with a system prompt, which Pydantic AI keeps in the message history."""
    mine: Agent[None, str] = Agent(FunctionModel(reply), system_prompt="Mine.")
    return await pydantic_ai.prompt(ctx, text, agent=mine, tools=[get_weather])


@assistant.handler
async def clash(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own whose tool has the name of one of this package's tools beside it: a setup error."""
    mine: Agent[None, str] = Agent(FunctionModel(reply), system_prompt="Mine.")

    @mine.tool_plain(name="get_weather")
    def weather(city: str) -> str:
        return f"rainy in {city}"

    return await pydantic_ai.prompt(ctx, text, agent=mine, tools=[get_weather])


@assistant.handler
async def approval(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own with a tool that needs an approval, whose run stops with the call for its caller."""
    mine: Any = Agent(FunctionModel(reply), output_type=[str, DeferredToolRequests])

    @mine.tool_plain(requires_approval=True)
    def refund_order(city: str) -> str:
        return f"refunded in {city}"

    return await pydantic_ai.prompt(ctx, text, agent=mine)


@assistant.handler
async def limited(ctx: sessions.SessionContext, text: str) -> str:
    """A prompt whose runs have a lower limit than Pydantic AI's default."""
    from pydantic_ai.usage import UsageLimits  # pylint: disable=import-outside-toplevel

    return await pydantic_ai.prompt(
        ctx, text, model=FunctionModel(reply), tools=[get_weather], usage_limits=UsageLimits(request_limit=5)
    )


spied: collections.Counter[str] = collections.Counter()


class Spy(AbstractCapability[Any]):
    """A capability of one's own, which counts the model requests it sees, by the prompt."""

    async def before_model_request(self, ctx: RunContext[Any], request_context: Any) -> Any:
        spied[str(ctx.prompt)] += 1
        return request_context


@assistant.handler
async def watched(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own, configured with a capability of its own."""
    mine: Any = Agent(FunctionModel(reply), capabilities=[Spy()])
    return await pydantic_ai.prompt(ctx, text, agent=mine)


@dataclass
class Shop:
    """The dependencies of an Agent of your own: who it serves."""

    customer: str


def shop_agent() -> Any:
    shop: Any = Agent(FunctionModel(reply), deps_type=Shop)

    @shop.instructions
    def prices(run: RunContext[Shop]) -> str:
        return f"Prices for {run.deps.customer}."

    return shop


shop = pydantic_ai.agent("pai_shop", shop_agent(), deps=lambda ctx: Shop(ctx.key))


class NotFoundError(Exception):
    """As an SDK, or a database's client, names a missing resource: the model's provider refusing, if a model's."""


lookups: collections.Counter[str] = collections.Counter()


@assistant.handler
async def profiled(ctx: sessions.SessionContext, text: str) -> str:
    """An Agent of your own whose instructions look up a profile, which is missing the first time."""
    mine: Any = Agent(FunctionModel(reply))  # Any: pyright matches no overload of the decorator to a RunContext[None]

    @mine.instructions
    def profile(run: RunContext[None]) -> str:
        asked_for = str(run.prompt)
        lookups[asked_for] += 1
        if lookups[asked_for] == 1:
            raise NotFoundError(f"no profile yet for {asked_for}")
        return "Be kind."

    return await pydantic_ai.prompt(ctx, text, agent=mine)


def weather_then_answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model that sees only the trimmed history: it calls the weather tool, then answers with its result."""
    last = messages[-1]
    returned = [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []
    if returned:
        return ModelResponse(parts=[TextPart(f"tool said: {returned[-1].model_response_str()}")])
    city = str(_prompts(messages)[-1]).split(" ")[-1]
    return ModelResponse(parts=[ToolCallPart("get_weather", {"city": city}, tool_call_id=f"call-{city}")])


def keep_last_two(messages: list[ModelMessage]) -> list[ModelMessage]:
    """A history processor that trims what the model sees (and what the run keeps) to the last two messages."""
    return messages[-2:]


def trimmed_agent() -> Any:
    from pydantic_ai import Agent  # pylint: disable=import-outside-toplevel
    from pydantic_ai.capabilities import ProcessHistory  # pylint: disable=import-outside-toplevel

    return Agent(FunctionModel(weather_then_answer), capabilities=[ProcessHistory(keep_last_two)])


trimmed = pydantic_ai.agent("pai_trimmed", trimmed_agent(), tools=[get_weather])


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([assistant, per_user, trimmed, shop]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/pai_assistant/{path}", **kwargs)


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


async def test_a_message_is_answered_with_the_instructions_and_kept_in_the_conversation(harness: HarnessEnvironment):
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
    assert "nonexistent" in unknown


async def test_a_tool_functions_terminal_error_is_its_answer(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"forbidden {k}")).json() == f"tool said: Error: no access to {k}"


async def test_a_refused_message_fails_with_424_and_the_next_is_answered(harness: HarnessEnvironment):
    k = key()
    refused = await chat(harness, k, f"refuse {k}")
    assert refused.status_code == 424, refused.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_a_stopped_turn_ends_cancelled_and_the_next_message_starts_afresh(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(
            f"{harness.ingress_url}/pai_assistant/{k}/chat/send", content=f"wait {k}", headers=TEXT
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


def test_a_model_whose_provider_cannot_run_is_refused_with_what_to_install(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(restate.TerminalError, match="OPENAI_API_KEY") as no_key:
        _agent._model("openai/gpt-6-sol")  # pyright: ignore[reportPrivateUsage]
    assert no_key.value.status_code == 424
    with pytest.raises(restate.TerminalError, match="Unknown provider"):
        _agent._model("nope/x")  # pyright: ignore[reportPrivateUsage]


def test_a_model_that_cannot_be_made_is_refused_whatever_its_error(monkeypatch: pytest.MonkeyPatch):
    """Making a model makes no request: before Pydantic AI 2.15, a provider's SDK raised its own error for a missing
    key, which is refused as any other."""
    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel

    class OpenAIError(Exception):
        """As the OpenAI SDK names the error of a client it cannot make."""

    def missing_key(name: str) -> Any:
        raise OpenAIError("Missing credentials. Please pass an `api_key`, or set the `OPENAI_API_KEY` variable.")

    monkeypatch.setattr(_agent, "infer_model", missing_key)
    with pytest.raises(restate.TerminalError, match="OPENAI_API_KEY") as refused:
        _agent._model("openai/gpt-6-sol")  # pyright: ignore[reportPrivateUsage]
    assert refused.value.status_code == 424


def test_an_agent_of_your_own_takes_no_system():
    from pydantic_ai import Agent  # pylint: disable=import-outside-toplevel

    with pytest.raises(TypeError, match="pass no system"):
        pydantic_ai.agent("mine", Agent(instructions="mine"), system="theirs")


def test_a_providers_http_refusal_is_terminal_and_a_rate_limit_is_retried():
    from pydantic_ai.exceptions import ModelHTTPError  # pylint: disable=import-outside-toplevel

    from restate.agents.internal import refusal  # pylint: disable=import-outside-toplevel

    unauthorized = refusal(ModelHTTPError(401, "gpt-6-sol", {"error": "invalid_api_key"}))
    assert unauthorized is not None and unauthorized.status_code == 424
    assert refusal(ModelHTTPError(429, "gpt-6-sol", {"error": "rate_limited"})) is None
    assert refusal(ModelHTTPError(500, "gpt-6-sol", None)) is None


async def test_an_agent_of_your_own_runs_with_its_instructions_and_this_packages_tools(harness: HarnessEnvironment):
    k = key()
    assert (await post(harness, f"{k}/own", json=f"system {k}")).json() == "system: Mine."
    assert (await post(harness, f"{k}/own", json=f"weather {k}")).json() == f"tool said: sunny in {k}"


async def test_with_a_history_processor_that_trims_each_tool_result_is_still_stored(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        base = f"{harness.ingress_url}/pai_trimmed/{k}"
        for city in ("Rome", "Oslo"):
            answered = await ingress.post(f"{base}/chat", content=f"weather {city}", headers=TEXT)
            assert answered.json() == f"tool said: sunny in {city}"
        page = typing.cast(Messages, (await ingress.post(f"{base}/messages", json={})).json())
    # The whole conversation is kept, each tool call with its output, though the model saw only the last messages.
    outputs = [
        part.get("output") for message in page["messages"] for part in message["parts"] if part["type"] == "tool"
    ]
    assert outputs == ["sunny in Rome", "sunny in Oslo"]


async def test_options_may_be_functions_of_the_context(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        url = f"{harness.ingress_url}/pai_per_user/{k}/chat"
        assert (await ingress.post(url, content=f"system {k}", headers=TEXT)).json() == f"system: You help {k}."
        assert (await ingress.post(url, content=f"weather {k}", headers=TEXT)).json() == f"tool said: sunny in {k}"


async def test_an_agent_of_your_own_keeps_its_system_prompt_in_each_turn(harness: HarnessEnvironment):
    """Pydantic AI adds it to a conversation's first request only: that request is stored, system prompt included."""
    k = key()
    for turn in (1, 2):
        answered = (await post(harness, f"{k}/own_system", json=f"sysprompt {k}")).json()
        assert answered == f"system prompt: Mine.; prompts: {turn}"
    # The conversation reads as before: the system prompt is not a message of its own.
    assert await messages(harness, k) == [
        ("user", f"sysprompt {k}"),
        ("assistant", "system prompt: Mine.; prompts: 1"),
        ("user", f"sysprompt {k}"),
        ("assistant", "system prompt: Mine.; prompts: 2"),
    ]


async def test_a_system_prompt_comes_first_after_turns_that_ended_before_any_model_request(
    harness: HarnessEnvironment,
):
    """A setup error, say, ends the first turn before Pydantic AI made its first request: the next turn's is the
    conversation's first, and the model sees the earlier prompt after the system prompt, as later turns do."""
    k = key()
    assert (await post(harness, f"{k}/clash", json=f"weather {k}")).status_code == 500
    # A turn of two model requests (a tool call, then the answer): the second sees the earlier turn once too.
    for turn, word in ((2, "sysweather"), (3, "sysprompt")):
        answered = (await post(harness, f"{k}/own_system", json=f"{word} {k}")).json()
        assert answered == f"system prompt: Mine.; prompts: {turn}"


async def test_an_agent_that_keeps_calling_tools_ends_the_turn_at_its_usage_limit(harness: HarnessEnvironment):
    """A retry would carry on with a fresh limit (50 model requests): the turn ends for good, after one attempt."""
    k = key()
    failed = await chat(harness, k, f"loop {k}")
    assert failed.status_code == 500 and "usage limit" in failed.text, failed.text
    assert asked[k] == 50
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_usage_limits_set_a_runs_limit(harness: HarnessEnvironment):
    k = key()
    failed = await post(harness, f"{k}/limited", json=f"loop {k}")
    assert failed.status_code == 500 and "request_limit of 5" in failed.text, failed.text
    assert asked[k] == 5


async def test_a_tool_whose_retries_run_out_ends_the_turn(harness: HarnessEnvironment):
    k = key()
    failed = await chat(harness, k, f"picky {k}")
    assert failed.status_code == 500 and "exceeded max retries" in failed.text, failed.text
    assert asked[k] == 2  # one attempt: the call, and the call again after its retry
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_a_response_that_the_model_could_not_read_is_retried(harness: HarnessEnvironment):
    """Not one of the run's own errors (retries that ran out, say): a model request's, which a retry asks again."""
    k = key()
    answered = await chat(harness, k, f"garbled {k}")
    assert answered.status_code == 200, answered.text
    assert answered.json() == f"echo: garbled {k}"
    assert garbled[f"garbled {k}"] == 2


async def test_a_run_stopped_for_a_deferred_tool_call_fails_and_the_call_is_closed(harness: HarnessEnvironment):
    """Nothing gives its approval: the turn fails rather than answer with the call, and the next message is answered,
    with the call closed (a provider refuses calls without results; the scripted model too)."""
    k = key()
    failed = await post(harness, f"{k}/approval", json=f"deferred {k}")
    assert failed.status_code == 500 and "wait_for_signal" in failed.text, failed.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"


async def test_an_agent_of_your_own_runs_with_its_own_capabilities(harness: HarnessEnvironment):
    k = key()
    assert (await post(harness, f"{k}/watched", json=f"hi {k}")).json() == f"echo: hi {k}"
    assert spied[f"hi {k}"] == 1


def kinds(own: Any, model: Any) -> list[type]:
    """The kinds of the capabilities that a run of ``own`` (an Agent of your own, or None) on ``model`` gets."""
    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel

    return [type(c) for c in _agent._capabilities(own, model)]  # pyright: ignore[reportPrivateUsage]


def test_the_built_in_agent_gets_the_compaction_of_its_models_provider():
    """OpenAI's for its Responses API, which openai/... models use, and none for a provider without one; and each
    run reinjects the system prompt, which the conversation does not store."""
    from pydantic_ai.models.openai import (  # pylint: disable=import-outside-toplevel
        OpenAIChatModel,
        OpenAICompaction,
        OpenAIResponsesModel,
    )
    from pydantic_ai.providers.openai import OpenAIProvider  # pylint: disable=import-outside-toplevel

    provider = OpenAIProvider(api_key="sk-test")
    assert kinds(None, OpenAIResponsesModel("gpt-6-sol", provider=provider)) == [OpenAICompaction, ReinjectSystemPrompt]
    assert kinds(None, OpenAIChatModel("gpt-6-sol", provider=provider)) == [ReinjectSystemPrompt]
    assert kinds(None, FunctionModel(reply)) == [ReinjectSystemPrompt]


def test_the_built_in_agent_on_anthropic_gets_its_compaction():
    pytest.importorskip("anthropic")
    from pydantic_ai.models.anthropic import (  # pylint: disable=import-outside-toplevel
        AnthropicCompaction,
        AnthropicModel,
    )
    from pydantic_ai.providers.anthropic import AnthropicProvider  # pylint: disable=import-outside-toplevel

    model = AnthropicModel("claude-sonnet-5-5", provider=AnthropicProvider(api_key="sk-test"))
    assert kinds(None, model) == [AnthropicCompaction, ReinjectSystemPrompt]


def test_an_agent_of_your_own_runs_as_configured_with_the_system_prompt_reinjected():
    """No compaction is added to it, whether it has its own or not; the system prompt is reinjected, unless the agent
    reinjects it itself (with replace_existing=True, say)."""
    from pydantic_ai.models.openai import OpenAICompaction, OpenAIResponsesModel  # pylint: disable=import-outside-toplevel
    from pydantic_ai.providers.openai import OpenAIProvider  # pylint: disable=import-outside-toplevel

    model = OpenAIResponsesModel("gpt-6-sol", provider=OpenAIProvider(api_key="sk-test"))
    plain: Any = Agent(model)
    assert kinds(plain, model) == [ReinjectSystemPrompt]
    compacting: Any = Agent(model, capabilities=[OpenAICompaction(token_threshold=100_000)])
    assert kinds(compacting, model) == [ReinjectSystemPrompt]
    reinjecting: Any = Agent(model, capabilities=[ReinjectSystemPrompt(replace_existing=True)])
    assert kinds(reinjecting, model) == []


async def test_an_agent_of_your_own_gets_its_deps(harness: HarnessEnvironment):
    k = key()
    async with httpx2.AsyncClient(timeout=60) as ingress:
        url = f"{harness.ingress_url}/pai_shop/{k}/chat"
        assert (await ingress.post(url, content=f"system {k}", headers=TEXT)).json() == f"system: Prices for {k}."


async def test_a_setup_error_of_pydantic_ai_ends_the_turn(harness: HarnessEnvironment):
    k = key()
    failed = await post(harness, f"{k}/clash", json=f"weather {k}")
    assert failed.status_code == 500 and "not set up right" in failed.text, failed.text


async def test_an_error_of_the_agents_own_code_is_retried_whatever_its_name(harness: HarnessEnvironment):
    """Only a model request's error can be its provider refusing: an instructions function's is retried."""
    k = key()
    answered = await post(harness, f"{k}/profiled", json=f"hi {k}")
    assert answered.status_code == 200, answered.text
    assert answered.json() == f"echo: hi {k}"
    assert lookups[f"hi {k}"] == 2


def test_the_openai_sdks_error_for_a_missing_key_is_a_refusal(monkeypatch: pytest.MonkeyPatch):
    """As the SDK words it now: a provider's SDK may raise it, before Pydantic AI 2.15 checks the key itself."""
    openai = pytest.importorskip("openai")
    from restate.agents.internal import refusal  # pylint: disable=import-outside-toplevel

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(openai.OpenAIError) as missing:
        openai.AsyncOpenAI()
    refused = refusal(missing.value)
    assert refused is not None and refused.status_code == 424


async def test_an_answer_stored_before_a_crash_is_not_asked_for_again(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    replies: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        replies.append("asked")
        return ModelResponse(parts=[TextPart(f"answer {len(replies)}")])

    stored = _agent._Restate.after_model_request  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def store_then_crash_once(self: Any, ctx: Any, **kwargs: Any) -> Any:
        response = await stored(self, ctx, **kwargs)
        if not response.tool_calls and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the answer")
        return response

    monkeypatch.setattr(_agent._Restate, "after_model_request", store_then_crash_once)  # pyright: ignore[reportPrivateUsage]
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await pydantic_ai.prompt(ctx, text, model=FunctionModel(answer), system="Answer briefly.")

    backend, invocation = Backend(), request()
    with pytest.raises(RuntimeError, match="crashed after storing"):
        await invoke(asker, "ask", Host(backend, service="asker"), invocation, "hi")
    # The retry (the same invocation) returns the answer that was stored, without asking the model again.
    assert await invoke(asker, "ask", Host(backend, service="asker"), invocation, "hi") == "answer 1"
    assert replies == ["asked"]


def test_a_tool_result_of_bytes_is_stored_as_pydantic_ai_stores_it():
    from pydantic_ai.messages import ModelMessagesTypeAdapter  # pylint: disable=import-outside-toplevel

    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel

    message = ModelRequest(parts=[ToolReturnPart("picture", b"\xff\x00\xfe", tool_call_id="call-1")])
    # Storing it does not fail (a failure would fail every attempt, and run the tool again each time) ...
    stored = _agent._MESSAGE.dump_json(message)  # pyright: ignore[reportPrivateUsage]
    read = _agent._MESSAGE.validate_json(stored)  # pyright: ignore[reportPrivateUsage]
    # ... and it reads back as pydantic-ai's own adapter reads it back.
    [expected] = ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json([message]))
    assert isinstance(read, ModelRequest) and isinstance(expected, ModelRequest)
    assert read.parts[0].content == expected.parts[0].content  # type: ignore[union-attr]
