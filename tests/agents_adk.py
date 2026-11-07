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
"""A Google ADK agent on a scripted model, against a real Restate server: each point of _agent.py's docstring."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import re
import typing
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import httpx2
import pytest

pytest.importorskip("google.adk")

from google.adk.agents import LlmAgent, RunConfig  # noqa: E402
from google.adk.models import BaseLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

import restate  # noqa: E402
from support import RESTATE_IMAGE  # noqa: E402  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import adk, tool  # noqa: E402
from restate.agents._api import Messages  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As a model SDK names the error of a missing or wrong API key."""


asked: collections.Counter[str] = collections.Counter()
"""How often the model was asked, by the key word that ends each test's first prompt."""


def _prompts(contents: list[types.Content]) -> list[str]:
    return [
        part.text
        for content in contents
        if content.role == "user"
        for part in content.parts or []
        # Not what ADK shows an agent of other agents' events, as context.
        if part.text and not part.text.startswith("For context:") and not re.match(r"\[\w+\] said:", part.text)
    ]


def reply(system: str, contents: list[types.Content]) -> list[types.Part]:
    """The scripted model: it answers the last prompt from the conversation alone, as a retry would ask again."""
    prompt = _prompts(contents)[-1]
    asked[_prompts(contents)[0].split(" ")[-1]] += 1
    last = contents[-1].parts or []
    responses = [part.function_response for part in last if part.function_response]
    word, _, rest = prompt.partition(" ")
    if word == "system":
        return [types.Part(text=f"system: {system}")]
    if word == "refuse":
        raise AuthenticationError("invalid x-api-key")
    if word == "handoff" and not responses and system != "You handle refunds.":
        call = types.FunctionCall(id=f"call-{rest}", name="transfer_to_agent", args={"agent_name": "refunds"})
        return [types.Part(function_call=call)]
    names = {"weather": "get_weather", "crash": "flaky", "wait": "blocking", "wrong": "get_weather"}
    names |= {"forbidden": "forbidden", "unknown": "nonexistent"}
    if word in names and not responses:
        args: dict[str, Any] = {"town": 3} if word == "wrong" else {"city": rest}
        return [types.Part(function_call=types.FunctionCall(id=f"call-{rest}", name=names[word], args=args))]
    if responses:
        result = (responses[-1].response or {}).get("result", responses[-1].response)
        return [types.Part(text=f"tool said: {result}")]
    return [types.Part(text=f"echo: {prompt}")]


class Scripted(BaseLlm):
    """A model of ADK that answers with ``reply``, from the agent's instruction and the request's contents."""

    model: str = "scripted"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        config = llm_request.config
        # ADK adds lines of its own to the agent's instruction (its name, its sub-agents): the first is the agent's.
        system = str(config.system_instruction).splitlines()[0] if config and config.system_instruction else ""
        parts = reply(system, list(llm_request.contents))
        yield LlmResponse(content=types.Content(role="model", parts=parts))


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


@tool
async def forbidden(city: str) -> str:
    """Fails for good."""
    raise restate.TerminalError(f"no access to {city}")


assistant = adk.agent(
    "adk_assistant", model=Scripted(), system="Answer briefly.", tools=[get_weather, flaky, blocking, forbidden]
)


@assistant.handler
async def twice(ctx: sessions.SessionContext, text: str) -> list[str]:
    """Two prompts of one invocation: each its own."""
    return [await adk.prompt(ctx, text, model=Scripted()), await adk.prompt(ctx, f"again {text}", model=Scripted())]


def desk() -> LlmAgent:
    """A triage agent that transfers refunds to the refunds agent, which has this package's tools."""
    refunds = LlmAgent(
        name="refunds", model=Scripted(), instruction="You handle refunds.", tools=adk.tools([get_weather])
    )
    return LlmAgent(name="triage", model=Scripted(), instruction="You triage.", sub_agents=[refunds])


triage = adk.agent("adk_desk", agent=desk())


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([assistant, triage]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, text: str, service: str = "adk_assistant") -> httpx2.Response:
    return await post(harness, f"{service}/{key}/chat", content=text, headers=TEXT)


