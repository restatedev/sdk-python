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
"""Prebuilt LangGraph nodes that are Restate steps: wait for a signal, run a workflow, call, send, ask another agent.

Add them to a graph of your own, as the tools of ``restate.agents`` are added
to an agent. Each one reads its input from the graph's state, and writes what
it gets back to the state field ``output``::

    from restate.agents.langgraph import SqlCheckpointSaver, nodes

    graph = StateGraph(State)
    graph.add_node("approve", nodes.wait_for_signal("approval", input=lambda s: s["refund"], output="approved"))
    graph.add_node("ship", nodes.workflow(shipping.run, input=lambda s: s["order"], output="receipt"))

The simplest is an agent whose ``chat`` runs the graph, built on LangGraph's
``MessagesState``; it sets everything below up::

    desk = langgraph.agent("desk", graph=graph)

Or run the graph in a handler of an agent (an ``AgentObject``) yourself, with
its context as LangGraph's run context, a checkpointer in its database, and
``durability="sync"`` (each step's checkpoint stored before the step runs)::

    desk = AgentObject("desk")


    @desk.handler()
    async def refund(ctx: sessions.SessionContext, refund: Refund) -> State:
        app: Any = graph.compile(checkpointer=SqlCheckpointSaver(ctx.db))  # Any: LangGraph types no Restate context
        config: RunnableConfig = {"configurable": {"thread_id": "refunds"}}
        resuming = (await app.aget_state(config)).next
        return await app.ainvoke(None if resuming else {"refund": refund}, config, context=ctx, durability="sync")

Each node does its step once. A crash re-runs a node from the checkpoint it
started from, which ``durability="sync"`` stored before the node ran, and
LangGraph gives it the same task id there, so the node
joins the workflow, the agent's answer or the wait it started before rather
than start another. A wait is listed by the agent's ``pending`` view, and its
``signal`` view resolves it.
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from langgraph.config import get_config
from langgraph.runtime import Runtime
from pydantic import PydanticSchemaGenerationError, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from restate.agents import _signals
from restate.agents._tools import handler_of, input_type_of, inputs_of, keyed, untyped
from restate.exceptions import TerminalError
from restate.sessions import SessionContext


class Node(typing.Protocol):
    """A LangGraph node: it takes the state and the run's runtime, and returns the state's update."""

    def __call__(self, state: Any, *, runtime: Runtime[Any]) -> Awaitable[dict[str, Any]]: ...


_TASK_ID = "__pregel_task_id"
_CHECKPOINTER = "__pregel_checkpointer"
_DURABILITY = "__pregel_durability"


class _Step(typing.NamedTuple):
    """A node's run: the agent's context, the node's name, and the key that is the same each time it re-runs."""

    ctx: SessionContext
    node: str
    idempotency_key: str


def _step(runtime: Runtime[Any], kind: str) -> _Step:
    """Return the run of a node of ``kind``, from LangGraph's runtime and config."""
    ctx = typing.cast("SessionContext | None", runtime.context)
    if ctx is None:
        raise TypeError(f"nodes.{kind} runs in a graph invoked with the handler's context: ainvoke(..., context=ctx)")
    configurable = get_config().get("configurable", {})
    if configurable.get(_CHECKPOINTER) is None:
        raise TypeError(
            f"nodes.{kind} runs in a graph with a checkpointer, so that a retried attempt resumes it: "
            "compile(checkpointer=SqlCheckpointSaver(ctx.db))"
        )
    # Only "sync" stores a step's checkpoint before its tasks run. Otherwise a crash before the save makes the
    # resumed run create the checkpoint again, with a new id, and so new task ids: the step would be made twice.
    if configurable.get(_DURABILITY) != "sync":
        raise TypeError(
            f"nodes.{kind} runs in a graph that stores each step's checkpoint before the step runs, so that a "
            'retried attempt gets the same task ids: ainvoke(..., durability="sync")'
        )
    # The task's namespace holds its id, which LangGraph derives from the checkpoint the step started from:
    # a node re-run from that checkpoint gets the same one. It holds the subgraph's path too.
    namespace = typing.cast(str, configurable.get("checkpoint_ns") or configurable[_TASK_ID])
    node = namespace.rsplit("|", 1)[-1].split(":", 1)[0]
    return _Step(ctx, node, f"langgraph:{ctx.key}:{namespace}")


