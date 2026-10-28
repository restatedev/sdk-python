from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from restate import RunOptions, SdkInternalBaseException
from restate.extensions import current_context

from pydantic_ai import ToolDefinition
from pydantic_ai._run_context import AgentDepsT
from pydantic_ai.exceptions import UserError
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from ._serde import PydanticTypeAdapter


@dataclass
class RestateMCPGetToolsContextRunResult:
    """A simple wrapper for tool results to be used with Restate's run_typed."""

    output: dict[str, ToolDefinition]


MCP_GET_TOOLS_SERDE = PydanticTypeAdapter(RestateMCPGetToolsContextRunResult)


@dataclass
class RestateMCPToolRunResult:
    """A simple wrapper for tool results to be used with Restate's run_typed."""

    output: Any


MCP_RUN_SERDE = PydanticTypeAdapter(RestateMCPToolRunResult)


class RestateMCPToolset(WrapperToolset[AgentDepsT]):
    """A wrapper for MCPToolset that integrates with Restate."""

    def __init__(self, wrapped: MCPToolset[AgentDepsT], run_options: RunOptions):
        super().__init__(wrapped)
        self._wrapped = wrapped
        self.get_tools_options = replace(run_options, serde=MCP_GET_TOOLS_SERDE)
        self.run_tools_options = replace(run_options, serde=MCP_RUN_SERDE)

    def visit_and_replace(
        self, visitor: Callable[[AbstractToolset[AgentDepsT]], AbstractToolset[AgentDepsT]]
    ) -> AbstractToolset[AgentDepsT]:
        return visitor(self)

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        async def get_tools_in_context() -> RestateMCPGetToolsContextRunResult:
            res = await self._wrapped.get_tools(ctx)
            # ToolsetTool is not serializable as it holds a SchemaValidator
            # (which is also the same for every MCP tool so unnecessary to pass along the wire every time),
            # so we just return the ToolDefinitions and wrap them in ToolsetTool outside of the activity.
            return RestateMCPGetToolsContextRunResult(output={name: tool.tool_def for name, tool in res.items()})

        context = current_context()
        if context is None:
            raise UserError(
                "A toolset cannot be used without a Restate context. Make sure to run it within an agent or a run context."
            )

        try:
            tool_defs = await context.run_typed("get mcp tools", get_tools_in_context, self.get_tools_options)
            return {name: self.tool_for_tool_def(tool_def) for name, tool_def in tool_defs.output.items()}
        except SdkInternalBaseException as e:
            raise Exception("Internal error during get_tools call") from e

    def tool_for_tool_def(self, tool_def: ToolDefinition) -> ToolsetTool[AgentDepsT]:
        assert isinstance(self.wrapped, MCPToolset)
        return self.wrapped.tool_for_tool_def(tool_def)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDepsT],
        tool: ToolsetTool[AgentDepsT],
    ) -> Any:
        async def call_tool_in_context() -> RestateMCPToolRunResult:
            res = await self._wrapped.call_tool(name, tool_args, ctx, tool)
            return RestateMCPToolRunResult(output=res)

        context = current_context()
        if context is None:
            raise UserError(
                "A toolset cannot be used without a Restate context. Make sure to run it within an agent or a run context."
            )
        try:
            res = await context.run_typed(f"Calling mcp tool {name}", call_tool_in_context, self.run_tools_options)
        except SdkInternalBaseException as e:
            raise Exception("Internal error during tool call") from e

        return res.output
