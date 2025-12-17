#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#

from typing import List, Any, overload, Callable, Union, TypeVar, Awaitable
import dataclasses

from agents import (
    Handoff,
    TContext,
    Agent,
    RunContextWrapper,
    ModelBehaviorError,
    AgentBase,
)

from agents.function_schema import DocstringStyle

from agents.tool import (
    FunctionTool,
    Tool,
    ToolFunction,
    ToolErrorFunction,
    function_tool as oai_function_tool,
    default_tool_error_function,
)
from agents.tool_context import ToolContext
from agents.items import TResponseOutputItem

from restate import TerminalError

from .models import State, AgentsTerminalException

T = TypeVar("T")

MaybeAwaitable = Union[Awaitable[T], T]


def raise_terminal_errors(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    # Raise terminal errors and cancellations
    if isinstance(error, TerminalError):
        # For the agent SDK it needs to be an AgentsException, for restate it needs to be a TerminalError
        # so we create a new exception that inherits from both
        raise AgentsTerminalException(error.message)

    if isinstance(error, ModelBehaviorError):
        return f"An error occurred while calling the tool: {str(error)}"

    raise error


def propagate_cancellation(failure_error_function: ToolErrorFunction | None = None) -> ToolErrorFunction:
    _fn = failure_error_function if failure_error_function is not None else default_tool_error_function

    def inner(context: RunContextWrapper[Any], error: Exception):
        """Raise cancellations as exceptions."""
        if isinstance(error, TerminalError):
            if error.status_code == 409:
                raise error from None
        return _fn(context, error)

    return inner


@overload
def durable_function_tool(
    func: ToolFunction[...],
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
) -> FunctionTool:
    """Overload for usage as @function_tool (no parentheses)."""
    ...


@overload
def durable_function_tool(
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
) -> Callable[[ToolFunction[...]], FunctionTool]:
    """Overload for usage as @function_tool(...)."""
    ...


def durable_function_tool(
    func: ToolFunction[...] | None = None,
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = raise_terminal_errors,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
) -> FunctionTool | Callable[[ToolFunction[...]], FunctionTool]:
    failure_fn = propagate_cancellation(failure_error_function)

    if callable(func):
        return oai_function_tool(
            func=func,
            name_override=name_override,
            description_override=description_override,
            docstring_style=docstring_style,
            use_docstring_info=use_docstring_info,
            failure_error_function=failure_fn,
            strict_mode=strict_mode,
            is_enabled=is_enabled,
        )
    else:
        return oai_function_tool(
            name_override=name_override,
            description_override=description_override,
            docstring_style=docstring_style,
            use_docstring_info=use_docstring_info,
            failure_error_function=failure_fn,
            strict_mode=strict_mode,
            is_enabled=is_enabled,
        )


def get_function_call_ids(response: list[TResponseOutputItem]) -> List[str]:
    """Extract function call IDs from the model response."""
    # TODO: support function calls in other response types
    return [item.call_id for item in response if item.type == "function_call"]


def _create_wrapper(state, captured_tool):
    async def on_invoke_tool_wrapper(tool_context: ToolContext[Any], tool_input: Any) -> Any:
        turnstile = state.turnstile
        call_id = tool_context.tool_call_id
        # wait for our turn
        await turnstile.wait_for(call_id)
        try:
            # invoke the original tool
            res = await captured_tool.on_invoke_tool(tool_context, tool_input)
            # allow the next tool to proceed
            turnstile.allow_next_after(call_id)
            return res
        except BaseException as ex:
            # if there was an error, it will be propagated up, towards the handler
            # but we need to make sure that all subsequent tools will not execute
            # as they might interact with the restate context.
            turnstile.cancel_all_after(call_id)
            # re-raise the exception
            raise ex from None

    return on_invoke_tool_wrapper


def wrap_agent_tools(
    agent: Agent[TContext],
    state: State,
) -> Agent[TContext]:
    """
    Wrap the tools of an agent to use the Restate error handling.

    Returns:
        A new agent with wrapped tools.
    """
    wrapped_tools: list[Tool] = []
    for tool in agent.tools:
        if isinstance(tool, FunctionTool):
            wrapped = _create_wrapper(state, tool)
            wrapped_tools.append(dataclasses.replace(tool, on_invoke_tool=wrapped))
        else:
            wrapped_tools.append(tool)

    wrapped_handoffs: list[Agent[Any] | Handoff[Any]] = []
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            wrapped_handoff = wrap_agent_tools(handoff, state)
            wrapped_handoffs.append(wrapped_handoff)
        elif isinstance(handoff, Handoff):
            wrapped_handoffs.append(wrap_agent_handoff_tools(handoff, state))
        else:
            raise TypeError(f"Unsupported handoff type: {type(handoff)}")

    return agent.clone(tools=wrapped_tools, handoffs=wrapped_handoffs)


def wrap_agent_handoff_tools(
    handoff: Handoff[TContext],
    state: State,
) -> Handoff[TContext]:
    """
    Wrap the tools of a handoff to use the Restate error handling.

    Returns:
        A new handoff with wrapped tools.
    """

    original_on_invoke_handoff = handoff.on_invoke_handoff

    async def wrapped(*args, **kwargs) -> Any:
        agent = await original_on_invoke_handoff(*args, **kwargs)
        return wrap_agent_tools(agent, state)

    return dataclasses.replace(handoff, on_invoke_handoff=wrapped)
