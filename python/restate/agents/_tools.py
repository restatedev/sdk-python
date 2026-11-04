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
"""
Tools for any of the frameworks: defined once with ``tool``, given to any
agent's ``tools``, which turns them into the framework's own. And the tools
that are Restate itself: calling a handler, sending to one, running a workflow,
handing a task to another agent, and waiting for a signal. As sdk-sessions'
``tools.ts``.

A tool runs with the agent's context, and with the id of the model's tool
call: Restate calls carry an idempotency key made of the invocation id and that
id, so a tool call run again after a crash makes the same call, once.

A tool's input schema comes from type hints, by pydantic, imported when a tool
is made: an agent framework's extra brings it.
"""

from __future__ import annotations

import inspect
import json
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from restate.agents import _signals
from restate.agents._calls import call_of
from restate.agents._common import PerSession, ToolError, configured
from restate.agents._turn import stop_signal_of, tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.handler import Handler, handler_from_callable
from restate.sessions import SessionContext

T = typing.TypeVar("T")

INJECTED = ("ctx", "call_id", "idempotency_key")
"""The parameters a tool's function gets from where it runs, rather than from the model."""


def _pydantic() -> Any:
    """pydantic, which tools take their inputs' schemas from."""
    try:
        import pydantic
    except ImportError as error:
        raise ImportError(
            "restate.agents' tools take their inputs' schemas from pydantic: install pydantic, or a framework's "
            "extra that includes it, such as restate-sdk[pydantic_ai] or restate-sdk[langgraph]"
        ) from error
    return pydantic


@dataclass(frozen=True)
class ToolCall:
    """A tool call: the agent's context, and the id of the model's call (the same when a crash runs it again)."""

    ctx: SessionContext
    call_id: str

    @property
    def idempotency_key(self) -> str:
        """
        The tool call's idempotency key, ``<invocation id>:<call id>``: for a
        call the tool makes once, however often it runs (a Restate call's, or a
        payment API's).
        """
        return f"{self.ctx.request.id}:{self.call_id}"


@dataclass(frozen=True)
class Tool:
    """
    A tool of any framework: its ``name`` and ``description`` for the model,
    its input's JSON schema, which a framework offers the model, and how it
    runs: ``run(args, call)``, its result as text for the model, of the
    model's arguments as they came (arguments that do not fit the input answer
    with what is wrong, for the model to call it again).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    _run: Callable[[dict[str, Any], ToolCall], Awaitable[Any]]
    _validate: Callable[[dict[str, Any]], Any]

    async def run(self, args: object, call: ToolCall) -> str:
        """The tool's result for the model, as text: see ``tool`` for how it fails."""
        try:
            arguments = self._validate(typing.cast(dict[str, Any], args if isinstance(args, dict) else {}))
        except Exception as error:  # pydantic's ValidationError: the model's to fix
            return f"Error: the arguments do not fit the tool's input: {error}"
        try:
            return _text(await self._run(arguments, call))
        except BaseException as error:
            # The turn stopped (its invocation was cancelled, or its attempt
            # ended): it ends, rather than the model going on with the tool's
            # error as its answer (such as a call the cancellation cancelled).
            stop = stop_signal_of(call.ctx)
            if stop is not None and stop.reason is not None:
                raise stop.reason from error
            if isinstance(error, TerminalError):
                # A terminal error is the tool's answer, which the model can act on.
                return f"Error: {error.message}"
            if not isinstance(error, Exception):
                raise
            # Any other error fails the attempt, which Restate retries.
            raise ToolError(self.name, error) from error


async def run_tool(tool: Tool, args: object, call: ToolCall) -> str:
    """
    Run ``tool`` for tool call ``call``, as a framework's integration runs one:
    the conversation publishes its start and its end, for the clients that
    follow it, and the calls whose results are stored are settled first.
    """
    await tool_call_started(call.ctx)
    try:
        return await tool.run(args, call)
    finally:
        await tool_call_ended(call.ctx)


def _text(value: object) -> str:
    """A result as text: a string as it is, nothing as "done", anything else as JSON."""
    if isinstance(value, str):
        return value
    if value is None:
        return "done"
    try:
        return json.dumps(value)
    except TypeError:
        return typing.cast(bytes, _pydantic().TypeAdapter(type(value)).dump_json(value)).decode()


# --- tool: a function of your own -----------------------------------------------------------------


