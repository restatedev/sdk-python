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
"""An OpenAI Agents SDK agent on a scripted model, against a real Restate server: each point of _agent.py's docstring."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import copy
import dataclasses
import json
import typing
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

pytest.importorskip("agents")

from agents import (  # noqa: E402
    Agent,
    GuardrailFunctionOutput,
    Handoff,
    Model,
    ModelResponse,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    Usage,
    function_tool,
    handoff,
    output_guardrail,
    set_tracing_disabled,
    tool_input_guardrail,
)
from agents.extensions import handoff_filters  # noqa: E402
from agents.run import CallModelData, ModelInputData  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText  # noqa: E402

import restate  # noqa: E402
from support import RESTATE_IMAGE  # noqa: E402  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import openai, tool  # noqa: E402
from restate.agents._api import Messages  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402

# No traces sent to OpenAI: the tests have no key.
set_tracing_disabled(True)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As a model SDK names the error of a missing or wrong API key."""


Item = dict[str, Any]
Output = ResponseOutputMessage | ResponseFunctionToolCall


def said(text: str) -> ResponseOutputMessage:
    content: list[Any] = [ResponseOutputText(type="output_text", text=text, annotations=[])]
    return ResponseOutputMessage(id="msg", type="message", role="assistant", status="completed", content=content)


def calls(call_id: str, name: str, arguments: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(type="function_call", call_id=call_id, name=name, arguments=arguments)


class Scripted(Model):
    """A model of the SDK that answers with ``reply``, from the agent's instructions and the input; it keeps each request."""

    def __init__(self, reply: Callable[[str | None, list[Item]], list[Output]]) -> None:
        self.reply = reply
        self.requests: list[tuple[str | None, list[Item]]] = []

    async def get_response(
        self, system_instructions: str | None, input: Any, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        items: list[Item] = (
            [{"role": "user", "content": input}] if isinstance(input, str) else copy.deepcopy(list(input))
        )
        self.requests.append((system_instructions, items))
        return ModelResponse(output=list(self.reply(system_instructions, items)), usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


asked: collections.Counter[str] = collections.Counter()
"""How often the model was asked, by the key word that ends each test's first prompt."""


def _prompts(items: list[Item]) -> list[str]:
    return [str(i["content"]) for i in items if i.get("role") == "user" and isinstance(i.get("content"), str)]


def reply(system: str | None, items: list[Item]) -> list[Output]:
    """The scripted model: it answers the last prompt from the conversation alone, as a retry would ask again."""
    prompt = _prompts(items)[-1]
    asked[_prompts(items)[0].split(" ")[-1]] += 1
    last = items[-1]
    result = last.get("output") if last.get("type") == "function_call_output" else None
    word, _, rest = prompt.partition(" ")
    if word == "system":
        return [said(f"system: {system}")]
    if word == "refuse":
        raise AuthenticationError("invalid x-api-key")
    if word == "loop":
        return [calls(f"call-{uuid.uuid4().hex}", "get_weather", f'{{"city": "{rest}"}}')]
    names = {"weather": "get_weather", "crash": "flaky", "wait": "blocking", "wrong": "get_weather"}
    names |= {"unknown": "nonexistent", "forbidden": "forbidden", "handoff": "transfer_to_refunds", "book": "book"}
    if word == "secret":
        return [said("the SECRET is 42")]
    if word == "handoff" and system == "You handle refunds." and result is None:
        # The refunds agent, handed the conversation without the handoff (a filter took it out).
        return [said(f"refunds: {rest}")]
    if word in names and result is None:
        arguments = '{"town": 3}' if word == "wrong" else "{}" if word == "handoff" else f'{{"city": "{rest}"}}'
        return [calls(f"call-{rest}", names[word], arguments)]
    if result is not None:
        return [said(f"tool said: {result}")]
    return [said(f"echo: {prompt}")]


model = Scripted(reply)


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


assistant = openai.agent(
    "oai_assistant", model=model, system="Answer briefly.", tools=[get_weather, flaky, blocking, forbidden]
)


@assistant.handler
async def twice(ctx: sessions.SessionContext, text: str) -> list[str]:
    """Two prompts of one invocation: each its own."""
    return [await openai.prompt(ctx, text, model=model), await openai.prompt(ctx, f"again {text}", model=model)]


async def tools_of(ctx: sessions.SessionContext) -> list[openai.AnyTool]:
    """Each user's tools, as an async function of the context gives them."""
    return [get_weather]


per_user = openai.agent("oai_per_user", model=model, system=lambda ctx: f"You help {ctx.key}.", tools=tools_of)


def desk(model: Model) -> Agent[Any]:
    """A triage agent that hands refunds off to the refunds agent, which has this package's tools."""
    refunds = Agent(name="refunds", instructions="You handle refunds.", model=model, tools=openai.tools([get_weather]))
    return Agent(name="triage", instructions="You triage.", model=model, handoffs=[refunds])


triage = openai.agent("oai_desk", agent=desk(model))
forgetful = openai.agent("oai_desk_window_0", agent=desk(model), window=0)
"""The same desk, whose model sees no earlier turn whole: the handoff is carried from turn to turn."""
remembering = Scripted(reply)
"""The model of an agent with a small budget, whose requests the tests read."""
summarizing = openai.agent("oai_summarizing", model=remembering, budget=10)
"""An agent whose model sees 40 characters of the turns before the window: it summarizes the earlier ones."""


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([assistant, per_user, triage, forgetful, summarizing]), restate_image=RESTATE_IMAGE
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, text: str, service: str = "oai_assistant") -> httpx2.Response:
    return await post(harness, f"{service}/{key}/chat", content=text, headers=TEXT)


async def messages(harness: HarnessEnvironment, key: str) -> list[tuple[str, str]]:
    page = typing.cast(Messages, (await post(harness, f"oai_assistant/{key}/messages", json={})).json())
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
            f"{harness.ingress_url}/oai_assistant/{k}/chat/send", content=f"wait {k}", headers=TEXT
        )
        invocation = sent.json()["invocationId"]

        async def in_the_tool() -> bool:
            return k in blocked

        await until(in_the_tool)
        await post(harness, f"oai_assistant/{k}/stop", json=None)
        ended = await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation}/attach")
    assert ended.status_code == 409, ended.text
    assert (await chat(harness, k, f"hi {k}")).json() == f"echo: hi {k}"
    thread = await messages(harness, k)
    assert ("assistant", CANCELLED) in thread
    assert thread[-1] == ("assistant", f"echo: hi {k}")


