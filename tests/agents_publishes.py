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
"""When a followed turn publishes, and what a follower then sees: in this process, for each framework.

A turn publishes as it starts (the companion's ``start`` does), after each
write of steps that commits (a reply with tool calls, the tools' results), and
as it closes. A follower reads the ``messages`` view once told: at each
publish, the companion here reads it as a follower would, at once.
"""

# pylint: disable=import-error,import-outside-toplevel

from __future__ import annotations

import json
import typing
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from restate import sessions
from restate.agents import tool
from restate.agents._object import AgentObject

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


CALLS = 3
"""How many tool calls the model's first reply makes, at once."""


@dataclass
class Seen:
    """What a follower saw: at each companion call, and as each model request went out."""

    events: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    """Each companion call that answers with a revision, or a model request, and the results the view showed then."""
    steering: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    """The messages steered to the turn, which each publish returns."""

    def companion_calls(self) -> list[str]:
        return [event for event, _ in self.events if event != "model"]


class Companion:
    """The agent's companion in this process: each publish (and a ``start`` that publishes) reads the ``messages``
    view, as a follower told of it would, and returns a new revision with the messages steered to the turn."""

    def __init__(self, obj: AgentObject, backend: Any, seen: Seen) -> None:
        self.obj = obj
        self.backend = backend
        self.seen = seen
        self.revision = 0

    async def results(self) -> int:
        """Return how many tool results the ``messages`` view shows."""
        from sessions_object import Host, request

        from restate.sessions.internal import invoke

        view = Host(self.backend, service=self.obj.name).view()
        page = await invoke(self.obj, "messages", view, request(), {"limit": 1000})
        return sum("output" in part for message in page["messages"] for part in message["parts"])

    async def answer(self, handler: str, arg: Any) -> bytes:
        if handler == "start" and not json.loads(arg).get("publishes"):
            self.seen.events.append(("start", await self.results()))
            return b"null"
        if handler in ("start", "publish"):
            self.revision += 1
            self.seen.events.append((handler, await self.results()))
            return json.dumps({"revision": self.revision, "steering": self.seen.steering}).encode()
        assert handler == "close", f"the companion has no answer for {handler}"
        self.seen.events.append(("close", await self.results()))
        return b"null"


def follow(obj: AgentObject, seen: Seen, handler: str = "chat") -> Callable[[str], Any]:
    """Return a ``chat`` of ``obj`` in this process, each call a turn, whose follower records what it sees in ``seen``;
    or a call of another of its handlers, ``handler``."""
    from sessions_database import Backend
    from sessions_object import Answer, Host, request

    from restate.sessions.internal import invoke

    backend = Backend()
    companion = Companion(obj, backend, seen)

    class Deferred(Answer):
        def __init__(self, handler: str, arg: Any) -> None:
            super().__init__(b"null")
            self.handler, self.arg = handler, arg

        def __await__(self):
            return companion.answer(self.handler, self.arg).__await__()

    class Followed(Host):
        def generic_call(self, service, handler, arg, key=None, idempotency_key=None, headers=None):
            self.calls.append((service, handler, arg, key, idempotency_key))
            return Deferred(handler, arg)

    async def chat(text: str) -> Any:
        return await invoke(obj, handler, Followed(backend, service=obj.name), request(), text)

    return chat


def lookup_of(seen: Seen, steer: str | None = None):
    """The tool the model calls: it steers ``steer`` to the turn while it runs, if given."""

    @tool(name="lookup")
    async def lookup(about: str) -> str:
        """Look something up."""
        if steer is not None and not seen.steering:
            seen.steering.append({"id": "steer-1", "text": steer})
        return f"found {about}"

    return lookup


# --- The frameworks' agents, each with a model that calls ``lookup`` CALLS times at once, then answers ----------


def pydantic_model(seen: Seen, calls: int = CALLS) -> Any:
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.events.append(("model", 0))
        if any(isinstance(m, ModelResponse) for m in messages) or not calls:
            told = [str(p.content) for m in messages for p in getattr(m, "parts", []) if isinstance(p, UserPromptPart)]
            return ModelResponse(parts=[TextPart(f"answered after {', '.join(told)}")])
        return ModelResponse(
            parts=[ToolCallPart("lookup", {"about": f"t{i}"}, tool_call_id=f"c{i}") for i in range(calls)]
        )

    return FunctionModel(model)


def pydantic_agent(seen: Seen, steer: str | None = None, *, calls: int = CALLS, **options: Any) -> AgentObject:
    from restate.agents import pydantic_ai

    return pydantic_ai.agent("followed", model=pydantic_model(seen, calls), tools=[lookup_of(seen, steer)], **options)


