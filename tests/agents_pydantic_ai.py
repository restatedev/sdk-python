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
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
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


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The scripted model: it answers the last prompt from the conversation alone, as a retry would ask again."""
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
    names = {"weather": "get_weather", "crash": "flaky", "wait": "blocking", "wrong": "get_weather"}
    names |= {"unknown": "nonexistent", "forbidden": "forbidden"}
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


assistant = pydantic_ai.agent(
    "pai_assistant",
    model=FunctionModel(reply),
    system="Answer briefly.",
    tools=[get_weather, flaky, blocking, forbidden],
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


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        sessions.app(assistant, per_user), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/pai_assistant/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, text: str) -> httpx.Response:
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
    async with httpx.AsyncClient(timeout=60) as ingress:
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


async def test_options_may_be_functions_of_the_context(harness: HarnessEnvironment):
    k = key()
    async with httpx.AsyncClient(timeout=60) as ingress:
        url = f"{harness.ingress_url}/pai_per_user/{k}/chat"
        assert (await ingress.post(url, content=f"system {k}", headers=TEXT)).json() == f"system: You help {k}."
        assert (await ingress.post(url, content=f"weather {k}", headers=TEXT)).json() == f"tool said: sunny in {k}"


async def test_an_answer_stored_before_a_crash_is_not_asked_for_again(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.pydantic_ai import _agent  # pylint: disable=import-outside-toplevel
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    replies: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        replies.append("asked")
        return ModelResponse(parts=[TextPart(f"answer {len(replies)}")])

    save = _agent._Conversation.save  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def save_then_crash_once(self: Any, messages: Any) -> None:
        await save(self, messages)
        last = messages[-1]
        if isinstance(last, ModelResponse) and not (last.metadata or {}) and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the answer")

    monkeypatch.setattr(_agent._Conversation, "save", save_then_crash_once)  # pyright: ignore[reportPrivateUsage]
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
