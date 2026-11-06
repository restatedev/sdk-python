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
"""Tools that work with any of the agent frameworks.

Define a tool once with ``tool``, and pass it in any agent's ``tools``: the
integration converts it to the framework's own tool type. The other tools
here are built on Restate itself: ``call`` calls a handler, ``send`` sends to
one, ``workflow`` runs a workflow, ``subagent`` hands a task to another agent,
and ``wait_for_signal`` waits for a signal.

A tool runs with the agent's context and the id of the model's tool call. The
Restate calls these tools make use an idempotency key built from the
invocation id and that id, so a tool call that runs again after a crash makes
the same call only once.

A tool's input schema is derived from its type hints by pydantic, which is
imported when the tool is created. Each framework extra installs it.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
import typing
from collections.abc import Awaitable, Callable, Generator
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
"""The parameters a tool function gets from the runtime rather than from the model."""


def _pydantic() -> Any:
    """Import and return pydantic, or raise ``ImportError`` explaining how to install it."""
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
    """A running tool call: the agent's context and the id of the model's tool call.

    The id stays the same when a crash makes the tool call run again.
    """

    ctx: SessionContext
    call_id: str

    @property
    def idempotency_key(self) -> str:
        """The tool call's idempotency key, ``<invocation id>:<call id>``.

        Pass it to anything the tool must do only once however often it runs,
        such as a Restate call or a payment API request.
        """
        return f"{self.ctx.request.id}:{self.call_id}"


_RUNNING: contextvars.ContextVar[SessionContext] = contextvars.ContextVar("restate_agents_running")
"""The context of the framework run in progress, for tools that the framework calls without it."""


@contextlib.contextmanager
def tools_in(ctx: SessionContext) -> Generator[None, None, None]:
    """Run a framework in ``ctx``: the tools it calls find their call with ``current_call``."""
    token = _RUNNING.set(ctx)
    try:
        yield
    finally:
        _RUNNING.reset(token)


def current_call(call_id: str) -> ToolCall:
    """Return tool call ``call_id`` of the framework run in progress (see ``tools_in``)."""
    return ToolCall(_RUNNING.get(), call_id)


@dataclass(frozen=True)
class Tool:
    """A tool that any framework's agent can use.

    Create one with ``tool``, ``call``, ``send``, ``workflow``, ``subagent``
    or ``wait_for_signal`` rather than directly.

    Attributes:
        name: The tool's name, as the model sees it.
        description: What the tool does, for the model.
        input_schema: The JSON schema of the tool's input, which the
            framework passes to the model.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    _run: Callable[[dict[str, Any], ToolCall], Awaitable[Any]]
    _validate: Callable[[dict[str, Any]], Any]

    async def run(self, args: object, call: ToolCall) -> str:
        """Run the tool with the model's raw arguments, and return its result as text for the model.

        Arguments that do not match the input schema are not an error: the
        result then tells the model what is wrong, so it can call the tool
        again. See ``tool`` for how errors are handled.

        Args:
            args: The arguments, as the model gave them.
            call: The tool call this run is for.

        Returns:
            The result, or ``"Error: <message>"`` for the model to act on.

        Raises:
            ToolError: If the tool fails with an error other than
                ``TerminalError``; Restate then retries the attempt.
        """
        try:
            arguments = self._validate(typing.cast(dict[str, Any], args if isinstance(args, dict) else {}))
        except Exception as error:  # pydantic's ValidationError, for the model to fix
            return f"Error: the arguments do not fit the tool's input: {error}"
        try:
            return _text(await self._run(arguments, call))
        except BaseException as error:
            # The turn stopped (its invocation was cancelled, or its attempt
            # ended). End it, rather than letting the model carry on with the
            # tool's error as the result (such as a call that the cancellation
            # cancelled).
            stop = stop_signal_of(call.ctx)
            if stop is not None and stop.reason is not None:
                raise stop.reason from error
            if isinstance(error, TerminalError):
                # A terminal error is the tool's result, which the model can act on.
                return f"Error: {error.message}"
            if not isinstance(error, Exception):
                raise
            # Any other error fails the attempt, which Restate retries.
            raise ToolError(self.name, error) from error