def _validator(input_type: Any, name: str) -> Callable[[Any], Any]:
    """Return what makes a value from the state (a dict, say) a value of a handler's ``input_type``.

    Graph state is plain data, while a handler may take a Pydantic model, a
    dataclass or a ``TypedDict``: the value is validated into it, and one of
    that type already is passed through. A value that does not validate fails
    the node with a terminal error, as a retry would read the same state.
    """
    if input_type is None or input_type is Any:
        return lambda value: value
    try:
        adapter: TypeAdapter[Any] = TypeAdapter(input_type)
    except PydanticSchemaGenerationError:
        # A type Pydantic does not know, such as a msgspec Struct: the handler's serde takes the value as it is.
        return lambda value: value

    def validated(value: Any) -> Any:
        try:
            return adapter.validate_python(value)
        except ValidationError as error:
            raise TerminalError(f"the input of {name} is not valid: {error}") from error

    return validated


class _Target(typing.NamedTuple):
    """A handler a node calls: its definition, its name for errors, and what validates its input."""

    handler: Any
    name: str
    validated: Callable[[Any], Any]


def _target(target: Callable[..., Any], input: object, key: object, *, keys: bool) -> _Target:
    """Check a node's ``target`` against its ``input`` and ``key``, and return it.

    ``input`` must be given exactly when the handler takes one; with ``keys``,
    ``key`` must be given exactly when the handler is keyed (a virtual
    object's or a workflow's).
    """
    handler = handler_of(target)
    input_type = input_type_of(handler)
    name = f"{handler.service_tag.name}.{handler.name}"
    takes_input = input_type is not None
    if takes_input != (input is not None):
        raise TypeError(
            f"{name} {'takes' if takes_input else 'takes no'} input: {'pass' if takes_input else 'drop'} input="
        )
    if keys and keyed(handler) != (key is not None):
        raise TypeError(
            f"{name} {'is' if keyed(handler) else 'is not'} keyed: {'pass' if keyed(handler) else 'drop'} key="
        )
    return _Target(handler, name, _validator(input_type, name))


def _update(output: str | None, value: object) -> dict[str, Any]:
    return {output: value} if output is not None else {}


def wait_for_signal(
    signal: str,
    *,
    output: str | None = None,
    input: Callable[[Any], Any] | None = None,
) -> Node:
    """Create a node that waits for a signal to the agent, and writes its value to ``output``.

    A person's approval, say, or another system's result. The graph waits
    without holding a process, for as long as it takes. The agent's
    ``pending`` view lists the wait, with ``input``'s value, and its
    ``signal`` view resolves it::

        graph.add_node("approve", nodes.wait_for_signal("approval", input=lambda s: s["refund"], output="approved"))

    Args:
        signal: The signal's name.
        output: The state field that gets the signal's value. None to write
            nothing.
        input: What the graph waits for (such as the refund to approve), from
            the state, for ``pending`` to show. None to show nothing.

    Returns:
        The node, for ``StateGraph.add_node``.
    """

    async def node(state: Any, *, runtime: Runtime[Any]) -> dict[str, Any]:
        step = _step(runtime, "wait_for_signal")
        # The wait's id starts with the invocation's (the same in each attempt), as the waits of tools do, so that a
        # turn that ends without it (killed, say) has it closed: the companion closes the waits whose ids start
        # with the turn's.
        wait_id = f"{step.ctx.request.id}:{step.idempotency_key}"
        request: _signals.WaitRequest = {"id": wait_id, "name": signal}
        if input is not None:
            # As JSON, which the companion stores: the state may hold models (a workflow's result, say).
            request["input"] = to_jsonable_python(input(state))
        value = await step.ctx.call(_signals.wait, request, key=_signals.signals_of(step.ctx), idempotency_key=wait_id)
        return _update(output, value)

    return node


def workflow(
    target: Callable[..., Any],
    *,
    output: str | None = None,
    input: Callable[[Any], Any] | None = None,
    key: Callable[[Any], str] | None = None,
) -> Node:
    """Create a node that runs a workflow to completion, and writes its result to ``output``::

        graph.add_node("ship", nodes.workflow(shipping.run, input=lambda s: s["order"], output="receipt"))

    Args:
        target: The workflow's main handler.
        output: The state field that gets the workflow's result. None to
            write nothing.
        input: The workflow's input, from the state. Required if the handler
            takes one. Plain data (a dict, say) is validated into the
            handler's input type, such as a Pydantic model.
        key: The workflow id, from the state. Defaults to one of the node's
            own, made of the agent's key and the node's task.

    Returns:
        The node, for ``StateGraph.add_node``.

    Raises:
        TypeError: If ``target`` is not a handler, or it takes an input and
            ``input`` is not given (or the other way around).

    When it runs, the node fails with a terminal error if the input does not
    validate.
    """
    called = _target(target, input, key, keys=False)

    async def node(state: Any, *, runtime: Runtime[Any]) -> dict[str, Any]:
        step = _step(runtime, "workflow")
        workflow_id = key(state) if key is not None else step.idempotency_key
        value = called.validated(input(state)) if input is not None else None
        result = await untyped(step.ctx).call(
            target, *inputs_of(called.handler, value), key=workflow_id, idempotency_key=step.idempotency_key
        )
        return _update(output, result)

    return node


