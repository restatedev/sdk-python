# AI integration tests

End-to-end tests for the AI SDK integrations that live in this repo
(`restate.ext.*`). They run the integration against a **real** restate-server
in two complementary model modes:

- **Scripted:** deterministic models emit real agent-SDK response objects. This
  forces each integration's protocol paths under replay without credentials.
- **Live:** real (cheap) LLM calls validate the upstream provider's response
  types and formats under replay.

The goal is to catch, when an upstream agent SDK (or this SDK) is bumped:

- **journal mismatches / non-determinism** (Restate error `RT0016`),
- **infinite error/retry loops**,
- interference between concurrent invocations (turnstile / session state).

## What is covered

Integrations: **openai-agents** (`openai_service.py`), **pydantic-ai**
(`pydantic_service.py`), **Google ADK** (`google_adk_service.py`), and
**LangChain** (`langchain_service.py`). Each integration runs in its own
isolated dependency environment. Every scenario uses `always_replay=True`
(forces a suspend/replay on every await -- the HTTP/1.1-style path and the
strongest non-determinism detector) and `disable_retries=True`. Tool-call and
multi-turn scenarios also run against the live OpenAI or Gemini API: 28
scripted + 8 live runs.

These are integration and durability tests, not agent evaluations. Scripted
responses drive the intended agent SDK protocol paths deterministically
(e.g. the turnstile test is guaranteed five parallel tool calls). Live runs
only require normal completion and matching tool-call outputs -- their job is
catching real provider response-type drift through the journaling path. The
handlers return generated run items, messages, or normalized ADK event contents
so tests can match tool calls to tool outputs with the same call ID, proving
each tool executed.

LangChain's two live checks are separate `_live` tests. The service always uses
normal module-level `ChatOpenAI` agents. Scripted tests replace only
`ChatOpenAI._agenerate` at the provider boundary, leaving normal tool binding,
agent compilation, and `RestateMiddleware` execution unchanged.

Google ADK follows the same shape: the service uses normal module-level Gemini
agents and explicit `App` / `Runner` setup. Scripted tests replace only
`Gemini.generate_content_async` at the provider boundary. All Google ADK tests
except the remote-RPC scenario run through a virtual object backed by
`RestateSessionService`; the remote-RPC scenario intentionally covers the
stateless service and in-memory session path.

| Test | Model modes | Pattern it stresses |
| ---- | ----------- | ------------------- |
| `test_agent_tool_call_replays_cleanly` | scripted + live | single tool call and LLM-call journaling |
| `test_multi_turn_session` | scripted + live | expected response content + non-empty durable VO session state |
| `test_concurrent_distinct_keys` | scripted | 20 parallel invocations, no interference |
| `test_parallel_tools_turnstile` | scripted | many tool calls in one turn and turnstile ordering |
| `test_terminal_tool_error_fails_fast` | scripted | `TerminalError` -> terminal failure, no loop |
| `test_local_handoff` | scripted | local agent delegation and agent-graph serialization |
| `test_remote_handoff_serializes_across_rpc` | scripted | durable RPC and pydantic serde |

Detection is deliberately simple: forced replay surfaces a journal mismatch in
the SDK, and the harness client timeout prevents a mismatch retry loop from
hanging CI indefinitely. `disable_retries=True` separately makes ordinary
handler failures surface without retry backoff.

## Running locally

Requires Docker. `just test-ai` runs the full suite -- both the scripted and
live tests:

```shell
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=...
just test-ai
```

The scripted tests need no key; the live tests require `OPENAI_API_KEY` for
OpenAI Agents, Pydantic AI, and LangChain, and `GOOGLE_API_KEY` for Google ADK.
They fail (rather than skip) without their key, since a configured live run is
meaningless without a real model. The recipes print full generated messages or
events with pytest capture disabled. To run all scripted suites without keys:

```shell
just test-ai-scripted
```

Use `just test-ai-openai`, `just test-ai-pydantic`, `just test-ai-google-adk`,
or `just test-ai-langchain` to run one integration's full scripted + live
matrix in its isolated environment.

These tests are **not** part of `just test` / `just verify`. In CI they run on
PRs to `main` (`AI Integration` workflow): the scripted tests on every PR
(including forks), the live tests only when repository secrets are available.
The `AI SDK Bump Test` workflow accepts one of the four agent SDKs and a release
tag. It temporarily pins that SDK to the requested version, re-resolves its
dependency family, type-checks the integrations, and runs the selected scripted
and live suite.

Each integration has one versioned direct SDK dependency in `pyproject.toml`.
LangChain's `langchain-core`, `langgraph`, and test-only `langchain-openai`
dependencies are deliberately unversioned: changing the single `langchain`
version floor (or selecting a workflow version) resolves compatible versions of
the whole family.

## Extending

- **New integration:** add `<name>_service.py` (mirroring an app from
  restatedev/ai-examples), `<name>_model_stub.py`, and `<name>_test.py`, plus an
  isolated `Justfile` recipe containing only that integration's optional extra.
- **More scenarios:** failure injection (kill the service mid-invocation),
  higher concurrency (100), sequential/orchestrator workflows.