async def test_each_prompt_of_an_invocation_is_its_own(harness: HarnessEnvironment):
    k = key()
    answered = await post(harness, f"oai_assistant/{k}/twice", json=f"hi {k}")
    assert answered.json() == [f"echo: hi {k}", f"echo: again hi {k}"]


async def test_options_may_be_functions_of_the_context(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"system {k}", "oai_per_user")).json() == f"system: You help {k}."
    assert (await chat(harness, k, f"weather {k}", "oai_per_user")).json() == f"tool said: sunny in {k}"


async def test_a_handoff_holds_for_the_next_messages(harness: HarnessEnvironment):
    k = key()
    assert (await chat(harness, k, f"system {k}", "oai_desk")).json() == "system: You triage."
    await chat(harness, k, f"handoff {k}", "oai_desk")
    assert (await chat(harness, k, f"system {k}", "oai_desk")).json() == "system: You handle refunds."
    # The refunds agent has the tools that openai.tools() gave it.
    assert (await chat(harness, k, f"weather {k}", "oai_desk")).json() == f"tool said: sunny in {k}"


async def test_turns_that_outgrow_the_budget_are_summarized_once_answered(harness: HarnessEnvironment):
    k = key()
    for word in ("one", "two", "three", "four", "five"):
        assert (await chat(harness, k, f"hi {word}", "oai_summarizing")).json() == f"echo: hi {word}"
    # Once the fourth turn was answered, the three before it (out of the next window) outgrew the budget: the model
    # summarized them, in a request of its own.
    summaries = [
        _prompts(items)[-1] for _, items in remembering.requests if _prompts(items)[-1].startswith("Summarize")
    ]
    assert len(summaries) == 1
    assert "User: hi one" in summaries[0] and "User: hi three" in summaries[0] and "hi four" not in summaries[0]
    # The fifth turn's request has the summary in their place, then the fourth turn (the window), and the prompt.
    latest = _prompts(remembering.requests[-1][1])
    assert latest[0].startswith("A summary of our conversation so far:") and "User: hi one" in latest[0]
    assert latest[1:] == ["hi four", "hi five"]
    # The conversation itself keeps every turn.
    page = (await post(harness, f"oai_summarizing/{k}/messages", json={})).json()
    assert len(page["messages"]) == 10


async def test_a_handoff_holds_when_the_model_sees_no_earlier_turn_whole(harness: HarnessEnvironment):
    k = key()
    await chat(harness, k, f"handoff {k}", "oai_desk_window_0")
    assert (await chat(harness, k, f"system {k}", "oai_desk_window_0")).json() == "system: You handle refunds."
    assert (await chat(harness, k, f"system {k}", "oai_desk_window_0")).json() == "system: You handle refunds."


# --- In process -----------------------------------------------------------------------------------

PUBLISHED = {"publish": b"null"}
"""What the signals object answers when the turn publishes its progress (at each tool call)."""


