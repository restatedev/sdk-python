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
"""A Claude agent on a scripted Anthropic client: each point of _agent.py's docstring, and the API's rules."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import copy
import typing
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

pytest.importorskip("anthropic")

from anthropic import AsyncAnthropic  # noqa: E402
from anthropic.types.beta import BetaMessage  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import anthropic, tool  # noqa: E402
from restate.agents._api import Messages  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As the Anthropic SDK names the error of a missing or wrong API key."""


Reply = Callable[[dict[str, Any]], BetaMessage]


def message(*content: dict[str, Any], stop: str = "end_turn") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(content),
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


def text(said: str) -> dict[str, Any]:
    return {"type": "text", "text": said}


def tool_use(call_id: str, name: str, input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": input}


class FakeClient:
    """An ``AsyncAnthropic`` whose ``beta.messages.stream`` answers with ``reply``, and keeps each request."""

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def stream(self, **params: Any) -> "FakeStream":
        self.requests.append(copy.deepcopy(params))
        return FakeStream(self.reply, params)

    def client(self) -> AsyncAnthropic:
        return typing.cast(AsyncAnthropic, self)


class FakeStream:
    def __init__(self, reply: Reply, params: dict[str, Any]) -> None:
        self.reply = reply
        self.params = params

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> BetaMessage:
        return self.reply(self.params)


asked: collections.Counter[str] = collections.Counter()
"""How often the model was asked, by the key word that ends each test's first prompt."""


def _prompts(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m["role"] == "user" and isinstance(m["content"], str)]


def reply(params: dict[str, Any]) -> BetaMessage:
    """The scripted model: it answers the last prompt from the conversation alone, as a retry would ask again."""
    messages: list[dict[str, Any]] = params["messages"]
    prompt = _prompts(messages)[-1]
    asked[_prompts(messages)[0].split(" ")[-1]] += 1
    last = messages[-1]
    results = (
        [b for b in last["content"] if b.get("type") == "tool_result"] if isinstance(last["content"], list) else []
    )
    word, _, rest = prompt.partition(" ")
    if word == "system":
        return message(text(f"system: {params.get('system')}"))
    if word == "refuse":
        raise AuthenticationError("invalid x-api-key")
    if word == "long":
        return message(text("and so on"), stop="max_tokens")
    if word == "full":
        return message(stop="model_context_window_exceeded")
    if word == "novel":
        # As the SDK reads a response: without validating it against the stop reasons it knows.
        return message(text("a reply")).model_copy(update={"stop_reason": "a_stop_reason_of_the_future"})
    if word == "decline":
        return message(stop="refusal")
    if word == "pause":
        if last["role"] == "assistant":
            return message(text(" resumed"))
        return message(text("searching"), stop="pause_turn")
    names = {"weather": "get_weather", "crash": "flaky", "wait": "blocking", "wrong": "get_weather"}
    names |= {"unknown": "nonexistent", "forbidden": "forbidden"}
    if word in names and not results:
        args: dict[str, Any] = {"town": 3} if word == "wrong" else {"city": rest}
        return message(tool_use(f"call-{rest}", names[word], args), stop="tool_use")
    if word == "fallback" and not results:
        # The requested model declined after starting a tool call; the fallback model calls the tool afresh.
        handover = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-x"}}
        return message(
            {"type": "thinking", "thinking": "", "signature": "declined"},
            tool_use("declined", "get_weather", {"city": "nowhere"}),
            {**handover, "trigger": {"type": "refusal"}},
            tool_use(f"call-{rest}", "get_weather", {"city": rest}),
            stop="tool_use",
        )
    if results:
        return message(text(f"tool said: {results[-1]['content']}"))
    return message(text(f"echo: {prompt}"))


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


client = FakeClient(reply)

assistant = anthropic.agent(
    "claude_assistant",
    client=client.client(),
    system="Answer briefly.",
    tools=[get_weather, flaky, blocking, forbidden],
)


@assistant.handler
async def twice(ctx: sessions.SessionContext, said: str) -> list[str]:
    """Two prompts of one invocation: each its own."""
    return [
        await anthropic.prompt(ctx, said, client=client.client()),
        await anthropic.prompt(ctx, f"again {said}", client=client.client()),
    ]


async def tools_of(ctx: sessions.SessionContext) -> list[anthropic.AnyTool]:
    """Each user's tools, as an async function of the context gives them."""
    return [get_weather]


per_user = anthropic.agent(
    "claude_per_user", client=client.client(), system=lambda ctx: f"You help {ctx.key}.", tools=tools_of
)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        sessions.app(assistant, per_user), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/claude_assistant/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, said: str) -> httpx.Response:
    return await post(harness, f"{key}/chat", content=said, headers=TEXT)


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


# --- Against a Restate server ---------------------------------------------------------------------