async def run_tool(tool: Tool, args: object, call: ToolCall) -> str:
    """Run ``tool`` for a tool call, the way a framework integration should.

    Unlike ``Tool.run``, it publishes the tool call's start and end to the
    clients that follow the conversation, and first settles the earlier calls
    whose results the framework has stored.

    Args:
        tool: The tool to run.
        args: The arguments, as the model gave them.
        call: The tool call this run is for.

    Returns:
        The tool's result as text, as ``Tool.run`` returns it.
    """
    await tool_call_started(call.ctx)
    try:
        return await tool.run(args, call)
    finally:
        await tool_call_ended(call.ctx)


def _text(value: object) -> str:
    """Convert a result to text: a string as is, ``None`` as ``"done"``, anything else as JSON."""
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
    """Create a tool from a function, sync or async.

    The model supplies the function's arguments; their type hints and the
    docstring describe the tool to the model::

        @tool
        async def get_weather(city: str) -> str:
            '''The current weather in a city.'''
            return f"Sunny in {city}"

    Three parameter names are reserved, and filled in by the runtime instead
    of the model: ``ctx``, the agent's ``SessionContext`` (for ``ctx.db``,
    calls and sends); ``call_id``, the tool call's id; and
    ``idempotency_key``, built from the invocation id and ``call_id``, for
    whatever the tool must do only once. A crash runs an unfinished tool call
    again with the same id.

    If the function raises ``restate.TerminalError`` (or a Restate call it
    makes fails with one), the model gets ``"Error: <message>"`` as the
    result, and can act on it. Any other exception fails the attempt, and
    Restate retries it.

    Args:
        fn: The function. Omit it to use ``tool(name=..., description=...)``
            as a decorator.
        name: The tool's name. Defaults to the function's name.
        description: What the tool does, for the model. Defaults to the
            function's docstring.

    Returns:
        The tool, or a decorator that creates it.

    Raises:
        ImportError: If pydantic is not installed.
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
    """Run ``body``, a Restate call of tool ``name``, so that it is made once however often the tool call runs.

    ``body`` gets the id under which the call was first made, possibly by an
    earlier attempt, and so the same idempotency key (see ``_calls.py``).
    """
    made = await call_of(call.ctx, name, input, call.call_id)
    try:
        result = await body(ToolCall(call.ctx, made.id))
    except TerminalError as error:
        # A terminal error is the call's result; any other error fails the attempt, and the retry calls again.
        await made.answered(f"Error: {error.message}")
        raise
    await made.answered()
    return result


def untyped(ctx: SessionContext) -> Any:
    """Return ``ctx`` untyped, to call a handler whose input type is known only at run time."""
    return ctx


def inputs_of(handler: Handler[Any, Any], value: Any) -> tuple[Any, ...]:
    """Return the positional arguments for calling ``handler``: ``(value,)`` if it takes an input."""
    return (value,) if handler.arity >= 2 else ()


def handler_of(target: Callable[..., Any]) -> Handler[Any, Any]:
    try:
        return handler_from_callable(target)
    except ValueError:
        raise TypeError(f"{target!r} is not a handler of a service, an object or a workflow") from None


def input_type_of(handler: Handler[Any, Any]) -> Any:
    """Return the type of a handler's input, or ``None`` if it takes none."""
    if handler.arity < 2:
        return None
    parameters = list(inspect.signature(handler.fn, eval_str=True).parameters.values())
    annotation = parameters[1].annotation
    return Any if annotation is inspect.Parameter.empty else annotation


@dataclass(frozen=True)
class _Input:
    """The input of a Restate tool: the handler's input (flattened if it is an object), and the key if the model picks it."""

    schema: dict[str, Any]
    validate: Callable[[dict[str, Any]], tuple[str | None, Any]]