def asker(**options: Any) -> sessions.SessionObject:
    """An object of your own whose handler answers each prompt with ``openai.prompt``."""
    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await openai.prompt(ctx, text, **options)

    return obj


async def test_an_answer_stored_before_a_crash_is_not_asked_for_again(monkeypatch: pytest.MonkeyPatch):
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    answering = Scripted(lambda system, items: [said(f"answer {len(answering.requests)}")])
    on_llm_end = _agent._Hooks.on_llm_end  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def store_then_crash_once(self: Any, *args: Any) -> None:
        await on_llm_end(self, *args)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the answer")

    monkeypatch.setattr(_agent._Hooks, "on_llm_end", store_then_crash_once)  # pyright: ignore[reportPrivateUsage]
    backend, invocation, obj = Backend(), request(), asker(model=answering)
    with pytest.raises(RuntimeError, match="crashed after storing"):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "hi")
    # The retry (the same invocation) returns the answer that was stored, without asking the model again.
    assert await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "hi") == "answer 1"
    assert len(answering.requests) == 1


async def test_a_turn_closed_after_its_handoff_carries_the_agent_it_handed_off_to():
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents import AgentObject  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    def failing(system: str | None, items: list[Item]) -> list[Output]:
        word = _prompts(items)[-1].split(" ")[0]
        if word == "handfail" and system == "You triage.":
            return [calls("call-h", "transfer_to_refunds", "{}")]
        if word == "handfail":
            # The agent handed off to fails for good, before its first reply: the turn closes with the error.
            raise restate.TerminalError("the refunds model is down")
        return reply(system, items)

    # A window of 0: the next turn loads no reply of this one, only what it carried.
    obj = AgentObject("asker", window=0)
    desk_agent = desk(Scripted(failing))

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await openai.prompt(ctx, text, agent=desk_agent)

    backend = Backend()
    with pytest.raises(restate.TerminalError, match="refunds model is down"):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "handfail x")
    answer = await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "system x")
    assert answer == "system: You handle refunds."


@pytest.mark.parametrize("ends", ["killed, then settled", "crashed, then failed"])
async def test_a_turn_that_ends_after_its_handoff_another_way_carries_the_agent_it_handed_off_to(ends: str):
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents import AgentObject  # pylint: disable=import-outside-toplevel
    from restate.agents._conversation import settle  # pylint: disable=import-outside-toplevel
    from restate.agents.openai._agent import ITEMS  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    asked_refunds: list[str] = []

    def dying(system: str | None, items: list[Item]) -> list[Output]:
        word = _prompts(items)[-1].split(" ")[0]
        if word == "handgone" and system == "You triage.":
            return [calls("call-g", "transfer_to_refunds", "{}")]
        if word == "handgone":
            asked_refunds.append("asked")
            if ends == "crashed, then failed" and len(asked_refunds) > 1:
                raise restate.TerminalError("the refunds model is down")
            # The invocation is gone (or crashed) after the handoff, before the refunds agent replies.
            raise RuntimeError("the invocation is gone")
        return reply(system, items)

    obj = AgentObject("asker", window=0)
    desk_agent = desk(Scripted(dying))

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await openai.prompt(ctx, text, agent=desk_agent)

    @obj.handler(name="settle_as_the_companion_has_it")
    async def settling(ctx: sessions.SessionContext, invocation_id: str) -> bool:
        return await settle(ctx, ITEMS, invocation_id)

    backend = Backend()

    def host() -> Host:
        return Host(backend, service="asker", answers=PUBLISHED)

    gone = request()
    with pytest.raises(RuntimeError, match="gone"):
        await invoke(obj, "ask", host(), gone, "handgone x")
    if ends == "killed, then settled":
        assert await invoke(obj, "settle_as_the_companion_has_it", host(), request(), gone.id) is True
    else:
        # The retry starts with the refunds agent, which fails for good: the turn closes with its error.
        with pytest.raises(restate.TerminalError, match="model is down"):
            await invoke(obj, "ask", host(), gone, "handgone x")
    assert await invoke(obj, "ask", host(), request(), "system x") == "system: You handle refunds."


async def test_a_handoff_cut_off_by_a_crash_is_asked_again_and_hands_off_once(monkeypatch: pytest.MonkeyPatch):
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    scripted = Scripted(reply)
    on_llm_end = _agent._Hooks.on_llm_end  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def store_then_crash_once(self: Any, context: Any, agent: Any, response: Any) -> None:
        await on_llm_end(self, context, agent, response)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the handoff")

    monkeypatch.setattr(_agent._Hooks, "on_llm_end", store_then_crash_once)  # pyright: ignore[reportPrivateUsage]
    backend, invocation, obj = Backend(), request(), asker(agent=desk(scripted))
    with pytest.raises(RuntimeError, match="crashed after storing"):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "handoff x")
    handed = await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "handoff x")
    assert handed.startswith("tool said: ")
    # Triage was asked twice (its reply was dropped and asked again), then refunds once.
    assert [system for system, _ in scripted.requests] == ["You triage.", "You triage.", "You handle refunds."]
    following = await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "system x")
    assert following == "system: You handle refunds."