@typing.overload
def tool(fn: Callable[..., Any], /, *, name: str | None = None, description: str | None = None) -> Tool: ...


@typing.overload
def tool(*, name: str | None = None, description: str | None = None) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None, *, name: str | None = None, description: str | None = None
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """
    A tool, from a function: the model gives its parameters, described by
    their types and the docstring::

        @tool
        async def get_weather(city: str) -> str:
            '''The current weather in a city.'''
            return f"Sunny in {city}"

    Three parameters are not the model's: ``ctx``, the agent's context
    (``ctx.db``, calls, sends); ``call_id``, the tool call's id; and
    ``idempotency_key``, made of both, for what the tool must do once. A crash
    runs an unfinished tool call again with the same id. A
    ``restate.TerminalError`` the function raises (or a Restate call it makes
    fails with) is its answer, ``Error: <message>``, for the model to act on;
    any other error fails the attempt, which Restate retries.
    """
    if fn is None:

        def decorate(fn: Callable[..., Any]) -> Tool:
            return tool(fn, name=name, description=description)

        return decorate
    pydantic = _pydantic()
    parameters = inspect.signature(fn, eval_str=True).parameters.values()
    fields: dict[str, Any] = {
        p.name: (p.annotation if p.annotation is not p.empty else Any, ... if p.default is p.empty else p.default)
        for p in parameters
        if p.name not in INJECTED
    }
    model = pydantic.create_model(f"{fn.__name__}_input", **fields)
    injected = [p.name for p in parameters if p.name in INJECTED]

    async def run(arguments: dict[str, Any], call: ToolCall) -> Any:
        values = {"ctx": call.ctx, "call_id": call.call_id, "idempotency_key": call.idempotency_key}
        result = fn(**arguments, **{name: values[name] for name in injected})
        return await result if inspect.isawaitable(result) else result

    def validate(args: dict[str, Any]) -> dict[str, Any]:
        validated = model.model_validate(args)
        return {field: getattr(validated, field) for field in fields}

    return Tool(
        name=name or fn.__name__,
        description=description or inspect.getdoc(fn) or "",
        input_schema=typing.cast(dict[str, Any], model.model_json_schema()),
        _run=run,
        _validate=validate,
    )


# --- The tools that are Restate itself ------------------------------------------------------------


async def _once(name: str, input: object, call: ToolCall, body: Callable[[ToolCall], Awaitable[T]]) -> T:
    """
    ``body``, a Restate call of tool ``name`` with ``input``, made once however
    often the tool call runs (see _calls.py): it gets the id of the call, which
    an earlier attempt may have made, and so its idempotency key.
    """
    made = await call_of(call.ctx, name, input, call.call_id)
    try:
        result = await body(ToolCall(call.ctx, made.id))
    except TerminalError as error:
        # A terminal error answers the call; any other fails the attempt, and the retry makes it again.
        await made.answered(f"Error: {error.message}")
        raise
    await made.answered()
    return result


def _untyped(ctx: SessionContext) -> Any:
    """``ctx``, to call and send to a handler whose input a tool knows only at run time."""
    return ctx


def _inputs(handler: Handler[Any, Any], value: Any) -> tuple[Any, ...]:
    """The positional input of a call of ``handler``: ``value``, if it takes an input."""
    return (value,) if handler.arity >= 2 else ()


def _handler(target: Callable[..., Any]) -> Handler[Any, Any]:
    try:
        return handler_from_callable(target)
    except ValueError:
        raise TypeError(f"{target!r} is not a handler of a service, an object or a workflow") from None


def _input_type(handler: Handler[Any, Any]) -> Any:
    """The type of a handler's input, or None if it takes none."""
    if handler.arity < 2:
        return None
    parameters = list(inspect.signature(handler.fn, eval_str=True).parameters.values())
    annotation = parameters[1].annotation
    return Any if annotation is inspect.Parameter.empty else annotation


@dataclass(frozen=True)
class _Input:
    """A Restate tool's input: the handler's (flattened if an object), and the key if the model chooses it."""

    schema: dict[str, Any]
    validate: Callable[[dict[str, Any]], tuple[str | None, Any]]