def openai_model(seen: Seen, calls: int = CALLS) -> Any:
    from agents import Model, ModelResponse, Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    class Scripted(Model):
        async def get_response(self, system_instructions, input, *args, **kwargs):  # noqa: A002
            seen.events.append(("model", 0))
            items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
            if any(item.get("type") == "function_call_output" for item in items) or not calls:
                told = [str(item["content"]) for item in items if item.get("role") == "user"]
                text = f"answered after {', '.join(told)}"
                out: list[Any] = [
                    ResponseOutputMessage(
                        id="msg",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
                    )
                ]
            else:
                out = [
                    ResponseFunctionToolCall(
                        type="function_call", call_id=f"c{i}", name="lookup", arguments=f'{{"about": "t{i}"}}'
                    )
                    for i in range(calls)
                ]
            return ModelResponse(output=out, usage=Usage(), response_id=None)

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    return Scripted()


def openai_agent(seen: Seen, steer: str | None = None, *, calls: int = CALLS, **options: Any) -> AgentObject:
    from restate.agents import openai

    return openai.agent("followed", model=openai_model(seen, calls), tools=[lookup_of(seen, steer)], **options)


def adk_model(seen: Seen, calls: int = CALLS) -> Any:
    from google.adk.models import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    class Scripted(BaseLlm):
        model: str = "scripted"

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            seen.events.append(("model", 0))
            contents = list(llm_request.contents)
            responded = any(part.function_response for content in contents for part in content.parts or [])
            if responded or not calls:
                told = [
                    str(part.text)
                    for content in contents
                    if content.role == "user"
                    for part in content.parts or []
                    if part.text
                ]
                parts = [types.Part(text=f"answered after {', '.join(told)}")]
            else:
                parts = [
                    types.Part(function_call=types.FunctionCall(id=f"c{i}", name="lookup", args={"about": f"t{i}"}))
                    for i in range(calls)
                ]
            yield LlmResponse(content=types.Content(role="model", parts=parts))

    return Scripted()


def adk_agent(seen: Seen, steer: str | None = None, *, calls: int = CALLS, **options: Any) -> AgentObject:
    from restate.agents import adk

    return adk.agent("followed", model=adk_model(seen, calls), tools=[lookup_of(seen, steer)], **options)


AGENTS = {"pydantic_ai": pydantic_agent, "openai": openai_agent, "adk": adk_agent}
MODELS = {"pydantic_ai": pydantic_model, "openai": openai_model, "adk": adk_model}
"""Each framework's scripted model, for its ``prompt``."""


def make(framework: str, seen: Seen, steer: str | None = None, **options: Any) -> AgentObject:
    return typing.cast(Callable[..., AgentObject], AGENTS[framework])(seen, steer, **options)


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_follower_sees_the_tools_results_before_the_model_is_asked_again(framework: str):
    seen = Seen()
    chat = follow(make(framework, seen), seen)
    await chat("hello")
    # The second model request: the publish before it showed the results, which were committed just before it.
    second = [i for i, (event, _) in enumerate(seen.events) if event == "model"][1]
    before = [(event, results) for event, results in seen.events[:second] if event != "model"]
    assert before[-1] == ("publish", CALLS)


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_turn_of_tool_calls_publishes_as_it_starts_after_each_commit_and_as_it_closes(framework: str):
    seen = Seen()
    chat = follow(make(framework, seen), seen)
    await chat("hello")
    # The start; the reply with the calls (no results yet); the results; the close.
    assert [e for e in seen.events if e[0] != "model"] == [
        ("start", 0),
        ("publish", 0),
        ("publish", CALLS),
        ("close", CALLS),
    ]


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_plain_turn_publishes_as_it_starts_and_as_it_closes_only(framework: str):
    seen = Seen()
    chat = follow(make(framework, seen, calls=0), seen)
    await chat("hello")
    assert seen.companion_calls() == ["start", "close"]


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_message_steered_while_the_tools_run_is_taken_at_the_next_step(framework: str):
    seen = Seen()
    chat = follow(make(framework, seen, "use Celsius"), seen)
    # The tool steered it: the publish after the results returned it, and the next request had it.
    assert await chat("hello") == "answered after hello, use Celsius"
    assert seen.companion_calls() == ["start", "publish", "publish", "close"]


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_background_agent_publishes_only_its_end(framework: str):
    seen = Seen()
    chat = follow(make(framework, seen, background=True), seen)
    await chat("hello")
    # Its start publishes nothing, nor do its commits: only its close does.
    assert seen.companion_calls() == ["start", "close"]


@pytest.mark.parametrize("framework", list(AGENTS))
@pytest.mark.parametrize("background", [False, True])
async def test_a_handler_of_your_own_on_an_agent_publishes_each_committed_step_as_chat_does(
    framework: str, background: bool
):
    """No turn in the companion (no ``start`` nor ``close``, which only ``chat`` makes), but its steps publish as
    ``chat``'s do, so that the agent's followers see them; a background agent's publish nothing."""
    import importlib

    seen = Seen()
    agent = make(framework, seen, background=background)
    framework_module = importlib.import_module(f"restate.agents.{framework}")

    @agent.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await framework_module.prompt(ctx, text, model=MODELS[framework](seen), tools=[lookup_of(seen)])

    assert await follow(agent, seen, "ask")("hello") == "answered after hello"
    # The reply with the calls (no results yet); the results.
    assert [e for e in seen.events if e[0] != "model"] == ([] if background else [("publish", 0), ("publish", CALLS)])