async def messages(harness: HarnessEnvironment, key: str) -> list[tuple[str, str]]:
    page = typing.cast(Messages, (await post(harness, f"adk_assistant/{key}/messages", json={})).json())
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


# --- Against a Restate server ---------------------------------------------------------------------


async def test_a_message_is_answered_with_the_instruction_and_kept_in_the_conversation(harness: HarnessEnvironment):
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


async def test_arguments_that_do_not_fit_are_the_models_to_fix(harness: HarnessEnvironment):
    k = key()
    wrong = (await chat(harness, k, f"wrong {k}")).json()
    assert wrong.startswith("tool said: Error: the arguments do not fit")


async def test_a_call_to_a_tool_the_agent_does_not_have_is_the_models_to_fix(harness: HarnessEnvironment):
    k = key()
    unknown = (await chat(harness, k, f"unknown {k}")).json()
    assert unknown == "tool said: Error: there is no tool named 'nonexistent'"


async def test_a_tools_terminal_error_is_its_answer(harness: HarnessEnvironment):
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
            f"{harness.ingress_url}/adk_assistant/{k}/chat/send", content=f"wait {k}", headers=TEXT
        )
        invocation = sent.json()["invocationId"]

        async def in_the_tool() -> bool:
            return k in blocked

        await until(in_the_tool)
        await post(harness, f"adk_assistant/{k}/stop", json=None)
        ended = await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation}/attach")
    assert ended.status_code == 409, ended.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"
    thread = await messages(harness, k)
    assert ("assistant", CANCELLED) in thread
    assert thread[-1] == ("assistant", f"echo: hi {k}")


async def test_each_prompt_of_an_invocation_is_its_own(harness: HarnessEnvironment):
    k = key()
    answered = await post(harness, f"adk_assistant/{k}/twice", json=f"hi {k}")
    assert answered.json() == [f"echo: hi {k}", f"echo: again hi {k}"]


async def test_a_transfer_to_a_sub_agent_holds_for_the_next_messages(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"system {k}", "adk_desk")).json() == "system: You triage."
    await chat(harness, k, f"handoff {k}", "adk_desk")
    assert (await chat(harness, k, f"system {k}", "adk_desk")).json() == "system: You handle refunds."
    # The refunds agent has the tools that adk.tools() gave it.
    assert (await chat(harness, k, f"weather {k}", "adk_desk")).json() == f"tool said: sunny in {k}"


# --- Definitions ----------------------------------------------------------------------------------


def test_an_agent_needs_a_google_model_or_one_of_adk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RESTATE_AGENT_MODEL", raising=False)
    with pytest.raises(restate.TerminalError, match="needs a model"):
        adk.agent("no_model")
    with pytest.raises(restate.TerminalError, match="not google"):
        adk.agent("other", model="openai/gpt-6-sol")
    adk.agent("gemini", model="google/gemini-x")


def test_an_agent_of_your_own_takes_none_of_the_built_in_agents_options():
    # Any: no overload takes both, as the type checker says.
    make: Any = adk.agent
    with pytest.raises(TypeError, match="your own agent is configured .*: pass no system with it"):
        make("mine", agent=LlmAgent(name="mine", model=Scripted()), system="theirs")
    with pytest.raises(TypeError, match="pass no model, tools with it"):
        make("mine", agent=LlmAgent(name="mine", model=Scripted()), model=Scripted(), tools=[get_weather])
    ask: Any = adk.prompt
    with pytest.raises(TypeError, match="pass no tools with it"):
        asyncio.run(ask(None, "hi", agent=LlmAgent(name="mine", model=Scripted()), tools=[get_weather]))
    # No tools is none of the built-in agent's options.
    make("mine", agent=LlmAgent(name="mine", model=Scripted()), tools=[])
    # The options of a run of your own agent, which the built-in agent would ignore.
    with pytest.raises(TypeError, match="run_config is an option of your own agent: pass it with agent="):
        make("built_in", model=Scripted(), run_config=RunConfig())
    with pytest.raises(TypeError, match="run_config is an option of your own agent"):
        asyncio.run(ask(None, "hi", model=Scripted(), run_config=RunConfig()))