def _input_of(input_type: Any, model_keys: str | None) -> _Input:
    """
    The tool's input: ``input_type``'s, with ``key`` beside it if the model
    chooses the key of ``model_keys`` (an object's, a workflow's). An object's
    fields are the tool's own; any other input is the tool's field ``input``.
    """
    pydantic = _pydantic()
    adapter = pydantic.TypeAdapter(input_type) if input_type is not None else None
    inner: dict[str, Any] = adapter.json_schema() if adapter is not None else {"type": "object", "properties": {}}
    flat = inner.get("type") == "object" and "properties" in inner
    properties: dict[str, Any] = {}
    required: list[str] = []
    if model_keys is not None:
        properties["key"] = {"type": "string", "description": f"Which {model_keys}: its key"}
        required.append("key")
    if flat:
        properties.update(inner["properties"])
        required.extend(inner.get("required", []))
    elif adapter is not None:
        properties["input"] = inner
        required.append("input")
    schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
    if "$defs" in inner:
        schema["$defs"] = inner["$defs"]

    def validate(args: dict[str, Any]) -> tuple[str | None, Any]:
        key = None
        if model_keys is not None:
            key = args.get("key")
            if not isinstance(key, str):
                raise ValueError("key: a string is required")
        if adapter is None:
            return key, None
        value = {k: v for k, v in args.items() if k != "key"} if flat else args.get("input")
        return key, adapter.validate_python(value)

    return _Input(schema=schema, validate=validate)


def _keyed(handler: Handler[Any, Any]) -> bool:
    return handler.service_tag.kind != "service"


def call(
    target: Callable[..., Any],
    *,
    description: str,
    name: str | None = None,
    key: PerSession[str] | None = None,
) -> Tool:
    """
    A tool that calls handler ``target`` and answers with its result::

        call(order_status, description="The status of an order.")

    The model fills in the handler's input; of a virtual object, it chooses the
    key too, unless ``key`` gives it.
    """
    handler = _handler(target)
    service = handler.service_tag.name
    model_keys = service if _keyed(handler) and key is None else None
    shape = _input_of(_input_type(handler), model_keys)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        chosen, value = arguments["key"], arguments["value"]

        async def made(c: ToolCall) -> Any:
            target_key = chosen if key is None else await configured(key, c.ctx)
            target_key = target_key if _keyed(handler) else None
            return await _untyped(c.ctx).call(
                target, *_inputs(handler, value), key=target_key, idempotency_key=c.idempotency_key
            )

        return await _once(f"{service}/{handler.name}", arguments["raw"], tool_call, made)

    return Tool(
        name=name or f"{service}_{handler.name}",
        description=description,
        input_schema=shape.schema,
        _run=run,
        _validate=lambda args: _split(shape, args),
    )


def send(
    target: Callable[..., Any],
    *,
    description: str,
    name: str | None = None,
    key: PerSession[str] | None = None,
    delay: timedelta | None = None,
) -> Tool:
    """A tool that sends to handler ``target``, without waiting for it: a one-way call, made once, after ``delay``."""
    handler = _handler(target)
    service = handler.service_tag.name
    model_keys = service if _keyed(handler) and key is None else None
    shape = _input_of(_input_type(handler), model_keys)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        chosen, value = arguments["key"], arguments["value"]

        async def made(c: ToolCall) -> str:
            target_key = chosen if key is None else await configured(key, c.ctx)
            _untyped(c.ctx).send(
                target,
                *_inputs(handler, value),
                key=target_key if _keyed(handler) else None,
                delay=delay,
                idempotency_key=c.idempotency_key,
            )
            return f"sent to {service}{'' if target_key is None else f'/{target_key}'}/{handler.name}"

        return await _once(f"{service}/{handler.name}", arguments["raw"], tool_call, made)

    return Tool(
        name=name or f"{service}_{handler.name}",
        description=description,
        input_schema=shape.schema,
        _run=run,
        _validate=lambda args: _split(shape, args),
    )


def workflow(
    target: Callable[..., Any],
    *,
    description: str,
    name: str | None = None,
    key: PerSession[str] | None = None,
) -> Tool:
    """
    A tool that runs the workflow of ``target`` (its main handler) to the end,
    and answers with its result. Each tool call is a workflow of its own, with
    an id made of the agent's key and the tool call's id, unless ``key`` gives
    one.
    """
    handler = _handler(target)
    service = handler.service_tag.name
    shape = _input_of(_input_type(handler), None)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        value = arguments["value"]

        async def made(c: ToolCall) -> Any:
            workflow_id = f"{c.ctx.key}-{c.call_id}" if key is None else await configured(key, c.ctx)
            return await _untyped(c.ctx).call(
                target, *_inputs(handler, value), key=workflow_id, idempotency_key=c.idempotency_key
            )

        return await _once(service, arguments["raw"], tool_call, made)

    return Tool(
        name=name or service,
        description=description,
        input_schema=shape.schema,
        _run=run,
        _validate=lambda args: _split(shape, args),
    )