async def test_too_many_turns_fail_the_prompt_for_good_and_the_next_is_answered():
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    backend, obj = Backend(), asker(model=Scripted(reply), tools=[get_weather])
    with pytest.raises(restate.TerminalError, match="Max turns"):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "loop Rome")
    assert await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "hi there") == (
        "echo: hi there"
    )


def test_a_model_of_another_provider_is_refused():
    with pytest.raises(restate.TerminalError, match="openai"):
        openai.agent("other", model="anthropic/claude-opus-5-5")


def test_an_agent_of_your_own_takes_none_of_the_built_in_agents_options():
    # Any: no overload takes both, as the type checker says.
    make: Any = openai.agent
    with pytest.raises(TypeError, match="your own agent is configured .*: pass no system with it"):
        make("mine", agent=Agent(name="mine"), system="theirs")
    with pytest.raises(TypeError, match="pass no model, tools with it"):
        make("mine", agent=Agent(name="mine"), model="openai/gpt-6-sol", tools=[get_weather])
    ask: Any = openai.prompt
    with pytest.raises(TypeError, match="pass no tools with it"):
        asyncio.run(ask(None, "hi", agent=Agent(name="mine"), tools=[get_weather]))
    # No tools is none of the built-in agent's options.
    make("mine", agent=Agent(name="mine"), tools=[])
    # The options of a run of your own agent, which the built-in agent would ignore.
    with pytest.raises(TypeError, match="context, run_config are options of your own agent: pass them with agent="):
        make("built_in", system="Hi.", context=object(), run_config=RunConfig())
    with pytest.raises(TypeError, match="run_config is an option of your own agent"):
        asyncio.run(ask(None, "hi", run_config=RunConfig()))


async def test_a_run_of_more_turns_than_max_turns_fails_its_prompt():
    ask = invoker(asker(model=Scripted(reply), tools=[get_weather], max_turns=1))
    with pytest.raises(restate.TerminalError, match="Max turns"):
        await ask("weather Rome")
    assert await ask("hi there") == "echo: hi there"
    ask = invoker(asker(model=Scripted(reply), tools=[get_weather], max_turns=2))
    assert await ask("weather Rome") == "tool said: sunny in Rome"


class Account:
    """The run context of an Agent of your own: whose account it is."""

    def __init__(self, name: str) -> None:
        self.name = name


async def test_an_agent_of_your_own_gets_its_context_and_its_run_config():
    filtered: list[int] = []

    async def instructions(run: RunContextWrapper[Account], agent: Agent[Account]) -> str:
        return f"You help {run.context.name}."

    async def keep_three(data: CallModelData[Any]) -> ModelInputData:
        # It runs after the tools' outputs are stored, and changes only what the model sees.
        filtered.append(len(data.model_data.input))
        return ModelInputData(input=data.model_data.input[-3:], instructions=f"{data.model_data.instructions}!")

    scripted = Scripted(reply)
    mine = Agent[Account](name="mine", instructions=instructions, model=scripted, tools=openai.tools([get_weather]))
    config = RunConfig(call_model_input_filter=keep_three)
    ask = invoker(asker(agent=mine, context=Account("Ada"), run_config=config))
    assert await ask("system now") == "system: You help Ada.!"
    assert await ask("weather Rome") == "tool said: sunny in Rome"
    assert await ask("hi there") == "echo: hi there"
    # The conversation is stored whole, though the model saw only the last three items of each request.
    assert filtered == [1, 3, 5, 7]
    assert [len(items) for _, items in scripted.requests] == [1, 3, 3, 3]


# --- In process: an Agent of your own's options ---------------------------------------------------


def invoker(obj: sessions.SessionObject) -> Callable[..., Awaitable[Any]]:
    """Invoke ``obj``'s ``ask`` in one backend: with an invocation's request to retry it, or a new one."""
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    backend = Backend()

    async def ask(text: str, invocation: Any = None) -> Any:
        host = Host(backend, service="asker", answers=PUBLISHED)
        return await invoke(obj, "ask", host, invocation or request(), text)

    return ask


def crash_once_after(monkeypatch: pytest.MonkeyPatch, hook: str, when: Callable[..., bool]) -> None:
    """Make ``_Hooks.<hook>`` crash, once, after it has run for arguments that ``when`` accepts."""
    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel

    original = getattr(_agent._Hooks, hook)  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def then_crash(self: Any, *args: Any) -> Any:
        returned = await original(self, *args)
        if not crashed and when(*args):
            crashed.append(True)
            raise RuntimeError(f"the process crashed after {hook}")
        return returned

    monkeypatch.setattr(_agent._Hooks, hook, then_crash)  # pyright: ignore[reportPrivateUsage]