def no_companion(*_: Any) -> Any:
    raise AssertionError("a session object that is no agent has no companion to call, nor anything to publish")


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_plain_session_objects_prompt_with_tool_calls_stores_its_turn_and_publishes_nothing(
    framework: str, monkeypatch: pytest.MonkeyPatch
):
    """A handler of one's own, on a session object that is no agent, prompts with a framework: a turn of tool calls.

    The conversation stores the turn; nothing publishes, nor looks for a companion, and the handler makes no Restate
    call at all."""
    import importlib

    from sessions_database import Backend
    from sessions_object import Host, request

    from restate.agents import _signals, _turn
    from restate.sessions.internal import invoke

    monkeypatch.setattr(_turn, "publish", no_companion)
    monkeypatch.setattr(_signals, "companion_at", no_companion)
    seen = Seen()
    framework_module = importlib.import_module(f"restate.agents.{framework}")
    desk = sessions.SessionObject("desk")

    @desk.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await framework_module.prompt(ctx, text, model=MODELS[framework](seen), tools=[lookup_of(seen)])

    @desk.handler
    async def stored(ctx: sessions.SessionContext) -> list[Any]:
        turns = await ctx.db.values("SELECT prompt, answer FROM restate_agent_turns")
        steps = await ctx.db.values("SELECT count(*) FROM restate_agent_items")
        return [[list(turn) for turn in turns], steps[0][0]]

    backend = Backend()
    host = Host(backend, service="desk")
    assert await invoke(desk, "ask", host, request(), "hello") == "answered after hello"
    assert host.calls == []
    assert [event for event, _ in seen.events] == ["model", "model"]
    [turns, steps] = await invoke(desk, "stored", Host(backend, service="desk"), request())
    assert [answer for _, answer in turns] == ["answered after hello"]
    # The reply with the calls, their results, and the final reply.
    assert steps >= 3


class _State:
    """A companion's context in this process, for its state only: what it writes, in order."""

    def __init__(self, **state: Any) -> None:
        self.state = dict(state)
        self.writes: list[tuple[str, str]] = []
        self.resolved: list[str] = []

    async def get(self, key: str, type_hint: Any = None) -> Any:
        return self.state.get(key)

    def set(self, key: str, value: Any) -> None:
        self.writes.append(("set", key))
        self.state[key] = value

    def clear(self, key: str) -> None:
        self.writes.append(("clear", key))
        self.state.pop(key, None)

    def resolve_awakeable(self, awakeable: str, value: Any) -> None:
        self.resolved.append(awakeable)


async def test_a_publish_clears_the_watches_only_if_there_are_any():
    from restate.agents import _signals

    quiet = _State(revision=4)
    assert await _signals._publish(typing.cast(Any, quiet)) == {"revision": 5}  # pyright: ignore[reportPrivateUsage]
    # Nobody watched: one write, of the revision.
    assert quiet.writes == [("set", "revision")]

    watched = _State(revision=4, watching=[{"revision": 4, "awakeable": "a1"}])
    await _signals._publish(typing.cast(Any, watched))  # pyright: ignore[reportPrivateUsage]
    assert watched.resolved == ["a1"] and watched.writes == [("set", "revision"), ("clear", "watching")]


@pytest.mark.parametrize("framework", list(AGENTS))
async def test_a_signal_tool_of_a_plain_session_object_fails_its_turn_for_good_and_the_model_never_sees_it(
    framework: str,
):
    """Waiting needs an agent: the turn fails once, terminally, and is closed ``failed``; the model is not asked
    again with the error as the tool's result, as it could not fix how the object is set up."""
    import importlib

    from sessions_database import Backend
    from sessions_object import Host, request

    from restate.agents import wait_for_signal
    from restate.exceptions import TerminalError
    from restate.sessions.internal import invoke

    seen = Seen()
    waits = wait_for_signal("approval", description="Wait for an approval.", name="lookup")
    framework_module = importlib.import_module(f"restate.agents.{framework}")
    desk = sessions.SessionObject("desk")

    @desk.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await framework_module.prompt(ctx, text, model=MODELS[framework](seen), tools=[waits])

    @desk.handler
    async def stored(ctx: sessions.SessionContext) -> list[Any]:
        return [list(row) for row in await ctx.db.values("SELECT status, answer FROM restate_agent_turns")]

    backend = Backend()
    host = Host(backend, service="desk")
    with pytest.raises(TerminalError) as raised:
        await invoke(desk, "ask", host, request(), "refund o1")
    assert raised.value.message.startswith("waiting for signal 'approval' needs an agent"), raised.value.message
    assert [event for event, _ in seen.events] == ["model"] and host.calls == []
    [[status, answer]] = await invoke(desk, "stored", Host(backend, service="desk"), request())
    assert status == "failed" and "needs an agent" in answer