def _split(shape: _Input, args: dict[str, Any]) -> dict[str, Any]:
    """The tool call's key and the handler's input, validated, and the raw arguments (what makes the call the same)."""
    key, value = shape.validate(args)
    return {"key": key, "value": value, "raw": args}


_TASK_NAME = r"^[a-z0-9-]+$"


def subagent(
    agent: object,
    *,
    description: str | None = None,
    name: str | None = None,
    handler: str = "chat",
) -> Tool:
    """
    A tool, named after ``agent`` (an agent, or any virtual object) unless
    ``name`` names it, that hands a task to it: to the object keyed
    ``<this key>.<task name>``, through its ``handler`` (``chat``), and answers
    with its answer. Tasks of different names run at once; the same name
    continues the same task. The model reads what the agent is from its
    description::

        coder = langgraph.agent("coder", description="Builds and tests code in Python")
        lead = langgraph.agent("lead", tools=[subagent(coder)])
    """
    agent_name = typing.cast(str, getattr(agent, "name"))  # noqa: B009 (an SDK's definition)
    tag = getattr(agent, "service_tag", None)
    about = description if description is not None else typing.cast("str | None", getattr(tag, "description", None))
    pydantic = _pydantic()

    class Task(pydantic.BaseModel):  # type: ignore[name-defined]  # pyright: ignore[reportUntypedBaseClass]
        name: str = pydantic.Field(pattern=_TASK_NAME, description='A short name for the task, such as "fizzbuzz"')
        task: str = pydantic.Field(description="The task in full: the agent sees nothing else")

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        task_name, task = typing.cast(str, arguments["name"]), typing.cast(str, arguments["task"])

        async def made(c: ToolCall) -> Any:
            return await c.ctx.generic_call(
                agent_name, handler, task, key=f"{c.ctx.key}.{task_name}", idempotency_key=c.idempotency_key
            )

        return await _once(agent_name, {"name": task_name, "task": task}, tool_call, made)

    def validate(args: dict[str, Any]) -> dict[str, Any]:
        task = Task.model_validate(args)
        return {"name": task.name, "task": task.task}

    return Tool(
        name=name or agent_name,
        description=(
            f"Hand a task to the {agent_name} agent{f' ({about})' if about else ''}, and get its answer. "
            "Tasks of different names run at once; the same name continues the same task."
        ),
        input_schema=typing.cast(dict[str, Any], Task.model_json_schema()),
        _run=run,
        _validate=validate,
    )


def wait_for_signal(signal: str, *, description: str, input: Any = None, name: str | None = None) -> Tool:
    """Create a tool that waits for a signal to the agent, and answers with its value.

    A person's approval, say, or another system's result. The agent waits
    without holding a process, for as long as it takes::

        approval = wait_for_signal("approval", description="Wait for a manager to approve a refund.", input=Refund)

    Each wait has an id. The agent's ``pending`` view lists the waits, and its
    ``signal`` view resolves one, by its id or as the oldest wait for a name::

        curl localhost:8080/support/alice/signal --json '{"name": "approval", "value": "approved"}'

    Args:
        signal: The signal's name.
        description: What the tool waits for, for the model.
        input: The type of what the model says it waits for (such as the
            refund to approve), which ``pending`` shows. None if it says nothing.
        name: The tool's name. Defaults to ``"wait_for_<signal>"``.

    Returns:
        The tool, for any framework's agent.
    """
    shape = _input_of(input, None)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        async def made(c: ToolCall) -> Any:
            wait_id = c.idempotency_key
            request: _signals.WaitRequest = {"id": wait_id, "name": signal}
            if input is not None:
                request["input"] = arguments["raw"]
            return await c.ctx.call(_signals.wait, request, key=_signals.signals_of(c.ctx), idempotency_key=wait_id)

        return await _once(f"signal/{signal}", arguments["raw"], tool_call, made)

    return Tool(
        name=name or f"wait_for_{signal}",
        description=description,
        input_schema=shape.schema,
        _run=run,
        _validate=lambda args: _split(shape, args),
    )
