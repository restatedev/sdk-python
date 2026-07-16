# AI integration tests

End-to-end tests for the AI SDK integrations that live in this repo
(`restate.ext.*`). They run the integration against a **real** restate-server
in two complementary model modes:

- **Scripted:** a deterministic model emits real OpenAI Agents SDK response
  objects. This forces the SDK protocol paths under replay
  without requiring a model credential.
- **Live:** real (cheap) LLM calls validate the upstream provider's response
  types and formats under replay.

The goal is to catch, when an upstream agent SDK (or this SDK) is bumped:

- **journal mismatches / non-determinism** (Restate error `RT0016`),
- **infinite error/retry loops**,
- interference between concurrent invocations (turnstile / session state).

## What is covered

Integration: **openai-agents** (see `openai_service.py`). Every scenario runs
against the scripted model with `always_replay=True` (forces a suspend/replay
on every await -- the HTTP/1.1-style path and the strongest non-determinism
detector) and `disable_retries=True`. The tool-call and multi-turn scenarios
additionally run against the live OpenAI API: 7 scripted + 2 live runs.

These are integration and durability tests, not agent evaluations. Scripted
responses drive the intended OpenAI Agents SDK protocol paths deterministically
(e.g. the turnstile test is guaranteed five parallel tool calls). Live runs
only require normal completion -- their job is catching real provider
response-type drift through the journaling path.

| Test | Model modes | Pattern it stresses |
| ---- | ----------- | ------------------- |
| `test_agent_tool_call_replays_cleanly` | scripted + live | single tool call and LLM-call journaling |
| `test_multi_turn_session` | scripted + live | durable session state across turns + replay |
| `test_concurrent_distinct_keys` | scripted | 20 parallel invocations, no interference |
| `test_parallel_tools_turnstile` | scripted | many tool calls in one turn and turnstile ordering |
| `test_terminal_tool_error_fails_fast` | scripted | `TerminalError` -> terminal failure, no loop |
| `test_local_handoff` | scripted | native openai-agents handoff and agent-graph serialization |
| `test_remote_handoff_serializes_across_rpc` | scripted | durable RPC and pydantic serde |

Detection is deliberately simple: with `disable_retries=True`, a journal
mismatch (Restate error `RT0016`) fails the invocation, so the awaiting call
raises and the test fails. Retries being off also means an "infinite error
scenario" surfaces as a single fast failure instead of a loop.

## Running locally

Requires Docker. `just test-ai` runs the full suite -- both the scripted and
live tests:

```shell
export OPENAI_API_KEY=sk-...
just test-ai
```

The scripted tests need no key; the live tests require `OPENAI_API_KEY` and
fail (rather than skip) without one, since a configured live run is meaningless
without a real model. To run only the scripted half (no key):

```shell
uv run -m pytest ai-tests/ -m "not live_model" -v
```

These tests are **not** part of `just test` / `just verify`. In CI they run on
PRs to `main` (`AI Integration` workflow): the scripted tests on every PR
(including forks), the live tests only when repository secrets are available.
The `AI SDK Bump Test` workflow runs the full suite against a candidate
agent-SDK version before bumping.

## Extending

- **New integration:** add `<name>_service.py` (mirroring an app from
  restatedev/ai-examples) and `test_<name>.py`.
  Keep each integration's deps isolated once more than one is in play.
- **More scenarios:** failure injection (kill the service mid-invocation),
  higher concurrency (100), sequential/orchestrator workflows.