async def test_a_turn_answered_by_the_tool_of_an_agent_handed_off_to_carries_that_agent():
    @tool
    async def book(city: str) -> str:
        """Book a trip."""
        return f"booked {city}"

    def booking(system: str | None, items: list[Item]) -> list[Output]:
        word = _prompts(items)[-1].split(" ")[0]
        if word == "handbook" and system == "You triage.":
            return [calls("call-h", "transfer_to_booker", "{}")]
        if word == "handbook" and not any(i.get("name") == "book" for i in items):
            return [calls("call-b", "book", '{"city": "Rome"}')]
        return reply(system, items)

    scripted = Scripted(booking)
    booker = Agent(
        name="booker",
        instructions="You book.",
        model=scripted,
        tools=openai.tools([book]),
        tool_use_behavior="stop_on_first_tool",
    )
    triage = Agent(name="triage", instructions="You triage.", model=scripted, handoffs=[booker])
    ask = invoker(asker(agent=triage))
    # The booker answers with its tool's output, and no reply of its own: the turn stores the answer as its reply.
    assert await ask("handbook Rome") == "booked Rome"
    # The next turn is with the booker still.
    assert await ask("system x") == "system: You book."


async def test_a_handoff_built_by_hand_runs():
    scripted = Scripted(reply)
    refunds = Agent(name="refunds", instructions="You handle refunds.", model=scripted)

    async def to_refunds(context: Any, arguments: str) -> Agent[Any]:
        return refunds

    # Built by hand, not with handoff(): it names no agent until the SDK invokes it.
    by_hand = Handoff(
        tool_name="transfer_to_refunds",
        tool_description="Hand off to the refunds agent.",
        input_json_schema={},
        on_invoke_handoff=to_refunds,
        agent_name="refunds",
    )
    ask = invoker(asker(agent=Agent(name="triage", instructions="You triage.", model=scripted, handoffs=[by_hand])))
    # The refunds agent answers, with the handoff's output as the latest item.
    assert await ask("handoff x") == 'tool said: {"assistant": "refunds"}'
    assert [system for system, _ in scripted.requests] == ["You triage.", "You handle refunds."]
    # It is not followed from turn to turn: the next message goes back to the agent given.
    assert await ask("system x") == "system: You triage."


async def test_a_tool_that_ends_the_turn_is_stored_and_not_run_again():
    runs: list[str] = []

    @tool
    async def book(city: str) -> str:
        """Book a trip."""
        runs.append(city)
        return f"booked {city}"

    scripted = Scripted(reply)
    booker = Agent(name="booker", model=scripted, tools=openai.tools([book]), tool_use_behavior="stop_on_first_tool")
    ask = invoker(asker(agent=booker))
    assert await ask("book Rome") == "booked Rome"
    assert await ask("hi there") == "echo: hi there"
    assert runs == ["Rome"]
    _, items = scripted.requests[-1]
    # The tool's output was the answer: the turn ends with it as its message, which the next turn's model sees.
    assert [i.get("type", "message") for i in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
        "message",
    ]
    assert items[-2] == {"role": "assistant", "content": "booked Rome"}


@output_guardrail
async def no_secrets(context: RunContextWrapper[Any], agent: Agent[Any], output: Any) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered="SECRET" in str(output))


async def test_a_reply_an_output_guardrail_blocks_is_not_kept():
    scripted = Scripted(reply)
    ask = invoker(asker(agent=Agent(name="guarded", model=scripted, output_guardrails=[no_secrets])))
    with pytest.raises(restate.TerminalError, match="[Gg]uardrail"):
        await ask("secret now")
    assert await ask("hi there") == "echo: hi there"
    _, items = scripted.requests[-1]
    assert not any("SECRET" in json.dumps(item) for item in items)


async def test_a_stored_reply_is_not_the_answer_before_its_output_guardrails_pass(monkeypatch: pytest.MonkeyPatch):
    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: True)
    ask = invoker(asker(agent=Agent(name="guarded", model=Scripted(reply), output_guardrails=[no_secrets])))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("secret now", invocation)
    with pytest.raises(restate.TerminalError, match="[Gg]uardrail"):
        await ask("secret now", invocation)