# --- In process -----------------------------------------------------------------------------------


def final_response(event: Any) -> bool:
    return event.author != "user" and event.is_final_response() and event.content is not None


def end_of_agent(event: Any) -> bool:
    """The event a resumable app stores once an agent is done, after its final response: it has no content."""
    return bool(event.actions and event.actions.end_of_agent)


@pytest.mark.parametrize("crash_after", [final_response, end_of_agent])
async def test_an_answer_stored_before_a_crash_is_not_asked_for_again(
    monkeypatch: pytest.MonkeyPatch, crash_after: Callable[[Any], bool]
):
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents.adk import _agent  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    replies: list[str] = []

    class Answering(BaseLlm):
        model: str = "answering"

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            replies.append("asked")
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=f"answer {len(replies)}")]))

    append_event = _agent._Sessions.append_event  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def store_then_crash_once(self: Any, session: Any, event: Any) -> Any:
        stored = await append_event(self, session, event)
        if crash_after(event) and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the answer")
        return stored

    monkeypatch.setattr(_agent._Sessions, "append_event", store_then_crash_once)  # pyright: ignore[reportPrivateUsage]
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await adk.prompt(ctx, text, model=Answering())

    backend, invocation = Backend(), request()
    host = Host(backend, service="asker", answers={"publish": b"null"})
    with pytest.raises(RuntimeError, match="crashed after storing"):
        await invoke(asker, "ask", host, invocation, "hi")
    # The retry (the same invocation) returns the answer that was stored, without asking the model again.
    host = Host(backend, service="asker", answers={"publish": b"null"})
    assert await invoke(asker, "ask", host, invocation, "hi") == "answer 1"
    assert replies == ["asked"]


async def test_an_app_of_your_own_keeps_its_plugins_and_gets_this_packages_tools():
    from google.adk.apps import App  # pylint: disable=import-outside-toplevel
    from google.adk.plugins import BasePlugin  # pylint: disable=import-outside-toplevel
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    requests: list[str] = []

    class Counting(BasePlugin):
        async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> None:
            requests.append(callback_context.agent_name)

    mine = App(
        name="mine",
        root_agent=LlmAgent(name="mine", model=Scripted(), instruction="Mine.", tools=adk.tools([get_weather])),
        plugins=[Counting("counting")],
    )
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await adk.prompt(ctx, text, agent=mine)

    backend = Backend()

    async def asked(text: str) -> Any:
        return await invoke(asker, "ask", Host(backend, service="asker", answers={"publish": b"null"}), request(), text)

    assert await asked("system x") == "system: Mine."
    assert await asked("weather Rome") == "tool said: sunny in Rome"
    # The app's own plugin saw each model request: one, then two (the tool call, then the answer).
    assert requests == ["mine", "mine", "mine"]


def asking(agent: Any = None, **options: Any) -> Callable[..., Awaitable[Any]]:
    """Return how to ask, in process, an object whose handler answers with ``adk.prompt`` (in one backend)."""
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    asker = sessions.SessionObject("asker")

    if agent is not None:
        options["agent"] = agent

    @asker.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await adk.prompt(ctx, text, **options)

    backend = Backend()

    async def asked(text: str, invocation: Any = None) -> Any:
        host = Host(backend, service="asker", answers={"publish": b"null"})
        return await invoke(asker, "ask", host, invocation or request(), text)

    return asked


async def test_a_run_of_more_model_calls_than_its_run_config_allows_fails_its_prompt():
    mine = LlmAgent(name="mine", model=Scripted(), tools=adk.tools([get_weather]))
    asked = asking(mine, run_config=RunConfig(max_llm_calls=1))
    with pytest.raises(restate.TerminalError, match="limit of model calls"):
        await asked("weather Rome")
    assert await asked("hi there") == "echo: hi there"
    asked = asking(mine, run_config=RunConfig(max_llm_calls=2))
    assert await asked("weather Rome") == "tool said: sunny in Rome"