def _input_of(input_type: Any, model_keys: str | None) -> _Input:
    """Build a Restate tool's input from a handler's ``input_type``.

    If ``model_keys`` is given, the model also picks the key, in a ``key``
    field. An object type's fields become the tool's own fields; any other
    type becomes a single ``input`` field.
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
    """Create a tool that calls a handler and returns its result.

    The model fills in the handler's input, and for a virtual object or a
    workflow it also picks the key, unless ``key`` is given::

        call(order_status, description="The status of an order.")

    The call is made once per tool call, even if the tool call runs again
    after a crash.

    Args:
        target: A handler of a service, a virtual object or a workflow.
        description: What the tool does, for the model.
        name: The tool's name. Defaults to ``"<service>_<handler>"``.
        key: The key to call, or a function of the context that returns it.
            If omitted, the model picks it.

    Returns:
        The tool, for any framework's agent.

    Raises:
        TypeError: If ``target`` is not a handler.
    """
    handler = handler_of(target)
    service = handler.service_tag.name
    model_keys = service if _keyed(handler) and key is None else None
    shape = _input_of(input_type_of(handler), model_keys)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        chosen, value = arguments["key"], arguments["value"]

        async def made(c: ToolCall) -> Any:
            target_key = chosen if key is None else await configured(key, c.ctx)
            target_key = target_key if _keyed(handler) else None
            return await untyped(c.ctx).call(
                target, *inputs_of(handler, value), key=target_key, idempotency_key=c.idempotency_key
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
    """Create a tool that sends to a handler without waiting for it to run.

    Like ``call``, but a one-way call: the model gets a confirmation instead
    of the handler's result. The send is made once per tool call.

    Args:
        target: A handler of a service, a virtual object or a workflow.
        description: What the tool does, for the model.
        name: The tool's name. Defaults to ``"<service>_<handler>"``.
        key: The key to send to, or a function of the context that returns
            it. If omitted, the model picks it.
        delay: How long to wait before the handler runs.

    Returns:
        The tool, for any framework's agent.

    Raises:
        TypeError: If ``target`` is not a handler.
    """
    handler = handler_of(target)
    service = handler.service_tag.name
    model_keys = service if _keyed(handler) and key is None else None
    shape = _input_of(input_type_of(handler), model_keys)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        chosen, value = arguments["key"], arguments["value"]

        async def made(c: ToolCall) -> str:
            target_key = (chosen if key is None else await configured(key, c.ctx)) if _keyed(handler) else None
            untyped(c.ctx).send(
                target, *inputs_of(handler, value), key=target_key, delay=delay, idempotency_key=c.idempotency_key
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
    """Create a tool that runs a workflow to completion and returns its result.

    Each tool call starts a workflow of its own, whose id is built from the
    agent's key and the tool call's id, unless ``key`` is given.

    Args:
        target: The workflow's main handler.
        description: What the tool does, for the model.
        name: The tool's name. Defaults to the workflow's name.
        key: The workflow id, or a function of the context that returns it.

    Returns:
        The tool, for any framework's agent.

    Raises:
        TypeError: If ``target`` is not a handler.
    """
    handler = handler_of(target)
    service = handler.service_tag.name
    shape = _input_of(input_type_of(handler), None)

    async def run(arguments: dict[str, Any], tool_call: ToolCall) -> Any:
        value = arguments["value"]

        async def made(c: ToolCall) -> Any:
            workflow_id = f"{c.ctx.key}-{c.call_id}" if key is None else await configured(key, c.ctx)
            return await untyped(c.ctx).call(
                target, *inputs_of(handler, value), key=workflow_id, idempotency_key=c.idempotency_key
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
    """Validate the arguments into the key and the handler's input, keeping the raw arguments to identify the call."""
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
    """Create a tool that hands a task to another agent and returns its answer.

    The model names each task, and the task goes to the agent's key
    ``<this key>.<task name>``. Tasks with different names run concurrently;
    reusing a name continues the same task. The model learns what the agent
    does from its description::

        coder = langgraph.agent("coder", description="Builds and tests code in Python")
        lead = langgraph.agent("lead", tools=[subagent(coder)])

    Args:
        agent: An agent, or any virtual object with a handler that takes text.
        description: What the agent does, for the model. Defaults to the
            agent's own description.
        name: The tool's name. Defaults to the agent's name.
        handler: The handler that takes the task.

    Returns:
        The tool, for any framework's agent.
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
