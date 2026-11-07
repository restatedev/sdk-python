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
"""Bring back bugs that restate.agents had, one at a time, and see which tests notice.

Each mutant is a small edit of today's code that brings back one bug: a bug
that a fix commit describes ("history"), or one that would break the
contract of a turn ("durability"). For each, the runner applies the edit,
runs the tests that could see it, restores the code, and says what failed:

- caught: a test other than the bug's own regression tests failed, so the
  suite would have found the bug without them;
- regression only: only the tests that the fix commit added failed;
- survived: no test failed. The bug can come back unnoticed.

    uv run --no-sync python scripts/resurrect.py                 # every mutant (Docker: most tests need Restate)
    uv run --no-sync python scripts/resurrect.py openai-approval-resume pydantic-deps
    uv run --no-sync python scripts/resurrect.py --list

The tests that fail without any edit (the baseline) are left out of every
verdict. The code is restored after each mutant, and on Ctrl-C.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = "python/restate/agents"

OPENAI = ("tests/agents_openai.py", "tests/agents_output.py")
PYDANTIC_AI = ("tests/agents_pydantic_ai.py", "tests/agents_output.py")
LANGGRAPH = (
    "tests/agents_langgraph.py",
    "tests/agents_langgraph_graph.py",
    "tests/agents_langgraph_nodes.py",
    "tests/agents_langgraph_checkpointer.py",
    "tests/agents_langgraph_create_agent.py",
    "tests/agents_output.py",
)
EVERY_FRAMEWORK = (
    "tests/agents_conversation.py",
    "tests/agents_tools.py",
    "tests/agents_openai.py",
    "tests/agents_pydantic_ai.py",
    "tests/agents_adk.py",
    "tests/agents_langgraph.py",
    "tests/agents_langgraph_graph.py",
)
OBJECT = ("tests/agents_object.py", "tests/agents_signals.py")

# The tests that each fix commit added: a bug's own regression tests.
OWN: dict[str, tuple[str, ...]] = {
    "0c7e03a": (
        "test_a_call_that_needs_an_approval_fails_the_turn_and_never_runs",
        "test_a_call_cut_off_by_a_crash_that_needs_an_approval_fails_the_turn_without_asking_again",
        "test_a_call_cut_off_by_a_crash_asks_the_tools_approval_function",
        "test_a_call_cut_off_by_a_crash_to_a_tool_with_a_timeout_is_resumed_with_its_timeout",
        "test_a_call_cut_off_by_a_crash_to_a_guarded_tool_is_asked_for_again_through_its_guardrail",
        "test_an_error_of_the_agents_own_code_is_retried_and_a_models_refusal_is_not",
        "test_a_missing_api_key_is_a_refusal_for_the_built_in_agent_and_one_of_your_own",
        "test_a_setup_error_of_the_sdk_ends_the_turn",
        "test_a_missing_package_ends_the_turn_but_a_tools_import_error_is_retried",
    ),
    "2a351ed": (
        "test_a_model_that_cannot_be_made_is_refused_whatever_its_error",
        "test_an_agent_of_your_own_keeps_its_system_prompt_in_each_turn",
        "test_a_system_prompt_comes_first_after_turns_that_ended_before_any_model_request",
        "test_an_agent_that_keeps_calling_tools_ends_the_turn_at_its_usage_limit",
        "test_usage_limits_set_a_runs_limit",
        "test_a_tool_whose_retries_run_out_ends_the_turn",
        "test_a_response_that_the_model_could_not_read_is_retried",
        "test_a_run_stopped_for_a_deferred_tool_call_fails_and_the_call_is_closed",
        "test_an_agent_of_your_own_runs_with_its_own_capabilities",
        "test_the_built_in_agent_gets_the_compaction_of_its_models_provider",
        "test_the_built_in_agent_on_anthropic_gets_its_compaction",
        "test_an_agent_of_your_own_runs_as_configured_with_the_system_prompt_reinjected",
        "test_an_agent_of_your_own_gets_its_deps",
        "test_a_setup_error_of_pydantic_ai_ends_the_turn",
        "test_an_error_of_the_agents_own_code_is_retried_whatever_its_name",
        "test_the_openai_sdks_error_for_a_missing_key_is_a_refusal",
    ),
    "8f1e7f7": (
        "test_a_model_that_keeps_calling_tools_ends_the_turn_at_the_recursion_limit",
        "test_a_graph_past_its_recursion_limit_ends_the_turn_and_runs_no_further",
        "test_an_invalid_update_of_the_graph_ends_the_turn",
        "test_a_run_resumed_after_an_invalid_update_ends_its_turn",
        "test_only_langgraphs_own_errors_end_a_turn",
    ),
    "ffbc6af": (
        "test_a_tools_error_of_a_built_in_type_is_marked_and_is_no_refusal",
        "test_prune_keeps_the_latest_checkpoint_and_only_its_values_and_the_thread_goes_on",
        "test_prune_deletes_threads_and_knows_no_other_strategy",
        "test_a_subgraph_paused_and_pruned_resumes_whole_and_each_namespace_keeps_its_latest",
        "test_a_run_paused_below_a_subgraph_with_its_own_history_resumes_after_a_prune",
        "test_langgraph_keeps_an_interrupts_resume_values_on_the_task_that_raised_it",
        "test_create_agent_has_its_own_context_and_calls_restate_through_the_tools",
        "test_each_interrupt_below_a_node_that_interrupts_itself_waits_for_its_own_signal",
        "test_a_static_breakpoint_in_a_subgraph_fails_the_turn_and_the_next_message_starts_afresh",
        "test_any_error_of_the_graph_is_retried_whatever_its_name",
        "test_a_terminal_error_of_a_node_ends_the_turn_and_the_next_is_answered",
        "test_a_finished_turn_keeps_the_latest_checkpoint_of_each_namespace",
        "test_a_graph_has_its_own_context_and_the_prebuilt_nodes_still_work",
        "test_only_a_run_stopped_with_no_interrupt_is_at_a_breakpoint",
        "test_a_graph_with_static_breakpoints_is_refused",
        "test_a_graph_may_still_be_built_after_its_agent",
        "test_context_is_for_a_graph_of_your_own",
    ),
    # The fix of dot-only keys added keys to client_paths.py's tests rather than tests of its own.
    "40f0bbe": (
        "test_the_command_line_leaves_on_ctrl_c_while_it_waits_for_a_line",
        "test_a_call_reaches_the_key_it_names[.]",
        "test_a_call_reaches_the_key_it_names[..]",
        "test_the_agent_client_reaches_the_key_it_names[.]",
        "test_the_agent_client_reaches_the_key_it_names[..]",
    ),
    "c8ff842": (
        "test_an_openai_model_is_on_the_responses_api_where_reasoning_models_take_tools",
        "test_the_views_without_input_take_a_request_with_a_json_content_type",
    ),
    "": (),
}


@dataclasses.dataclass(frozen=True)
class Mutant:
    name: str
    bug: str
    """The bug it brings back, as its fix describes it."""
    file: str
    old: str
    """Text of today's code, found exactly once in ``file``."""
    new: str
    tests: tuple[str, ...]
    """The test files that could see it: those that use the code it changes."""
    commit: str = ""
    """The fix commit (``OWN``), or ``""`` for a mutant of no fix: a durability mutant."""

    @property
    def kind(self) -> str:
        return "history" if self.commit else "durability"


