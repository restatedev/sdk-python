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
"""The stores of Pydantic AI Harness's capabilities over the key's database, without a server.

Each test runs an agent's ``chat`` in this process, turn after turn (see
``agents_pydantic_ai_snapshots``), on a model that calls the harness's tools.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring,import-error

from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from agents_pydantic_ai_snapshots import ANSWERS, Chat, Script, until_snapshot  # noqa: E402
from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai_harness import ConversationSearch, Planning, ToolOutputLimits  # noqa: E402
from sessions_object import Host, request  # noqa: E402

from restate.agents import pydantic_ai  # noqa: E402
from restate.sessions.internal import invoke  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


def _prompt(messages: list[ModelMessage]) -> str | None:
    last = messages[-1]
    prompts = (
        [str(p.content) for p in last.parts if isinstance(p, UserPromptPart)] if isinstance(last, ModelRequest) else []
    )
    return prompts[-1] if prompts else None


def _returned(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    last = messages[-1]
    return [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []


def calls(script: dict[str, tuple[str, dict[str, Any]]]) -> FunctionModel:
    """A model that answers a prompt with the tool call ``script`` gives for its first word, then with the result."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if (returned := _returned(messages)) and not _prompt(messages):
            return ModelResponse(parts=[TextPart(f"tool said: {returned[-1].model_response_str()}")])
        prompt = _prompt(messages) or ""
        name, args = script[prompt.split(" ")[0]]
        return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"call-{prompt.replace(' ', '-')}")])

    return FunctionModel(model)


