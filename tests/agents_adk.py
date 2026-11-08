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

requests: list[list[types.Content]] = []
"""The requests of the agent with a small budget."""


class Remembering(Scripted):
    """The scripted model, keeping each request's contents."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        requests.append(list(llm_request.contents))
        async for response in super().generate_content_async(llm_request, stream):
            yield response


summarizing = adk.agent("adk_summarizing", model=Remembering(), budget=10)
"""An agent whose model sees 40 characters of the turns before the window: it summarizes the earlier ones."""


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([assistant, triage, summarizing]), restate_image=RESTATE_IMAGE
    ) as harness:
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


def asking(agent: Any = None, backend: Any = None, **options: Any) -> Callable[..., Awaitable[Any]]:
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

    backend = backend or Backend()

    async def asked(text: str, invocation: Any = None) -> Any:
        host = Host(backend, service="asker", answers={"publish": b"null"})
        return await invoke(asker, "ask", host, invocation or request(), text)

    asked.backend = backend  # type: ignore[attr-defined]
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


async def test_a_workflow_nodes_first_step_stored_before_a_crash_is_not_its_answer(monkeypatch: pytest.MonkeyPatch):
    from google.adk.apps import App  # pylint: disable=import-outside-toplevel
    from google.adk.workflow import Workflow  # pylint: disable=import-outside-toplevel
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
    first = LlmAgent(name="first", model=Scripted(), instruction="Step one.")
    second = LlmAgent(name="second", model=Scripted(), instruction="Step two.")
    # The root of an App may be a node of ADK's that is no agent: a Workflow, whose steps take the previous output.
    pipeline = App(name="pipeline", root_agent=Workflow(name="pipeline", edges=[("START", first, second)]))
    ask, invocation = asking(pipeline), request()
    with pytest.raises(RuntimeError, match="crashed after the first step"):
        await ask("system x", invocation)
    # The retry goes on with the second step, whose response is the answer.
    assert await ask("system x", invocation) == "echo: system: Step one."


async def test_an_agent_that_adk_retries_after_a_tools_error_answers_once_it_recovers():
    from google.adk.workflow import RetryConfig  # pylint: disable=import-outside-toplevel

    # ADK runs the agent again, in the same attempt: the tool's error, which it reports first, does not fail the turn.
    retried = RetryConfig(max_attempts=2, initial_delay=0.01, jitter=0)
    mine = LlmAgent(name="mine", model=Scripted(), tools=adk.tools([flaky]), retry_config=retried)
    k = key()
    assert await asking(mine)(f"crash {k}") == f"tool said: recovered in {k}"
    assert crashes[k] == 2


async def test_an_apps_events_compaction_is_off_and_the_conversation_keeps_its_order(
    caplog: pytest.LogCaptureFixture,
):
    from google.adk.apps import App  # pylint: disable=import-outside-toplevel
    from google.adk.apps.app import EventsCompactionConfig  # pylint: disable=import-outside-toplevel
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer  # pylint: disable=import-outside-toplevel
    from google.adk.events import Event  # pylint: disable=import-outside-toplevel
    from google.adk.events.event_actions import EventActions, EventCompaction  # pylint: disable=import-outside-toplevel

    from restate.agents.adk import _agent  # pylint: disable=import-outside-toplevel

    seen: list[list[types.Content]] = []

    class Seeing(Scripted):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            seen.append(list(llm_request.contents))
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    class Summarizing(BaseEventsSummarizer):
        """ADK's events compaction, with a summary of the events it is given."""

        async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
            summary = types.Content(role="model", parts=[types.Part(text=f"a summary of {len(events)} events")])
            ranged = EventCompaction(
                start_timestamp=events[0].timestamp, end_timestamp=events[-1].timestamp, compacted_content=summary
            )
            return Event(author="user", invocation_id=Event.new_id(), actions=EventActions(compaction=ranged))

    every_turn = EventsCompactionConfig(summarizer=Summarizing(), compaction_interval=1, overlap_size=0)
    mine = App(name="mine", root_agent=LlmAgent(name="mine", model=Seeing()), events_compaction_config=every_turn)
    _agent._without_compaction.cache_clear()  # pyright: ignore[reportPrivateUsage]
    asked = asking(mine)
    with caplog.at_level("WARNING", logger=_agent.__name__):
        for n in (1, 2, 3):
            assert await asked(f"hi {n}") == f"echo: hi {n}"
        # The built-in agent has no events compaction to turn off.
        assert await asking(model=Scripted())("hi there") == "echo: hi there"
    # ADK's compaction would sort the events by time, and put the earlier turn's answer (made as the turn loads)
    # after the prompt: the model sees the conversation in its order instead.
    assert [part.text for content in seen[-1] for part in content.parts or []] == [
        "hi 1",
        "echo: hi 1",
        "hi 2",
        "echo: hi 2",
        "hi 3",
    ]
    # Said once per process.
    warnings = [record for record in caplog.records if "events_compaction_config" in record.getMessage()]
    assert len(warnings) == 1