async def test_a_handoff_input_filter_keeps_each_prompt_marked(monkeypatch: pytest.MonkeyPatch):
    scripted = Scripted(reply)
    refunds = Agent(name="refunds", instructions="You handle refunds.", model=scripted)
    filtered = handoff(refunds, input_filter=handoff_filters.remove_all_tools)
    triage = Agent(
        name="triage",
        instructions="You triage.",
        model=scripted,
        tools=openai.tools([get_weather]),
        handoffs=[filtered],
    )
    ask = invoker(asker(agent=triage))
    assert await ask("weather Rome") == "tool said: sunny in Rome"
    # The refunds agent's reply is stored, then the process crashes: the retry finds it as the prompt's answer.
    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: agent.name == "refunds")
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("handoff x", invocation)
    asked = len(scripted.requests)
    assert await ask("handoff x", invocation) == "refunds: x"
    assert len(scripted.requests) == asked


async def test_a_reply_cut_off_after_its_handoff_started_is_resumed_with_its_own_agent(monkeypatch: pytest.MonkeyPatch):
    scripted = Scripted(reply)
    crash_once_after(monkeypatch, "on_agent_start", lambda context, agent: agent.name == "refunds")
    ask = invoker(asker(agent=desk(scripted)))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("handoff x", invocation)
    answer = await ask("handoff x", invocation)
    assert "no tool named" not in answer
    assert [system for system, _ in scripted.requests][-1] == "You handle refunds."


async def test_after_a_handoff_the_next_request_is_the_new_agents(monkeypatch: pytest.MonkeyPatch):
    scripted = Scripted(reply)
    # The handoff's output is stored before the refunds agent's first request, then the process crashes.
    crash_once_after(monkeypatch, "model_input", lambda data: data.agent.name == "refunds")
    ask = invoker(asker(agent=desk(scripted)))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("handoff x", invocation)
    asked = len(scripted.requests)
    assert (await ask("handoff x", invocation)).startswith("tool said: ")
    # The retry went on with the refunds agent, without asking triage again.
    assert [system for system, _ in scripted.requests[asked:]] == ["You handle refunds."]


def request_of() -> Any:
    from sessions_object import request  # pylint: disable=import-error,import-outside-toplevel

    return request()


def test_a_tool_output_of_any_kind_is_stored_once():
    from restate.agents._conversation import Conversation, Entry  # pylint: disable=import-outside-toplevel
    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel

    shell = {"type": "shell_call_output", "call_id": "s1", "output": "ok"}
    function = {"type": "function_call_output", "call_id": "f1", "output": "ok"}
    prompt = Entry(0, _agent.ITEMS.prompt("go"), prompt="p0")
    stored = Conversation(typing.cast(Any, None), _agent.ITEMS, [prompt, Entry(1, shell), Entry(2, function)])
    # The SDK hands the stored outputs back with the next request: none of them is new.
    new = {"type": "computer_call_output", "call_id": "c1", "output": "ok"}
    assert stored.new_results([shell, function, new]) == [new]


# --- In process: a conversation that lives long --------------------------------------------------


class ResponsesScripted(OpenAIResponsesModel):
    """A model of the Responses API (as the SDK's default provider makes of a model id), scripted: it keeps the settings."""

    def __init__(self, settings: list[Any]) -> None:
        super().__init__(model="scripted", openai_client=AsyncOpenAI(api_key="no requests"))
        self.scripted = Scripted(reply)
        self.settings = settings

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.settings.append(kwargs["model_settings"])
        return await self.scripted.get_response(*args, **kwargs)


async def test_the_built_in_agent_has_openais_compaction_and_an_agent_of_your_own_runs_as_configured():
    settings: list[Any] = []
    ask = invoker(asker(model=ResponsesScripted(settings)))
    assert await ask("hi there") == "echo: hi there"
    assert settings[-1].context_management == [{"type": "compaction"}]
    ask = invoker(asker(agent=Agent(name="mine", model=ResponsesScripted(settings))))
    assert await ask("hi there") == "echo: hi there"
    assert settings[-1].context_management is None


def test_only_a_model_of_the_responses_api_gets_compaction():
    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel

    def compaction(model: Any) -> Any:
        return _agent._long_lived(model).context_management  # pyright: ignore[reportPrivateUsage]

    client = AsyncOpenAI(api_key="no requests")
    assert compaction("gpt-6-sol") == [{"type": "compaction"}]
    assert compaction(OpenAIResponsesModel(model="gpt-6-sol", openai_client=client)) == [{"type": "compaction"}]
    assert compaction(OpenAIChatCompletionsModel(model="gpt-6-sol", openai_client=client)) is None
    assert compaction(Scripted(reply)) is None


