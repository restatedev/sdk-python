# AI integration tests

End-to-end tests for the AI SDK integrations that live in this repo
(`restate.ext.*`). They run the integration against a **real** restate-server
and make **real (cheap) LLM calls**.

The goal is to catch, when an upstream agent SDK (or this SDK) is bumped:

- **journal mismatches / non-determinism** (Restate error `RT0016`),
- **infinite error/retry loops**,
- interference between concurrent invocations (turnstile / session state).

## What is covered (v1)

Integration: **openai-agents** only (see `openai_service.py`).

| Axis        | Values                                              |
| ----------- | --------------------------------------------------- |
| Protocol    | HTTP/1.1 (suspends per await) and HTTP/2 (streaming) |
| Replay      | forced replay on every suspension point            |
| Retries     | disabled (a mismatch fails fast instead of looping) |
| Concurrency | 20 invocations on distinct keys; 5 on a shared key  |

Detection is deliberately simple: with `disable_retries=True`, a journal
mismatch (Restate error `RT0016`) fails the invocation, so the awaiting
`object_call` raises and the test fails. Retries being off also means an
"infinite error scenario" surfaces as a single fast failure instead of a loop.

## Running locally

Requires Docker and an OpenAI API key. Without the key the tests **fail** (they
are meaningless without a real model, so a missing key is a hard error, not a skip).

```shell
export OPENAI_API_KEY=sk-...
just test-ai
# or: uv run -m pytest ai-tests/ -v
```

These tests are **not** part of `just test` / `just verify`, and
run in CI only via the `AI Integration` workflow (nightly + manual dispatch).

## Extending

- **New integration:** add `<name>_service.py` (mirroring an app from
  restatedev/ai-examples) and `test_<name>.py`, reusing `ai_harness`.
  Keep each integration's deps isolated once more than one is in play.
- **More scenarios:** human-in-the-loop / awakeables, multi-agent handoff,
  failure injection (kill the service mid-invocation), higher concurrency (100).
- **Upstream-version matrix:** run against `openai-agents@latest` etc. to catch
  breakage ahead of a pin bump.