MUTANTS = [
    # --- #294 OpenAI (0c7e03a) ---
    Mutant(
        "openai-approval-resume",
        "a cut-off call that needs an approval runs on the retry, without it",
        f"{AGENTS}/openai/_agent.py",
        "    if any(approval is True for approval in approvals):",
        "    if False:",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-approval-run",
        "a run stopped for approvals answers (null), and the next message runs the call",
        f"{AGENTS}/openai/_agent.py",
        '    if getattr(result, "interruptions", None):',
        "    if False:",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-approval-function",
        "needs_approval read by truthiness: a tool whose function allows the call fails its turn on the retry",
        f"{AGENTS}/openai/_agent.py",
        "    if not callable(needs):\n        return bool(needs)",
        "    return bool(needs)",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-refusal-window",
        "an error of the agent's own code (an instructions function's NotFoundError) is taken for a refusal",
        f"{AGENTS}/openai/_agent.py",
        "        if not hooks.requesting:",
        "        if False:",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-missing-key",
        "the refusal window opens too late: a missing API key is retried forever",
        f"{AGENTS}/openai/_agent.py",
        "        self.requesting = True\n        return filtered",
        "        return filtered",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-setup-error",
        "the SDK's setup errors (UserError: an unknown model prefix) are retried forever",
        f"{AGENTS}/openai/_agent.py",
        "        # The SDK's own (an unknown model prefix, an input filter that returns no ModelInputData...).\n"
        '        raise TerminalError(f"{SETUP}: {error}") from error',
        "        raise",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-tool-timeout",
        "a resumed call bypasses its tool's timeout",
        f"{AGENTS}/openai/_agent.py",
        "    if _INVOKE is None:\n        return await tool.on_invoke_tool(context, arguments)",
        "    if True:\n        return await tool.on_invoke_tool(context, arguments)",
        OPENAI,
        "0c7e03a",
    ),
    Mutant(
        "openai-tool-guardrails",
        "a cut-off call to a tool with guardrails is resumed directly, bypassing them",
        f"{AGENTS}/openai/_agent.py",
        '    return any(getattr(tool, name, None) for name in ("tool_input_guardrails", "tool_output_guardrails"))',
        "    return False",
        OPENAI,
        "0c7e03a",
    ),
    # --- #292 Pydantic AI (2a351ed) ---
    Mutant(
        "pydantic-system-prompt",
        "an Agent of your own loses its system prompt on every turn",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "    if ReinjectSystemPrompt not in have:\n        chosen.append(ReinjectSystemPrompt())",
        "    pass",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-usage-limit",
        "UsageLimitExceeded is retried, each retry with a fresh limit",
        f"{AGENTS}/pydantic_ai/_agent.py",
        '            raise TerminalError(f"the run reached its usage limit: {error}") from error',
        "            raise",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-gave-up",
        "UnexpectedModelBehavior (retries that ran out, a token limit) is retried forever",
        f"{AGENTS}/pydantic_ai/_agent.py",
        '            raise TerminalError(f"the agent gave up: {error}") from error',
        "            raise",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-unreadable-response",
        "one response the model could not read ends the turn instead of being retried",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "            if error in restate.model_errors and not isinstance(error, ContentFilterError):",
        "            if False:",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-user-error",
        "Pydantic AI's UserError (a setup error) is retried forever",
        f"{AGENTS}/pydantic_ai/_agent.py",
        '            raise TerminalError(f"the agent is not set up right: {error}") from error',
        "            raise",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-refusal-by-name",
        "an error of the agent's own code is taken for a refusal by its name",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "            if error not in restate.model_errors:",
        "            if False:",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-deferred",
        "a run stopped for a deferred tool call answers with the requests' repr",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "        if isinstance(result.output, DeferredToolRequests):",
        "        if False:",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-deps",
        "an Agent of your own with a deps_type gets no deps",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "                    deps=deps,",
        "                    deps=None,",
        PYDANTIC_AI,
        "2a351ed",
    ),
    Mutant(
        "pydantic-model-error",
        "a model that cannot be made (the OpenAI SDK's own missing-key error) is not a refusal",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "        raise ModelError(f'the model \"{named}\": {error}') from error",
        "        raise",
        PYDANTIC_AI,
        "2a351ed",
    ),
    # --- #291 LangGraph (8f1e7f7) ---
    Mutant(
        "langgraph-own-errors",
        "LangGraph's own errors (recursion limit, invalid update) are retried forever",
        f"{AGENTS}/langgraph/_errors.py",
        "    if isinstance(error, GraphRecursionError):",
        "    if False:\n        return None\n    if isinstance(error, GraphRecursionError):",
        LANGGRAPH,
        "8f1e7f7",
    ),
    Mutant(
        "langgraph-invalid-update-state",
        "a checkpoint with writes LangGraph cannot apply fails every later read of the state",
        f"{AGENTS}/langgraph/_graph.py",
        "    except InvalidUpdateError:\n        latest = await app.checkpointer.aget_tuple(THREAD)",
        "    except ZeroDivisionError:\n        latest = await app.checkpointer.aget_tuple(THREAD)",
        LANGGRAPH,
        "8f1e7f7",
    ),
    # --- #290 LangGraph (ffbc6af) ---
    Mutant(
        "langgraph-static-breakpoints",
        "a graph compiled with static breakpoints is accepted",
        f"{AGENTS}/langgraph/_graph.py",
        "graph.interrupt_after_nodes):\n        raise TypeError(BREAKPOINTS)",
        "graph.interrupt_after_nodes):\n        pass",
        LANGGRAPH,
        "ffbc6af",
    ),
    Mutant(
        "langgraph-subgraph-breakpoint",
        "a run stopped at a subgraph's breakpoint answers half done",
        f"{AGENTS}/langgraph/_graph.py",
        "                if at_breakpoint(paused):",
        "                if False:",
        LANGGRAPH,
        "ffbc6af",
    ),
    Mutant(
        "langgraph-refusal-by-name",
        "a graph's error is taken for a refusal by its name",
        f"{AGENTS}/langgraph/_graph.py",
        "                # No refusal, whatever its name: a graph's models, tools and nodes are all its own.\n"
        "                from_tool(ctx, error)\n",
        "",
        LANGGRAPH,
        "ffbc6af",
    ),
    Mutant(
        "langgraph-prune",
        "a finished turn keeps every checkpoint: the database grows with each step",
        f"{AGENTS}/langgraph/_graph.py",
        "                await saver.aprune([THREAD_ID])\n",
        "",
        LANGGRAPH,
        "ffbc6af",
    ),
    Mutant(
        "errors-weakref",
        "an error of a built-in type (KeyError) raised by a tool is masked by 'cannot create weak reference'",
        f"{AGENTS}/_common.py",
        "        self._errors[id(error)] = error",
        '        __import__("weakref").ref(error)\n        self._errors[id(error)] = error',
        EVERY_FRAMEWORK,
        "ffbc6af",
    ),
    # --- #278 (40f0bbe) ---
    Mutant(
        "client-dot-keys",
        "a key of '.' or '..' stays a dot segment, and the call reaches another path",
        "python/restate/client.py",
        '    return encoded if encoded.strip(".") else encoded.replace(".", "%2E")',
        "    return encoded",
        ("tests/client_paths.py", "tests/agents_object.py"),
        "40f0bbe",
    ),
    Mutant(
        "cli-ctrl-c",
        "Ctrl-C waits for the next line of input before the chat leaves",
        f"{AGENTS}/__main__.py",
        'name="restate-agents-chat-input", daemon=True)',
        'name="restate-agents-chat-input", daemon=False)',
        OBJECT,
        "40f0bbe",
    ),
    # --- #276 (c8ff842) ---
    Mutant(
        "views-optional-body",
        "a view without input refuses the TypeScript client's request (content-type: application/json): 400",
        f"{AGENTS}/_object.py",
        "        async def status(ctx: ViewContext, _: Nothing = None) -> Status:",
        "        async def status(ctx: ViewContext) -> Status:",
        OBJECT,
        "c8ff842",
    ),
    Mutant(
        "langgraph-responses-api",
        "an openai/ model runs on Chat Completions, where reasoning models refuse function tools",
        f"{AGENTS}/langgraph/_agent.py",
        '_OPTIONS: dict[str, dict[str, Any]] = {"openai": {"use_responses_api": True}}',
        "_OPTIONS: dict[str, dict[str, Any]] = {}",
        LANGGRAPH,
        "c8ff842",
    ),
    # --- Durability: the contract of a turn, from no fix ---
    Mutant(
        "langgraph-reask-stored-reply",
        "after a crash, the model is asked again for a final reply that was already stored",
        f"{AGENTS}/langgraph/_agent.py",
        "        if (final := conversation.final_reply()) is not None:\n"
        "            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.\n"
        "            return final\n"
        "        messages = typing.cast(list[BaseMessage], [entry.item for entry in conversation.entries])",
        "        messages = typing.cast(list[BaseMessage], [entry.item for entry in conversation.entries])",
        LANGGRAPH,
    ),
    Mutant(
        "pydantic-reask-stored-reply",
        "after a crash, the model is asked again for a final reply that was already stored",
        f"{AGENTS}/pydantic_ai/_agent.py",
        "        if (final := conversation.final_reply()) is not None:\n"
        "            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.\n"
        "            return final\n"
        "        history = ",
        "        history = ",
        PYDANTIC_AI,
    ),
    Mutant(
        "stored-result-forgotten",
        "a tool call's stored result is not seen: after a crash the tool runs again",
        f"{AGENTS}/_conversation.py",
        "        return result is not None and (not result.failed or result.output == failed)",
        "        return False",
        EVERY_FRAMEWORK,
    ),
]


