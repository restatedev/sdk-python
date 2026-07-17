# AI integration tests

End-to-end tests for the AI SDK integrations in `restate.ext.*`:

- OpenAI Agents
- Pydantic AI
- Google ADK
- LangChain

The tests run each integration against a real Restate server. Their purpose is
to catch breaking changes when either Restate or an upstream agent SDK is
upgraded.

## What we test

Every scenario runs with `always_replay=True`, forcing Restate to suspend and
replay on each await. This exercises the path most likely to expose:

- journal mismatches and other non-determinism,
- infinite failure or retry loops,
- incompatible agent SDK response types,
- interference between concurrent invocations or sessions.

`disable_retries=True` makes ordinary handler failures surface immediately
instead of hiding them behind retry backoff.

The common scenarios cover:

| Scenario | What it checks |
| --- | --- |
| Tool call | The model requests a tool and receives its output |
| Multi-turn session | Conversation state survives across virtual object calls |
| Concurrent invocations | Distinct object keys do not interfere with each other |
| Parallel tools | Multiple tool calls pass through the integration turnstile |
| Terminal tool error | A permanent failure surfaces without retrying forever |
| Local handoff | Delegation between agents is serialized correctly |
| Remote handoff | Agent state and tool results survive a durable RPC |

These are integration tests, not model evaluations. They check that the agent
SDK protocol remains compatible and deterministic, not whether an answer is
subjectively good.

## Model modes

The suite uses two complementary modes:

- **Scripted:** deterministic provider responses exercise the real agent SDK
  types and integration code without credentials.
- **Live:** small real OpenAI or Gemini calls catch upstream response type and
  format changes that scripted responses may miss.

All scenarios run in scripted mode. The tool-call and multi-turn scenarios also
run in live mode.

## Running the tests

Docker is required.

Run all deterministic tests without provider credentials:

```shell
just test-ai-scripted
```

Run all scripted and live tests:

```shell
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=...
just test-ai
```

Run one integration, including its live tests:

```shell
just test-ai-openai
just test-ai-pydantic
just test-ai-google-adk
just test-ai-langchain
```

Each integration runs in an isolated, locked dependency environment. Live tests
fail when their required key is missing; they do not silently skip.

## CI

The `AI Integration` workflow runs scripted tests on every pull request. Live
tests run only for trusted pull requests where repository secrets are
available.

The manually triggered `AI SDK Bump Test` workflow accepts an agent SDK and a
version, resolves that candidate dependency family, type-checks the integration,
and runs its scripted and live tests.

These tests are intentionally separate from `just test` and `just verify`.