class Blocked(BaseLlm):
    """A model whose every response is blocked, as a safety filter blocks one: an error code, no content."""

    model: str = "blocked"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(error_code="SAFETY", error_message="the response was blocked")


async def test_a_response_the_model_blocks_fails_the_prompt_for_good():
    ask = asking(model=Blocked())
    with pytest.raises(restate.TerminalError, match="SAFETY.*blocked"):
        await asyncio.wait_for(ask("hi there"), timeout=10)


async def test_an_agent_of_your_own_runs_as_configured_its_sub_agents_with_its_model():
    refunds = LlmAgent(name="refunds", instruction="You handle refunds.")
    ask = asking(LlmAgent(name="triage", model=Scripted(), instruction="You triage.", sub_agents=[refunds]))
    await ask("handoff x")
    assert await ask("system x") == "system: You handle refunds."


async def test_a_workflows_first_step_stored_before_a_crash_is_not_its_answer(monkeypatch: pytest.MonkeyPatch):
    from google.adk.agents import SequentialAgent  # pylint: disable=import-outside-toplevel
    from sessions_object import request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents.adk import _agent  # pylint: disable=import-outside-toplevel

    append_event = _agent._Sessions.append_event  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def crash_after_the_first_step(self: Any, session: Any, event: Any) -> Any:
        stored = await append_event(self, session, event)
        if event.author == "first" and final_response(event) and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after the first step")
        return stored

    monkeypatch.setattr(_agent._Sessions, "append_event", crash_after_the_first_step)  # pyright: ignore[reportPrivateUsage]
    steps: list[Any] = [
        LlmAgent(name="first", model=Scripted(), instruction="Step one."),
        LlmAgent(name="second", model=Scripted(), instruction="Step two."),
    ]
    ask, invocation = asking(SequentialAgent(name="pipeline", sub_agents=steps)), request()
    with pytest.raises(RuntimeError, match="crashed after the first step"):
        await ask("system x", invocation)
    # The retry goes on with the second step, whose response is the answer.
    assert await ask("system x", invocation) == "system: Step two."


async def test_the_built_in_agent_compacts_a_long_conversation():
    from restate.agents.adk._agent import COMPACT_EVERY  # pylint: disable=import-outside-toplevel

    seen: list[list[types.Content]] = []

    class Seeing(Scripted):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            seen.append(list(llm_request.contents))
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    asked = asking(model=Seeing())
    for n in range(1, COMPACT_EVERY + 2):
        assert await asked(f"hi {n}") == f"echo: hi {n}"
    # After the first COMPACT_EVERY invocations, ADK had the model summarize them (a request of its own) ...
    summarized = seen[-2]
    assert len(summarized) == 1 and "hi 1" in str(summarized[0].parts)
    # ... and the next request has the summary (as ADK shows an event of another author) in place of their events.
    latest = seen[-1]
    assert len(latest) == 2
    assert "said: echo: The following is a conversation history" in str(latest[0].parts)
    assert latest[-1].parts and latest[-1].parts[0].text == f"hi {COMPACT_EVERY + 1}"


class BadRequestError(Exception):
    """As a model's SDK names a request it refuses: the model's provider refusing, if it ended the turn."""


async def test_a_compaction_that_fails_leaves_the_turn_answered():
    from restate.agents.adk._agent import COMPACT_EVERY  # pylint: disable=import-outside-toplevel

    summaries: list[str] = []

    class NoSummaries(Scripted):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            if "The following is a conversation history" in str(llm_request.contents):
                summaries.append("refused")
                raise BadRequestError("the summary's request is refused")
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    asked = asking(model=NoSummaries())
    for n in range(1, COMPACT_EVERY + 2):
        assert await asked(f"hi {n}") == f"echo: hi {n}"
    # ADK tried to compact after the last two turns, which were answered all the same.
    assert summaries == ["refused", "refused"]
