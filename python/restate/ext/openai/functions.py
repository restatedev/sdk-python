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

from typing import List, Any
import dataclasses

from agents import (
    Handoff,
    TContext,
    Agent,
    RunContextWrapper,
    ModelBehaviorError,
)

from agents.tool import FunctionTool, Tool, ToolFunction, function_tool as oai_function_tool
from agents.tool_context import ToolContext
from agents.items import TResponseOutputItem

from restate import TerminalError

from .models import State, AgentsTerminalException


def function_tool(func: ToolFunction, *args, **kwargs) -> FunctionTool:
    failure_error_function = kwargs.pop("failure_error_function", raise_terminal_errors)

    return oai_function_tool(
        func,
        *args,
        failure_error_function=failure_error_function,
        **kwargs,
    )


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


def continue_on_terminal_errors(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    # Raise terminal errors and cancellations
    if isinstance(error, TerminalError):
        # For the agent SDK it needs to be an AgentsException, for restate it needs to be a TerminalError
        # so we create a new exception that inherits from both
        return f"An error occurred while running the tool: {str(error)}"

    if isinstance(error, ModelBehaviorError):
        return f"An error occurred while calling the tool: {str(error)}"

    raise error


def get_function_call_ids(response: list[TResponseOutputItem]) -> List[str]:
    """Extract function call IDs from the model response."""
    # TODO: support function calls in other response types
    return [item.call_id for item in response if item.type == "function_call"]


def _create_wrapper(state, captured_tool):
    async def on_invoke_tool_wrapper(tool_context: ToolContext[Any], tool_input: Any) -> Any:
        turnstile = state.turnstile
        call_id = tool_context.tool_call_id
        try:
            await turnstile.wait_for(call_id)
            return await captured_tool.on_invoke_tool(tool_context, tool_input)
        finally:
            turnstile.allow_next_after(call_id)

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