class CrashAfterTool(AbstractCapability[Any]):
    """Crashes the attempt once, after a tool of ``name`` ran and before its result is stored."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.crashed = False

    async def after_tool_execute(
        self, ctx: RunContext[Any], *, call: Any, tool_def: Any, args: Any, result: Any
    ) -> Any:
        if call.tool_name == self.name and not self.crashed:
            self.crashed = True
            raise RuntimeError("the process crashed after the tool ran")
        return result


async def _spills(chat: Chat) -> int:
    from restate.agents.pydantic_ai._stores import SPILLS  # pylint: disable=import-outside-toplevel

    @chat.obj.handler(name="spills")
    async def spills(ctx: Any, _: Any = None) -> int:
        [[count]] = await ctx.db.values(f"SELECT COUNT(*) FROM {SPILLS}")
        return int(count)

    return await invoke(chat.obj, "spills", chat.host(), request(), None)


async def _history(chat: Chat, run_id: str) -> list[ModelMessage]:
    source = pydantic_ai.history_source()

    @chat.obj.handler(name="history")
    async def history(ctx: Any, run: str) -> list[ModelMessage]:
        from restate.agents._tools import tools_in  # pylint: disable=import-outside-toplevel

        with tools_in(ctx):
            return await source.run_history(run_id=run)

    return await invoke(chat.obj, "history", chat.host(), request(), run_id)


async def clear(chat: Chat) -> None:
    host = Host(chat.backend, service=chat.name, answers={**ANSWERS, "reset": b"null"})
    await invoke(chat.obj, "erase", host, request())


async def test_a_plan_written_in_a_turn_is_read_in_the_next_and_clear_resets_it():
    model = calls(
        {
            "plan": ("write_plan", {"items": [{"content": "Pack"}, {"content": "Travel"}]}),
            "read": ("read_plan", {}),
        }
    )
    mine: Any = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store())])
    chat = Chat(pydantic_ai.agent("pai_plans", agent=mine))
    await chat("plan the trip")
    read = await chat("read it")
    assert "Pack" in read and "Travel" in read
    await clear(chat)
    assert "Pack" not in await chat("read again")


async def test_a_step_added_by_a_tool_call_that_runs_again_is_added_once():
    crash = CrashAfterTool("add_task")
    model = calls({"add": ("add_task", {"content": "Pack"}), "read": ("read_plan", {})})
    mine: Any = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store()), crash])
    chat = Chat(pydantic_ai.agent("pai_plans_once", agent=mine))
    invocation = request()
    with pytest.raises(RuntimeError, match="crashed after the tool ran"):
        await chat("add a step", invocation=invocation)
    await chat("add a step", invocation=invocation)
    assert crash.crashed
    assert (await chat("read it")).count("Pack") == 1


async def test_a_status_change_that_runs_again_after_some_of_its_writes_makes_the_rest(
    monkeypatch: pytest.MonkeyPatch,
):
    """Completing A unblocks B and C, one write each: a crash after B's leaves C's to the call that runs again,
    which reads the plan as it did the first time (not with B unblocked), and so makes C's write."""
    from pydantic_ai_harness.planning import PlanItem, TaskStatus  # pylint: disable=import-outside-toplevel

    from restate.agents._tools import tools_in  # pylint: disable=import-outside-toplevel
    from restate.agents.pydantic_ai import _stores  # pylint: disable=import-outside-toplevel

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Planning reminds the model of the plan in a prompt of its own: answer by the results alone.
        if returned := _returned(messages):
            return ModelResponse(parts=[TextPart(f"tool said: {returned[-1].model_response_str()}")])
        args = {"task_id": "A", "status": "completed"}
        return ModelResponse(parts=[ToolCallPart("update_task_status", args, tool_call_id="call-done")])

    planning = Planning(store=pydantic_ai.plan_store(), enable_subtasks=True)
    mine: Any = Agent(FunctionModel(model), capabilities=[planning])
    chat = Chat(pydantic_ai.agent("pai_plans_unblocked", agent=mine))
    store = _stores.KeyPlanStore()

    @chat.obj.handler(name="plan")
    async def plan(ctx: Any, items: list[dict[str, Any]] | None = None) -> list[str]:
        with tools_in(ctx):
            if items is not None:
                await store.set_items([PlanItem.model_validate(item) for item in items])
            return [f"{item.id}={item.status.value}" for item in await store.get_items()]

    blocked: dict[str, Any] = {"status": TaskStatus.blocked, "depends_on": ["A"]}
    seeded: list[dict[str, Any]] = [{"id": "A", "content": "A", "status": TaskStatus.in_progress}]
    seeded += [{"id": "B", "content": "B", **blocked}, {"id": "C", "content": "C", **blocked}]
    await invoke(chat.obj, "plan", chat.host(), request(), seeded)
    update = _stores.KeyPlanStore.update_item
    written: list[str] = []

    async def crashing(self: Any, item_id: str, **changes: Any) -> Any:
        updated = await update(self, item_id, **changes)  # committed
        written.append(item_id)
        if written == ["A", "B"]:
            raise RuntimeError("the process crashed between two writes")
        return updated

    monkeypatch.setattr(_stores.KeyPlanStore, "update_item", crashing)
    invocation = request()
    with pytest.raises(RuntimeError, match="crashed between two writes"):
        await chat("done with A", invocation=invocation)
    assert await invoke(chat.obj, "plan", chat.host(), request(), None) == ["A=completed", "B=pending", "C=blocked"]
    await chat("done with A", invocation=invocation)
    assert await invoke(chat.obj, "plan", chat.host(), request(), None) == ["A=completed", "B=pending", "C=pending"]
    # The call that ran again got the results of the writes it had made: it made only C's.
    assert written == ["A", "B", "A", "B", "C"]


async def test_a_large_tool_result_spills_and_is_read_in_the_same_turn_and_a_later_one():
    big = "x" * 30_000
    spilled: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _prompt(messages)
        returned = _returned(messages)
        if prompt and prompt.startswith("fetch"):
            return ModelResponse(parts=[ToolCallPart("fetch", {}, tool_call_id="call-fetch")])
        if prompt and prompt.startswith("again"):
            return ModelResponse(
                parts=[ToolCallPart("read_tool_result", {"handle": spilled[0]}, tool_call_id="call-2")]
            )
        result = returned[-1]
        if result.tool_name == "fetch":
            spilled.append(str((result.metadata or {}).get("handle") or _handle(result.model_response_str())))
            return ModelResponse(
                parts=[ToolCallPart("read_tool_result", {"handle": spilled[0]}, tool_call_id="call-1")]
            )
        return ModelResponse(parts=[TextPart(f"read {len(result.model_response_str())}")])

    fetched: list[int] = []

    def fetch() -> str:
        """Fetch a large page."""
        fetched.append(1)
        return big

    # The crash comes after the spill: the tool runs again, and spills again, over its own spill.
    crash = CrashAfterTool("fetch")
    limits = ToolOutputLimits(store=pydantic_ai.overflow_store())
    mine: Any = Agent(FunctionModel(model), tools=[fetch], capabilities=[crash, limits])
    chat = Chat(pydantic_ai.agent("pai_spills", agent=mine))
    invocation = request()
    with pytest.raises(RuntimeError, match="crashed after the tool ran"):
        await chat("fetch the page", invocation=invocation)
    first = await chat("fetch the page", invocation=invocation)
    assert spilled and first.startswith("read ")
    later = await chat("again the page")
    assert later == first and len(fetched) == 2
    assert await _spills(chat) == 1