async def test_turns_that_outgrow_the_budget_are_summarized_once_answered(harness: HarnessEnvironment):
    k = key()
    for word in ("one", "two", "three", "four", "five"):
        assert (await chat(harness, k, f"hi {word}", "adk_summarizing")).json() == f"echo: hi {word}"
    summaries = [_prompts(contents)[-1] for contents in requests if _prompts(contents)[-1].startswith("Summarize")]
    assert len(summaries) == 1 and "User: hi one" in summaries[0] and "hi four" not in summaries[0]
    latest = _prompts(requests[-1])
    assert latest[0].startswith("A summary of our conversation so far:") and latest[1:] == ["hi four", "hi five"]
    # The summary is a message of the user's, which the model acknowledges: the roles alternate, as every provider takes them.
    said = [(content.role, "".join(part.text or "" for part in content.parts or [])) for content in requests[-1]]
    assert [role for role, _ in said] == ["user", "model", "user", "model", "user"]
    assert said[1] == ("model", "Understood.")


async def test_the_state_a_killed_turn_set_holds_after_it_ended():
    from google.adk.tools.tool_context import ToolContext  # pylint: disable=import-outside-toplevel

    def get_weather(city: str, tool_context: ToolContext) -> str:  # pylint: disable=redefined-outer-name
        """Remember the city."""
        tool_context.state["city"] = city
        return "noted"

    class Dying(Scripted):
        """Gone after the tool's result is stored, as a killed invocation is."""

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            if any(part.function_response for part in llm_request.contents[-1].parts or []):
                raise RuntimeError("the invocation is gone")
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    dying = LlmAgent(name="mine", model=Dying(), instruction="City: {city?}", tools=[get_weather])
    mine = LlmAgent(name="mine", model=Scripted(), instruction="City: {city?}", tools=[get_weather])
    backend_asked = asking(dying)
    assert await backend_asked("hi 0") == "echo: hi 0"
    with pytest.raises(RuntimeError, match="gone"):
        await backend_asked("weather Lisbon")
    # Another invocation ends the killed turn, and the city its tool stored holds.
    asked = asking(mine, backend=backend_asked.backend)  # type: ignore[attr-defined]
    assert await asked("hi 2") == "echo: hi 2"
    assert await asked("system x") == "system: City: Lisbon"


async def test_earlier_turns_are_their_messages_and_answers():
    seen: list[list[types.Content]] = []

    class Seeing(Scripted):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            seen.append(list(llm_request.contents))
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    asked = asking(model=Seeing(), tools=[get_weather])
    assert await asked("weather Lisbon") == "tool said: sunny in Lisbon"
    assert await asked("hi 2") == "echo: hi 2"
    assert await asked("hi 3") == "echo: hi 3"
    # The first turn is its message and answer; the previous one is whole (here, the same); then the prompt.
    texts = [part.text for content in seen[-1] for part in content.parts or []]
    assert texts == ["weather Lisbon", "tool said: sunny in Lisbon", "hi 2", "echo: hi 2", "hi 3"]


async def test_the_session_state_holds_across_turns_that_are_not_loaded():
    from google.adk.tools.tool_context import ToolContext  # pylint: disable=import-outside-toplevel

    def get_weather(city: str, tool_context: ToolContext) -> str:  # pylint: disable=redefined-outer-name
        """Remember the city."""
        tool_context.state["city"] = city
        return "noted"

    mine = LlmAgent(name="mine", model=Scripted(), instruction="City: {city?}", tools=[get_weather])
    asked = asking(mine)
    assert await asked("weather Lisbon") == "tool said: noted"
    assert await asked("hi 2") == "echo: hi 2"
    assert await asked("hi 3") == "echo: hi 3"
    # The first turn's events are not loaded any more: its state is, as the latest turn carried it.
    assert await asked("system x") == "system: City: Lisbon"
