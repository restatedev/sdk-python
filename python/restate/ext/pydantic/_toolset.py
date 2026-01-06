from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from restate import RunOptions, SdkInternalBaseException, TerminalError
from restate.exceptions import SdkInternalException
from restate.extensions import current_context

from pydantic_ai import ToolDefinition
from pydantic_ai._run_context import AgentDepsT
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, UserError
from pydantic_ai.mcp import MCPServer, ToolResult
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from ._serde import PydanticTypeAdapter
from ._utils import current_state


@dataclass
class RestateContextRunResult:
    """A simple wrapper for tool results to be used with Restate's run_typed."""

    kind: Literal["output", "call_deferred", "approval_required", "model_retry"]
    output: Any
    error: str | None = None


CONTEXT_RUN_SERDE = PydanticTypeAdapter(RestateContextRunResult)


@dataclass
class RestateMCPGetToolsContextRunResult:
    """A simple wrapper for tool results to be used with Restate's run_typed."""

    output: dict[str, ToolDefinition]


MCP_GET_TOOLS_SERDE = PydanticTypeAdapter(RestateMCPGetToolsContextRunResult)


@dataclass
class RestateMCPToolRunResult:
    """A simple wrapper for tool results to be used with Restate's run_typed."""

    output: ToolResult


MCP_RUN_SERDE = PydanticTypeAdapter(RestateMCPToolRunResult)


class RestateContextRunToolSet(WrapperToolset[AgentDepsT]):
    """A toolset that automatically wraps tool calls with restate's `ctx.run_typed()`."""

    def __init__(self, wrapped: AbstractToolset[AgentDepsT], run_options: RunOptions):
        super().__init__(wrapped)
        self.options = replace(run_options, serde=CONTEXT_RUN_SERDE)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> Any:
        async def action() -> RestateContextRunResult:
            try:
                # A tool may raise ModelRetry, CallDeferred, ApprovalRequired, or UserError
                # to signal special conditions to the caller.
                # Since, restate ctx.run() will retry this exception we need to convert these exceptions
                # to a return value and handle them outside of the ctx.run().
                output = await self.wrapped.call_tool(name, tool_args, ctx, tool)
                return RestateContextRunResult(kind="output", output=output, error=None)
            except ModelRetry as e:
                return RestateContextRunResult(kind="model_retry", output=None, error=e.message)
            except CallDeferred:
                return RestateContextRunResult(kind="call_deferred", output=None, error=None)
            except ApprovalRequired:
                return RestateContextRunResult(kind="approval_required", output=None, error=None)
            except UserError as e:
                raise TerminalError(str(e)) from e

        id = ctx.tool_call_id
        if id is None:
            raise TerminalError("Tool call ID is required for turnstile synchronization.")
        context = current_context()
        if context is None:
            raise UserError(
                "A tool cannot be used without a Restate context. Make sure to run it within an agent or a run context."
            )
        turnstile = current_state().turnstile
        try:
            await turnstile.wait_for(id)
            res = await context.run_typed(f"Calling {name}", action, self.options)

            if res.kind == "call_deferred":
                raise CallDeferred()
            elif res.kind == "approval_required":
                raise ApprovalRequired()
            elif res.kind == "model_retry":
                assert res.error is not None
                raise ModelRetry(res.error)
            else:
                assert res.kind == "output"
                turnstile.allow_next_after(id)
                return res.output
        except Exception as e:
            turnstile.cancel_all_after(id)
            raise e from None
        except SdkInternalException as e:
            turnstile.cancel_all_after(id)
            raise RuntimeError() from e

    def visit_and_replace(
        self, visitor: Callable[[AbstractToolset[AgentDepsT]], AbstractToolset[AgentDepsT]]
    ) -> AbstractToolset[AgentDepsT]:
        return visitor(self)


class RestateMCPServer(WrapperToolset[AgentDepsT]):
    """A wrapper for MCPServer that integrates with restate."""

    def __init__(self, wrapped: MCPServer, run_options: RunOptions):
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
        assert isinstance(self.wrapped, MCPServer)
        return self.wrapped.tool_for_tool_def(tool_def)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDepsT],
        tool: ToolsetTool[AgentDepsT],
    ) -> ToolResult:
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