def _handle(text: str) -> str:
    """The handle in the preview the model gets for a spilled result."""
    for word in text.replace("`", " ").replace('"', " ").split():
        if ":" in word and "/" not in word and word.count(".") >= 1 and "call-fetch" in word:
            return word.strip(".,'()")
    raise AssertionError(f"no handle in {text[:500]!r}")


async def test_search_finds_a_tool_result_that_a_compaction_summarized_and_lists_the_runs_in_order():
    script = Script()
    source = pydantic_ai.history_source()
    found: list[str] = []
    runs: list[Any] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _prompt(messages)
        if prompt == "search fact":
            return ModelResponse(
                parts=[ToolCallPart("search_conversation_history", {"query": "found q0"}, tool_call_id="call-search")]
            )
        returned = _returned(messages)
        if returned and returned[-1].tool_name == "search_conversation_history":
            found.append(returned[-1].model_response_str())
            return ModelResponse(parts=[TextPart("searched")])
        return script(messages, info)

    class Runs(AbstractCapability[Any]):
        async def before_model_request(self, ctx: RunContext[Any], request_context: Any) -> Any:
            runs[:] = await source.list_runs()
            return request_context

    def lookup(about: str) -> str:
        """Look something up."""
        return f"found {about}: {'fact ' * 50}"

    search = ConversationSearch(source, scope="conversation")
    mine: Any = Agent(FunctionModel(model), tools=[lookup], capabilities=[search, Runs()])
    chat = Chat(pydantic_ai.agent("pai_search", agent=mine, context_window=8000))
    turns = await until_snapshot(chat)
    stored = "".join((await chat.read())["items"])
    assert "found q0" not in stored  # compacted away from the model's history
    assert await chat("search fact") == "searched"
    # The tool's result, as the turn stored it (the search's own call has the query too).
    assert found and "Tool [lookup]: found q0: fact" in found[0]
    assert len(runs) == turns + 1 and len({run.run_id for run in runs}) == len(runs)
    assert [run.started_at for run in runs] == sorted(run.started_at for run in runs)
    assert {run.conversation_id for run in runs} == {"pai_search/alice"}
    # Each run's messages are its turn's, as stored: the search's own run has its prompt first.
    history = await _history(chat, runs[-1].run_id)
    assert _prompt(history[:1]) == "search fact"


async def test_a_tool_call_that_only_reads_the_plan_commits_nothing(monkeypatch: pytest.MonkeyPatch):
    """A read's journal row is needed only if a write follows in the same tool call: it is written with that write."""
    from restate.agents.pydantic_ai import _stores  # pylint: disable=import-outside-toplevel

    model = calls(
        {
            "plan": ("write_plan", {"items": [{"content": "Pack"}, {"content": "Travel"}]}),
            "read": ("read_plan", {}),
        }
    )
    mine: Any = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store())])
    chat = Chat(pydantic_ai.agent("pai_plans_read", agent=mine))
    await chat("plan the trip")

    @chat.obj.handler(name="journal")
    async def journal(ctx: Any, _: Any = None) -> int:
        [[count]] = await ctx.db.values(f"SELECT COUNT(*) FROM {_stores.JOURNAL}")
        return int(count)

    journaled = await invoke(chat.obj, "journal", chat.host(), request(), None)
    syncs: list[bool] = []
    reading: list[bool] = []
    sync, get_items = Host.sync, _stores.KeyPlanStore.get_items

    async def counted(self: Any) -> None:
        syncs.extend(reading)
        await sync(self)

    async def read(self: Any) -> Any:
        reading.append(True)
        try:
            return await get_items(self)
        finally:
            reading.clear()

    monkeypatch.setattr(Host, "sync", counted)
    monkeypatch.setattr(_stores.KeyPlanStore, "get_items", read)
    answer = await chat("read it")
    assert "Pack" in answer and "Travel" in answer
    # No commit while the plan was read, and no journal row for the read.
    assert syncs == []
    assert await invoke(chat.obj, "journal", chat.host(), request(), None) == journaled