def call(
    target: Callable[..., Any],
    *,
    output: str | None = None,
    input: Callable[[Any], Any] | None = None,
    key: Callable[[Any], str] | None = None,
) -> Node:
    """Create a node that calls a handler, and writes its result to ``output``::

        graph.add_node("status", nodes.call(orders.status, input=lambda s: s["order"], output="status"))

    Args:
        target: A handler of a service, a virtual object or a workflow.
        output: The state field that gets the handler's result. None to
            write nothing.
        input: The handler's input, from the state. Required if the handler
            takes one. Plain data (a dict, say) is validated into the
            handler's input type, such as a Pydantic model.
        key: The key to call, from the state. Required for a virtual
            object's or a workflow's handler, and only for those.

    Returns:
        The node, for ``StateGraph.add_node``.

    Raises:
        TypeError: If ``target`` is not a handler, or ``input`` or ``key``
            is given when the handler takes none (or the other way around).

    When it runs, the node fails with a terminal error if the input does not
    validate.
    """
    called = _target(target, input, key, keys=True)

    async def node(state: Any, *, runtime: Runtime[Any]) -> dict[str, Any]:
        step = _step(runtime, "call")
        value = called.validated(input(state)) if input is not None else None
        result = await untyped(step.ctx).call(
            target,
            *inputs_of(called.handler, value),
            key=key(state) if key is not None else None,
            idempotency_key=step.idempotency_key,
        )
        return _update(output, result)

    return node


def send(
    target: Callable[..., Any],
    *,
    input: Callable[[Any], Any] | None = None,
    key: Callable[[Any], str] | None = None,
    delay: timedelta | None = None,
) -> Node:
    """Create a node that sends to a handler, without waiting for it to run::

        graph.add_node("remind", nodes.send(checkins.remind, input=lambda s: s["checkin"], delay=timedelta(days=1)))

    Args:
        target: A handler of a service, a virtual object or a workflow.
        input: The handler's input, from the state. Required if the handler
            takes one. Plain data (a dict, say) is validated into the
            handler's input type, such as a Pydantic model.
        key: The key to send to, from the state. Required for a virtual
            object's or a workflow's handler, and only for those.
        delay: How long to wait before the handler runs.

    Returns:
        The node, for ``StateGraph.add_node``. It writes nothing to the state.

    Raises:
        TypeError: If ``target`` is not a handler, or ``input`` or ``key``
            is given when the handler takes none (or the other way around).

    When it runs, the node fails with a terminal error if the input does not
    validate.
    """
    called = _target(target, input, key, keys=True)

    async def node(state: Any, *, runtime: Runtime[Any]) -> dict[str, Any]:
        step = _step(runtime, "send")
        value = called.validated(input(state)) if input is not None else None
        untyped(step.ctx).send(
            target,
            *inputs_of(called.handler, value),
            key=key(state) if key is not None else None,
            delay=delay,
            idempotency_key=step.idempotency_key,
        )
        return {}

    return node


def subagent(
    agent: object,
    *,
    message: Callable[[Any], str],
    output: str | None = None,
    key: Callable[[Any], str] | None = None,
    handler: str = "chat",
) -> Node:
    """Create a node that sends a message to another agent, and writes its answer to ``output``::

        graph.add_node("research", nodes.subagent(researcher, message=lambda s: s["question"], output="findings"))

    By default the message goes to the agent's key ``<this key>.<node name>``,
    so each run of the graph continues the same conversation with it.

    Args:
        agent: An agent, or any virtual object with a handler that takes text.
        message: The message, from the state.
        output: The state field that gets the agent's answer. None to write
            nothing.
        key: The agent's key, from the state. Defaults to
            ``<this key>.<node name>``.
        handler: The handler that takes the message.

    Returns:
        The node, for ``StateGraph.add_node``.
    """
    agent_name = typing.cast(str, getattr(agent, "name"))  # noqa: B009 (an SDK's definition)

    async def node(state: Any, *, runtime: Runtime[Any]) -> dict[str, Any]:
        step = _step(runtime, "subagent")
        agent_key = key(state) if key is not None else f"{step.ctx.key}.{step.node}"
        answer = await step.ctx.generic_call(
            agent_name, handler, message(state), key=agent_key, idempotency_key=step.idempotency_key
        )
        return _update(output, answer)

    return node


__all__ = ["Node", "call", "send", "subagent", "wait_for_signal", "workflow"]