async def test_a_request_starts_with_the_latest_compaction_item(monkeypatch: pytest.MonkeyPatch):
    from restate.agents.openai import _agent  # pylint: disable=import-outside-toplevel

    compacted = {"type": "compaction", "id": "cmp_1", "encrypted_content": "the summary"}
    original = _agent._Hooks.on_llm_end  # pyright: ignore[reportPrivateUsage]

    async def compacting(self: Any, context: Any, agent: Any, response: ModelResponse) -> None:
        # As OpenAI's server-side compaction does, at the second turn: its reply starts with a compaction item.
        if any("compact" in json.dumps(item) for item in response.to_input_items()):
            response = dataclasses.replace(response, output=[typing.cast(Any, compacted), *response.output])
        await original(self, context, agent, response)

    monkeypatch.setattr(_agent._Hooks, "on_llm_end", compacting)  # pyright: ignore[reportPrivateUsage]
    scripted = Scripted(reply)
    ask = invoker(asker(model=scripted))
    assert await ask("hi there") == "echo: hi there"
    assert await ask("compact now") == "echo: compact now"
    assert await ask("hi again") == "echo: hi again"
    _, items = scripted.requests[-1]
    # The request starts with the compaction item, which stands for the conversation before it.
    assert items[0] == compacted
    assert [i.get("content") for i in items if i.get("role") == "user"] == ["hi again"]


# --- In process: errors, approvals and the SDK's tool pipeline -------------------------------------


def weather_tool(ran: list[str], **wrapped: Any) -> Any:
    """A function tool of the SDK's named get_weather (as the scripted model calls), which ``wrapped`` configures."""

    @function_tool(name_override="get_weather", **wrapped)
    def weather(city: str) -> str:
        """The weather in a city."""
        ran.append(city)
        return f"weather in {city}"

    return weather


async def test_a_call_that_needs_an_approval_fails_the_turn_and_never_runs():
    ran: list[str] = []
    scripted = Scripted(reply)
    ask = invoker(asker(agent=Agent(name="guarded", model=scripted, tools=[weather_tool(ran, needs_approval=True)])))
    with pytest.raises(restate.TerminalError, match="needs_approval"):
        await ask("weather Rome")
    # The next message is answered, with the call closed: it never runs.
    assert await ask("hi there") == "echo: hi there"
    assert ran == []
    _, items = scripted.requests[-1]
    closed = [item for item in items if item.get("type") == "function_call_output"]
    assert len(closed) == 1 and str(closed[0]["output"]).startswith("Error: ")


async def test_a_call_cut_off_by_a_crash_that_needs_an_approval_fails_the_turn_without_asking_again(
    monkeypatch: pytest.MonkeyPatch,
):
    ran: list[str] = []
    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: True)
    scripted = Scripted(reply)
    ask = invoker(asker(agent=Agent(name="guarded", model=scripted, tools=[weather_tool(ran, needs_approval=True)])))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("weather Rome", invocation)
    asked = len(scripted.requests)
    with pytest.raises(restate.TerminalError, match="needs_approval"):
        await ask("weather Rome", invocation)
    # Neither run nor asked again (which would run the reply's other tools again, with new call ids).
    assert ran == []
    assert len(scripted.requests) == asked


@pytest.mark.parametrize("needed", [False, True])
async def test_a_call_cut_off_by_a_crash_asks_the_tools_approval_function(
    monkeypatch: pytest.MonkeyPatch, needed: bool
):
    asked_for: list[tuple[dict[str, Any], str]] = []

    async def approval(run: Any, arguments: dict[str, Any], call_id: str) -> bool:
        asked_for.append((arguments, call_id))
        return needed

    ran: list[str] = []
    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: True)
    scripted = Scripted(reply)
    ask = invoker(
        asker(agent=Agent(name="guarded", model=scripted, tools=[weather_tool(ran, needs_approval=approval)]))
    )
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("weather Rome", invocation)
    asked = len(scripted.requests)
    if needed:
        with pytest.raises(restate.TerminalError, match="needs_approval"):
            await ask("weather Rome", invocation)
        assert ran == []
        assert len(scripted.requests) == asked
    else:
        # It needs none: resumed with its call id, rather than asked again.
        assert await ask("weather Rome", invocation) == "tool said: weather in Rome"
        assert ran == ["Rome"]
        assert len(scripted.requests) == asked + 1
    assert asked_for[-1] == ({"city": "Rome"}, "call-Rome")


async def test_a_call_cut_off_by_a_crash_to_a_tool_with_a_timeout_is_resumed_with_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[str] = []

    @function_tool(name_override="get_weather", timeout=0.2)
    async def slow_weather(city: str) -> str:
        """The weather in a city, too slowly."""
        started.append(city)
        await asyncio.sleep(5)
        return f"weather in {city}"

    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: True)
    scripted = Scripted(reply)
    ask = invoker(asker(agent=Agent(name="timed", model=scripted, tools=[slow_weather])))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("weather Rome", invocation)
    asked = len(scripted.requests)
    answer = await ask("weather Rome", invocation)
    # Resumed, with its call id, rather than asked again; and the SDK's timeout applied to it.
    assert started == ["Rome"]
    assert len(scripted.requests) == asked + 1
    assert answer.startswith("tool said: ") and "timed out" in answer