def failures(files: tuple[str, ...], timeout: float) -> set[str] | None:
    """Run the tests of ``files``; return the names of those that failed, or ``None`` if the run timed out."""
    with tempfile.TemporaryDirectory() as tmp:
        report = pathlib.Path(tmp) / "report.xml"
        command = [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider", f"--junitxml={report}"]
        try:
            subprocess.run(command, cwd=ROOT, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None
        if not report.exists():
            return {"<the run did not report>"}
        failed: set[str] = set()
        for case in ElementTree.parse(report).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(f"{case.get('classname', '')}::{case.get('name', '')}")
        return failed


def is_own(node: str, own: set[str]) -> bool:
    """Whether test ``node`` is one of ``own``: named with its parameters, or without them for all of its cases."""
    name = node.rsplit("::", 1)[-1]
    return name in own or name.split("[", 1)[0] in own


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="the mutants to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list the mutants and exit")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds for each mutant's run (default 1800)")
    args = parser.parse_args()

    chosen = [m for m in MUTANTS if not args.names or m.name in args.names]
    if args.list or not chosen:
        for m in MUTANTS:
            print(f"{m.name:32} {m.kind:10} {m.commit or '-':8} {m.bug}")
        return 0 if args.list else 1

    # Each edit must find its text exactly once, or the mutant is out of date.
    for m in chosen:
        count = (ROOT / m.file).read_text().count(m.old)
        if count != 1:
            print(f"{m.name}: its text is found {count} times in {m.file}; update the mutant", file=sys.stderr)
            return 1

    files = tuple(sorted({f for m in chosen for f in m.tests}))
    print(f"baseline: {len(files)} test files ...", flush=True)
    started = time.monotonic()
    baseline = failures(files, args.timeout) or set()
    print(f"baseline: {len(baseline)} failing, left out ({time.monotonic() - started:.0f}s)", flush=True)
    for name in sorted(baseline):
        print(f"  {name}")

    rows: list[tuple[Mutant, str, list[str]]] = []
    for m in chosen:
        path = ROOT / m.file
        original = path.read_text()
        started = time.monotonic()
        try:
            path.write_text(original.replace(m.old, m.new, 1))
            failed = failures(m.tests, args.timeout)
        finally:
            path.write_text(original)
        if failed is None:
            verdict, seen = "hung", []
        else:
            seen = sorted(failed - baseline)
            own = set(OWN[m.commit])
            others = [n for n in seen if not is_own(n, own)]
            verdict = "caught" if others else "regression only" if seen else "SURVIVED"
        rows.append((m, verdict, seen))
        print(f"{m.name}: {verdict}, {len(seen)} failing ({time.monotonic() - started:.0f}s)", flush=True)
        for name in seen:
            print(f"    {name}")

    print("\n| Mutant | Kind | Fix | Bug | Verdict | Failing tests |\n| --- | --- | --- | --- | --- | --- |")
    for m, verdict, seen in rows:
        print(f"| `{m.name}` | {m.kind} | {m.commit or '-'} | {m.bug} | {verdict} | {len(seen)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