async def test_a_message_is_answered_with_the_system_prompt_and_kept_in_the_conversation(harness: HarnessEnvironment):
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
    async with httpx.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(
            f"{harness.ingress_url}/claude_assistant/{k}/chat/send", content=f"wait {k}", headers=TEXT
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


async def test_options_may_be_functions_of_the_context(harness: HarnessEnvironment):
    k = key()
    async with httpx.AsyncClient(timeout=60) as ingress:
        url = f"{harness.ingress_url}/claude_per_user/{k}/chat"
        assert (await ingress.post(url, content=f"system {k}", headers=TEXT)).json() == f"system: You help {k}."
        assert (await ingress.post(url, content=f"weather {k}", headers=TEXT)).json() == f"tool said: sunny in {k}"


# --- In process -----------------------------------------------------------------------------------

PUBLISHED = {"publish": b"null"}
"""What the signals object answers when the turn publishes its progress (at each tool call)."""


async def ask(*prompts: str, given: FakeClient, **options: Any) -> list[Any]:
    """Answer each prompt in turn, as one conversation of an object of your own, and return each answer or error."""
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    asker = sessions.SessionObject("asker")

    @asker.handler
    async def answer(ctx: sessions.SessionContext, said: str) -> str:
        return await anthropic.prompt(ctx, said, client=given.client(), **options)

    backend = Backend()
    answers: list[Any] = []
    for said in prompts:
        try:
            answers.append(
                await invoke(asker, "answer", Host(backend, service="asker", answers=PUBLISHED), request(), said)
            )
        except restate.TerminalError as error:
            answers.append(error)
    return answers


async def test_a_request_caches_the_conversation_and_drops_thinking_it_can_no_longer_use():
    given = FakeClient(reply)
    await ask("weather Rome", given=given, effort="high", tools=[get_weather, {"type": "web_search_20260209"}])
    first, second = given.requests
    assert first["model"] == "claude-opus-5-5"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"]
    assert first["output_config"] == {"effort": "high"}
    assert first["tools"] == [
        {
            "name": "get_weather",
            "description": "The current weather in a city.",
            "input_schema": get_weather.input_schema,
        },
        {"type": "web_search_20260209"},
    ]
    # Only appended to: the second request starts with the first's messages, unchanged.
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]


async def test_without_fallbacks_a_request_names_neither_fallbacks_nor_their_beta():
    given = FakeClient(reply)
    assert await ask("hi there", given=given, fallbacks=False, model="anthropic/claude-x") == ["echo: hi there"]
    [request] = given.requests
    assert "fallbacks" not in request and "output_config" not in request
    assert request["betas"] == ["thinking-binding-controls-2026-08-01"]
    assert request["model"] == "claude-x"


async def test_a_fallback_reply_runs_only_the_fallback_models_tools_and_is_sent_back_as_the_api_requires():
    given = FakeClient(reply)
    assert await ask("fallback Rome", given=given, tools=[get_weather]) == ["tool said: sunny in Rome"]
    sent_back = given.requests[1]["messages"][1]
    # Before the handover: no thinking and no tool calls of the model that declined. The marker stays.
    assert [b["type"] for b in sent_back["content"]] == ["fallback", "tool_use"]
    assert sent_back["content"][-1] == tool_use("call-Rome", "get_weather", {"city": "Rome"})
    results = given.requests[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["call-Rome"]


async def test_a_paused_turn_is_sent_back_to_go_on_and_its_text_is_part_of_the_answer():
    given = FakeClient(reply)
    assert await ask("pause now", given=given) == ["searching resumed"]
    assert [m["role"] for m in given.requests[1]["messages"]] == ["user", "assistant"]


async def test_a_declined_prompt_is_answered_as_declined_and_the_next_is_answered():
    given = FakeClient(reply)
    assert await ask("decline this", "hi there", given=given) == ["The model declined to answer.", "echo: hi there"]


async def test_a_reply_cut_off_at_max_tokens_fails_the_prompt_for_good_and_the_next_is_answered():
    given = FakeClient(reply)
    cut_off, answered = await ask("long story", "hi there", given=given, max_tokens=10)
    assert isinstance(cut_off, restate.TerminalError) and "max_tokens=10" in cut_off.message
    assert answered == "echo: hi there"
    # The failed run was closed with its error, so the next request alternates roles as the API requires.
    assert [m["role"] for m in given.requests[-1]["messages"]] == ["user", "assistant", "user"]


async def test_a_full_context_window_fails_the_prompt_for_good_and_a_new_stop_reason_ends_the_run():
    given = FakeClient(reply)
    full, novel = await ask("full now", "novel stop", given=given)
    assert isinstance(full, restate.TerminalError) and "context window" in full.message
    assert novel == "a reply"
    assert len(given.requests) == 2


async def test_an_answer_stored_before_a_crash_is_not_asked_for_again(monkeypatch: pytest.MonkeyPatch):
    from sessions_database import Backend  # pylint: disable=import-error,import-outside-toplevel
    from sessions_object import Host, request  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents.anthropic import _agent  # pylint: disable=import-outside-toplevel
    from restate.sessions.internal import invoke  # pylint: disable=import-outside-toplevel

    given = FakeClient(lambda params: message(text(f"answer {len(given.requests)}")))
    append = _agent._Conversation.append  # pyright: ignore[reportPrivateUsage]
    crashed: list[bool] = []

    async def append_then_crash_once(self: Any, history: Any, *added: Any) -> Any:
        appended = await append(self, history, *added)
        if added[-1].role == "assistant" and not crashed:
            crashed.append(True)
            raise RuntimeError("the process crashed after storing the answer")
        return appended

    monkeypatch.setattr(_agent._Conversation, "append", append_then_crash_once)  # pyright: ignore[reportPrivateUsage]
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def answer(ctx: sessions.SessionContext, said: str) -> str:
        return await anthropic.prompt(ctx, said, client=given.client())

    backend, invocation = Backend(), request()
    with pytest.raises(RuntimeError, match="crashed after storing"):
        await invoke(asker, "answer", Host(backend, service="asker", answers=PUBLISHED), invocation, "hi")
    # The retry (the same invocation) returns the answer that was stored, without asking the model again.
    assert (
        await invoke(asker, "answer", Host(backend, service="asker", answers=PUBLISHED), invocation, "hi") == "answer 1"
    )
    assert len(given.requests) == 1


def test_a_model_of_another_provider_is_refused():
    with pytest.raises(restate.TerminalError, match="anthropic"):
        anthropic.agent("other", model="openai/gpt-6-sol")