@tool_input_guardrail
def not_in_rome(data: Any) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.reject_content("not in Rome")


async def test_a_call_cut_off_by_a_crash_to_a_guarded_tool_is_asked_for_again_through_its_guardrail(
    monkeypatch: pytest.MonkeyPatch,
):
    ran: list[str] = []
    crash_once_after(monkeypatch, "on_llm_end", lambda context, agent, response: True)
    guarded = weather_tool(ran, tool_input_guardrails=[not_in_rome])
    ask = invoker(asker(agent=Agent(name="guarded", model=Scripted(reply), tools=[guarded])))
    invocation = request_of()
    with pytest.raises(RuntimeError, match="crashed"):
        await ask("weather Rome", invocation)
    assert await ask("weather Rome", invocation) == "tool said: not in Rome"
    assert ran == []


class NotFoundError(Exception):
    """As a database's client names a missing row: the model's provider refusing, if a model's."""


async def test_an_error_of_the_agents_own_code_is_retried_and_a_models_refusal_is_not():
    asked: list[str] = []

    def instructions(run: Any, agent: Any) -> str:
        asked.append("profile")
        if len(asked) == 1:
            raise NotFoundError("no profile yet")
        return "Be kind."

    ask = invoker(asker(agent=Agent(name="profiled", model=Scripted(reply), instructions=instructions)))
    invocation = request_of()
    with pytest.raises(NotFoundError):
        await ask("hi there", invocation)
    assert await ask("hi there", invocation) == "echo: hi there"
    # The model request's own error (an invalid key) is its provider refusing.
    with pytest.raises(restate.TerminalError) as refused:
        await ask("refuse now")
    assert refused.value.status_code == 424

    # An error of the run's own input filter (which runs before the model is made) is retried too.
    filtered: list[str] = []

    def profile_filter(data: Any) -> Any:
        filtered.append("profile")
        if len(filtered) == 1:
            raise NotFoundError("no profile yet")
        return data.model_data

    config = RunConfig(call_model_input_filter=profile_filter)
    ask = invoker(asker(agent=Agent(name="filtered", model=Scripted(reply)), run_config=config))
    invocation = request_of()
    with pytest.raises(NotFoundError):
        await ask("hi there", invocation)
    assert await ask("hi there", invocation) == "echo: hi there"


async def test_a_missing_api_key_is_a_refusal_for_the_built_in_agent_and_one_of_your_own(
    monkeypatch: pytest.MonkeyPatch,
):
    # The SDK makes the model's client (which needs the key) before the model request: still a refusal.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for options in ({"model": "openai/gpt-6-sol"}, {"agent": Agent(name="mine", model="gpt-6-sol")}):
        ask = invoker(asker(**options))
        with pytest.raises(restate.TerminalError) as refused:
            await ask("hi there")
        assert refused.value.status_code == 424, refused.value.message


async def test_a_setup_error_of_the_sdk_ends_the_turn():
    ask = invoker(asker(agent=Agent(name="prefixed", model="nosuchprefix/x")))
    with pytest.raises(restate.TerminalError, match="not set up right: Unknown prefix"):
        await ask("hi there")

    def no_model_input(data: Any) -> Any:
        return data.model_data.input

    filtered = RunConfig(call_model_input_filter=no_model_input)
    ask = invoker(asker(agent=Agent(name="filtered", model=Scripted(reply)), run_config=filtered))
    with pytest.raises(restate.TerminalError, match="not set up right: .*ModelInputData"):
        await ask("hi there")


async def test_a_missing_package_ends_the_turn_but_a_tools_import_error_is_retried():
    import importlib.util  # pylint: disable=import-outside-toplevel

    if importlib.util.find_spec("litellm") is None:
        ask = invoker(asker(agent=Agent(name="lite", model="litellm/anthropic/claude-x")))
        with pytest.raises(restate.TerminalError, match="not set up right: .*litellm"):
            await ask("hi there")

    # A function tool of the SDK's whose errors fail the run (rather than go to the model).
    @function_tool(name_override="get_weather", failure_error_function=None)
    def get_weather(city: str) -> str:
        """The weather in a city, from a package that is not installed yet."""
        raise ImportError("No module named 'weather_api'")

    ask = invoker(asker(agent=Agent(name="weather", model=Scripted(reply), tools=[get_weather])))
    invocation = request_of()
    with pytest.raises(ImportError):
        await ask("weather Rome", invocation)
    # Also on the retry, which runs the stored call again itself.
    with pytest.raises(ImportError):
        await ask("weather Rome", invocation)
