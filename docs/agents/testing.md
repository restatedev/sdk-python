# Testing `restate.agents`: where it stands, and the plan

`restate.agents` makes one promise for every framework it wraps: **each message
is answered once, and each tool call is made once, through crashes, retries,
cancellations and long waits.** The tests should be organised around that
promise. Today they are organised around the frameworks.

This document covers:

- what the tests cover now, and the gaps;
- how good the suite is, measured by bringing past bugs back;
- the strategy: a pyramid, mostly unit tests, then integration, then a few
  end-to-end tests, with the techniques that make it find what hand-picked
  cases miss;
- a plan in small PRs. It follows `sdk-sessions/coverage.md`, which did the same for the
TypeScript packages. Several of its tools carry over: the seeded crash
campaign, the e2e bank of agents, and the nightly soak.

## Running the tests

- **All of them:** `just test` (`uv run -m pytest tests/*`). The tests that
  need a Restate server start one in Docker (`restate.harness`, pinned in
  `tests/support.py`; `RESTATE_IMAGE` overrides it).
- **Without Docker:** `just test-fast` runs the in-process tests
  (`-m "not server"`). The others can run against a local `restate-server`
  (from npm, `@restatedev/restate-server`), through a pytest plugin that
  starts the harness's server as a process instead of a container (none is
  in the repository yet); `examples_agents.py` then registers its program
  as `host.docker.internal`, which must point to 127.0.0.1.
- **The mutants:** `uv run --no-sync python scripts/resurrect.py`, with the
  same server; run it in a worktree of its own, as it edits the code (and
  restores it).
- **Lint and types:** `just lint` and `just typecheck`, per directory, as the
  `Justfile` runs them (mypy works only that way).

## What is covered

There are about 6,000 lines of tests in 19 files, for 9,000 lines of code. They
run in two tiers.

| Tier | How | Used by |
| --- | --- | --- |
| In-process | `sessions_object.Host`: an attempt's context over an in-memory `Backend`, with no Restate | `agents_conversation`, `agents_messages`, `agents_tools`, and the crash tests that monkeypatch a hook |
| Real server | `restate.create_test_harness`: Restate in Docker, with the app on a thread of the test process | each framework's file, `agents_object`, `agents_signals`, `agents_webhook`, `examples_agents` |

Each framework's file runs its agent on a scripted model, which answers from
the first word of the prompt (`weather`, `crash`, `wait`, `refuse`, ...). The
extras' floors are each framework's latest release, so CI tests that release
only, and an install with no extras.

### The contract, per framework

The table below shows which framework tests each behaviour of the shared
contract.

- **H** = against a real server.
- **P** = in-process.
- **—** = not tested.

| Behaviour | OpenAI | Pydantic AI | ADK | LangGraph | LangGraph `graph=` |
| --- | --- | --- | --- | --- | --- |
| Answer, and the `messages` view after it | H | H | H | H | H |
| Tool call | H | H | H | H | H |
| Crash in a tool: same call id, model not asked again | H | H | H | H | H |
| An answer stored before a crash is not asked for again | P | P | P | P (new) | P (checkpointer only) |
| Crash or transient error in a model request | P (partial) | H | **—** | **—** | H |
| Refused (bad key) → 424, and the next message is answered | H | H | H | H | n/a (retried) |
| Stop → 409, and the next message starts afresh | H | H | H | H | H |
| A tool's `TerminalError` is its answer | H | H | H | **—** | H (ends the turn) |
| Bad arguments or an unknown tool go back to the model | H | H | H | H | n/a |
| `output=` | H | H | H | H | H |
| Several prompts in one invocation | H | H | H | H | **—** |
| `PerSession` options | H | H | **—** | H | H |
| The framework's own `agent=` / `graph=` | P, H | H | P | H | H |
| `wait_for_signal` through the framework | **—** | partial | **—** | **—** | H |
| `call` / `send` / `workflow` / `subagent` through the framework | **—** | **—** | **—** | `call` only | through nodes, not `agent(graph=)` |
| History from snapshots, compacted by the framework | n/a | P (the harness's `TieredCompaction`) | n/a | n/a | n/a |
| A deferred tool call waits for a signal (`wait_for`) | n/a | H | n/a | n/a | H (interrupts) |
| A stopped turn doesn't report a call done because its approval returned | n/a | P | n/a | n/a | n/a |
| The summary in the framework's own form (`Items.summary`) | H (one user message) | n/a | H | H | n/a |
| `background=` | **—** | **—** | **—** | **—** | **—** |
| `clear` / cursors / `sync` with this framework's items | **—** | **—** | **—** | **—** | **—** |

The shared core is tested once, through a made-up framework whose items are
plain dicts (`agents_conversation.ITEMS`):

- `agents_conversation`: run-once, and closing a run that stopped or failed.
- `agents_messages`: pages, cursors, drops and `clear`.
- `agents_object`: the views (`status`, `stop`, `signal`, `sync`).
- `agents_tools`: `call`, `wait_for_signal` and `subagent`.
- `agents_snapshots`: history from snapshots (loading, `history()`, known
  results, crashes, `compact` between turns, only the latest kept), each
  turn's `asked_at`, and `wait_for` (a wait per tool call, and an approval
  that is not the call's outcome).

## Gaps

They are ranked by what a failure would cost users and by what no other layer
would catch.

1. **No crash point is swept: each is chosen by hand.**
   - Every agent crash test raises once at one place: a `flaky` tool, or a
     monkeypatched hook such as `on_llm_end`, `after_model_request` or
     `append_event`. It then re-invokes the same request.
   - No agent test uses `Host.crash_after(n)`. That is the one mechanism that
     loses unsynced writes, as a real crash does.
   - Nothing loops over the sync points of a turn. Only
     `sessions_database.test_lost_writes_leave_every_reader_consistent` sweeps,
     and only for the storage.
   - The bugs that matter here sit at a boundary: the model answered but the
     reply is not stored yet, or the tool's journal entry is stored but its row
     is not. A hand-picked point finds the boundaries someone already thought of.
2. **No end-to-end test kills a process.**
   - Nothing runs agents in their own process, SIGKILLs it at random points
     and checks that money moved once.
   - The TypeScript `e2e/crash.ts` does this for every one of its frameworks,
     and found a lost-send bug that no unit test saw. Python has no equivalent.
   - No test runs with `always_replay=True`, which forces every step to replay
     from the journal.
3. **The contract is tested four times, unevenly.**
   - The OpenAI, Pydantic AI, ADK and LangGraph files each re-implement the
     same scaffolding:
     - the scripted model and its word dispatch;
     - the `flaky`, `blocking`, `forbidden` and `get_weather` tools;
     - the `post`, `chat`, `messages` and `until` helpers;
     - nine near-identical contract tests.
   - The holes in the table above follow from this: each file covers what its
     author thought of. A behaviour added to the contract has to be added four
     times, and in practice it isn't.
4. **Restate tools are not tested through the frameworks.**
   - `send()` and `workflow()` (`_tools.py`) never run in any test.
   - `wait_for_signal`, `call` and `subagent` are tested through the made-up
     framework and LangGraph's nodes, not through each framework's own
     tool-calling path.
   - That path is where the call id, the arguments and the result are mapped.
5. **Untested branches in the core.**
   - `_conversation.py`: a later prompt finishing an earlier prompt's run that
     was cut off (the `running.prompt != prompt_id` branches of `_answer`).
   - `_common.stored_answer`: a retry of a refused prompt raising 424 again.
   - `_turn.py`: `AttemptEnded` in the middle of a turn. `Host` requests never
     finish, so only the LangGraph suspension test reaches it.
   - `_object.py`: `background=`; `clear` while a turn runs; a refusal from an
     agent's own `@handler`.
   - `_signals.py`: a signal with neither id nor name; `deliver()` from a
     handler; re-watching after `WATCH` expires.
   - `client.py`: `_synced` retrying on 5xx, 429 and transport errors;
     `chat(idempotency_key=)`; `messages(before=, cursor=)`;
     `signal(id=, keep=)`.
   - `__main__.py`: the `/signal`, `/pending`, `/stop` and `/clear` commands, an
     unknown command, and how waits are shown.
6. **The wire format is untested.**
   - Several things are shared with Restate's other SDKs, so that one client
     follows an agent written in any of them:
     - the camelCase keys (`invocationId`, `timeoutSeconds`);
     - handler names such as `awaitTurn`;
     - the shapes of `Messages`, `Status`, `Waiting` and `Signal`.
   - No test pins them. A rename passes the Python suite and breaks the
     TypeScript client and UI.
7. **The fakes may drift from the real models.**
   - Every test runs on a scripted model.
   - When OpenAI, Gemini or Anthropic change a response shape, the fakes keep
     passing until a user hits it.
   - Only each framework's latest release is supported: the extras' floors
     are it. A newer release is tested only once the floors move to it.
8. **Most examples only load.**
   - `examples_agents.py` runs the tutorial, `refund_graph`, `loop` and
     `create_agent` end to end.
   - The basic OpenAI, Pydantic AI, ADK and LangGraph agents, and `support`,
     `followup`, `team`, `router` and `triage`, are served but never answer a
     message.
   - The tutorial in `modal-template/python` (lessons 09–16) uses these APIs and
     has no tests.
9. **The test hygiene adds flakiness and slows CI.**
   - **Reproducibility.** The harness ran `restatedev/restate:latest`, so a PR
     could turn red without a code change. (Pinned since: see the plan's status.)
   - **Containers.** Each module starts its own session-scoped harness, so about
     12 Restate containers stay up for the whole run. There was no `conftest.py`
     (one now marks the server tests; one shared harness is still to do).
   - **Sharing state across threads.**
     - `agents_object` set an `asyncio.Event` (`gate`), awaited on the server
       thread's loop, from the test's loop (a thread-safe `Gate` since).
     - `agents_langgraph_nodes` never resets its `threading.Event`s, so a test
       fails when rerun in the same process.
     - Monkeypatches of module globals (`DEFAULT_RECURSION_LIMIT`,
       `_graph.terminal_of`) apply while the shared server thread runs.
   - **Timing.** Fixed sleeps (0.5 s, 2.5 s) stand in for conditions, and the
     `blocking` tools fall back on the 60 s HTTP timeout if `stop` breaks.
   - **Running separately.** The tests can't run without Docker, can't run in
     parallel (xdist), and coverage is not measured.

## How good the suite is: past bugs brought back

`scripts/resurrect.py` measures the suite against bugs that really happened.

- **39 history mutants** (40 until one was retired, below). Each brings back,
  on today's code, one bug that a fix commit describes (#276, #278, #290,
  #291, #292, #294, the review of the storage plan's implementation, and the
  measurement of [access patterns](access-patterns.md)).
- **13 durability mutants.** Each breaks the contract of a turn without
  matching any past fix.

For each mutant, the runner runs the test files that use the code it changes.
It then asks: which tests fail?

- **Caught:** a test other than the fix's own regression tests fails. The
  suite would have found the bug without them.
- **Regression only:** only the tests that the fix commit added fail.
- **Survived:** nothing fails.

The run was against Restate 1.8.1 on 7 November 2026. The baseline was 283
passed and 1 skipped, in 2m16s, and the mutants took about 40 minutes.

| Mutant | Kind | Bug brought back | Verdict |
| --- | --- | --- | --- |
| `openai-approval-resume` | #294 | a cut-off call that needs an approval runs on the retry, without it | regression only |
| `openai-approval-run` | #294 | a run stopped for approvals answers "null", and the next message runs the call | regression only |
| `openai-approval-function` | #294 | `needs_approval` read by truthiness | regression only |
| `openai-refusal-window` | #294 | an error of the agent's own code is taken for a refusal | regression only |
| `openai-missing-key` | #294 | a missing API key is retried forever | **caught** |
| `openai-setup-error` | #294 | the SDK's setup errors are retried forever | regression only |
| `openai-tool-timeout` | #294 | a resumed call bypasses its tool's timeout | regression only |
| `openai-tool-guardrails` | #294 | a cut-off call to a guarded tool bypasses its guardrails | regression only |
| `pydantic-system-prompt` | #292 | an Agent of your own loses its system prompt each turn | regression only |
| `pydantic-usage-limit` | #292 | `UsageLimitExceeded` retried with a fresh limit | regression only |
| `pydantic-gave-up` | #292 | `UnexpectedModelBehavior` retried forever | regression only |
| `pydantic-unreadable-response` | #292 | one unreadable response ends the turn | regression only |
| `pydantic-user-error` | #292 | `UserError` retried forever | regression only |
| `pydantic-refusal-by-name` | #292 | an error of the agent's own code is a refusal by its name | regression only |
| `pydantic-deferred` | #292 | a deferred tool call answers with its repr | regression only; retired on 8 November |
| `pydantic-deps` | #292 | an Agent of your own gets no deps | regression only |
| `pydantic-model-error` | #292 | a model that cannot be made is not a refusal | **caught** |
| `langgraph-own-errors` | #291 | recursion limit and invalid update retried forever | regression only |
| `langgraph-invalid-update-state` | #291 | an invalid update fails every later read of the state | regression only |
| `langgraph-static-breakpoints` | #290 | a graph with static breakpoints is accepted | regression only |
| `langgraph-subgraph-breakpoint` | #290 | a run stopped at a subgraph's breakpoint answers half done | regression only |
| `langgraph-refusal-by-name` | #290 | a graph's error is a refusal by its name | regression only |
| `langgraph-prune` | #290 | a finished turn keeps every checkpoint | regression only |
| `errors-weakref` | #290 | a tool's `KeyError` is masked by "cannot create weak reference" | **caught** |
| `client-dot-keys` | #278 | a key of `.` or `..` reaches another path | regression only |
| `cli-ctrl-c` | #278 | Ctrl-C waits for a line of input | regression only |
| `views-optional-body` | #276 | a view refuses the TypeScript client's request | **caught** |
| `langgraph-responses-api` | #276 | OpenAI models run on Chat Completions | regression only |
| `langgraph-prune-or-chain` | review | the prune matches the kept versions with an `OR` of each: a `DeltaChannel` graph fails every turn from about its eleventh | regression only |
| `langgraph-values-or-chain` | review | a checkpoint of more than about 95 channels can't be read | regression only |
| `wait-rejoins-another-call` | review | a framework's new call with the same input joins an earlier call's wait, and gets its approval or denial | regression only |
| `approval-is-the-calls-outcome` | review | a turn stopped after a denial reports the denied call as done | **caught** |
| `call-matched-by-id-alone` | review | an approved tool's own Restate call shares its approval's record | regression only |
| `snapshots-kept` | review | every snapshot is kept, though only the latest is read | regression only |
| `pydantic-approval-not-marked` | review | a Pydantic AI approval is taken for the call | regression only |
| `pydantic-plan-reads-live` | review | a tool call that runs again reads the plan as its first run left it, and a step stays blocked | regression only |
| `pydantic-memory-run-ids` | review | a replayed memory edit misses its receipt and reports failure | regression only |
| `pydantic-memory-paths-unlimited` | review | `list_paths` ignores its limit | regression only |
| `pydantic-import-without-harness` | review | without Pydantic AI Harness, the import names no extra | regression only |
| `last-of-scans` | access patterns | every turn scans both tables for the last position and seq, and a cold one reads the whole database | regression only |
| `pydantic-reask-stored-reply` | durability | after a crash, the model is asked again for a stored final reply | **caught** |
| `calls-new-id-not-matched` | durability | a call asked again under a new id makes its Restate call again | **caught** |
| `langgraph-reask-stored-reply` | durability | after a crash, the model is asked again for a stored final reply | **SURVIVED**, now caught |
| `calls-settled-early` | durability | a returned call is forgotten before its result is stored; after a crash, the model asking again under a new id makes it again | **SURVIVED**, now caught |
| `calls-never-settled` | durability | a call's record is never forgotten: the key's state grows with every call | **SURVIVED**, now caught |

**The run of 8 November 2026**, after the frameworks moved to their latest
releases ([The frameworks](frameworks.md)), against Restate 1.8.1:

- **History:** 6 caught, 21 regression only. `pydantic-system-prompt` and
  `langgraph-prune` are now caught by other tests too.
- **`pydantic-deferred` survived, and is retired.** Deferred tool calls now
  wait for a signal (`handle_deferred_tool_calls`), so no run stops for them.
  Its regression test went with the old behaviour, and the guard it removes
  stays only as a safety net.
- **Durability: all 13 caught.** That is the 5 above, plus 8 new ones:
  - `snapshot-rows-twice`, `snapshot-results-unknown`,
    `snapshot-loads-everything`, `snapshot-hides-unanswered-turn` and
    `snapshot-compacted-every-turn`, for the core's snapshots;
  - `pydantic-compaction-not-snapshotted` and `pydantic-denial-approves`, for
    the Pydantic AI agent;
  - `tool-cancellation-swallowed`, for a tool whose task the framework
    cancels.

**The review's fixes**, on 8 November, against Restate 1.8.1: the 11 new
history mutants are all caught, `approval-is-the-calls-outcome` by the Pydantic
AI agent's test too, the other 10 by their own tests only.
`calls-new-id-not-matched` is still caught by `agents_tools`, now that
`wait_for` no longer rejoins an earlier attempt's call.

What it says (the run of 7 November):

- **Every past bug is caught today, but almost only by its own regression
  test.** 24 of 28 are caught only by the test that their fix added; 4 (14%)
  would have been caught by the rest of the suite. The suite records the bugs
  it has met, and finds few new ones. That is the case for shared,
  framework-independent tests: the conformance suite, the error tables, the
  sweeps.
- **12 of the 28 past bugs are error classification.** That is job 5: a
  refusal, a retry or a terminal error decided wrongly, as with
  `*-refusal-*`, `*-setup-error`, `*-usage-limit`, `*-gave-up`,
  `*-own-errors`, `*-model-error` and `errors-weakref`. A table of errors
  against outcomes, per framework, at the unit level, would have caught most
  of them before release. 8 more are in resuming (job 3).
- **"Retried forever" bugs fail slowly.** Such a mutant takes 75 to 280
  seconds, against about 13 for the others, because the test waits for its
  `until()` timeout while Restate retries. A test harness with a small retry
  budget, or an assertion on the number of attempts, would fail them at once.
- **Two predicted gaps are confirmed, and one is new.**
  - LangGraph's built-in agent has no test that a stored reply isn't asked
    for again; the coverage table had marked it missing.
  - Nothing checks that a returned Restate call is kept until its result is
    stored. The code looks right; the point is that no test would notice if it
    stopped being right, and a double effect is the bug users can't afford.
    This is what the crash sweeps (plan #4) will cover.
  - Nothing checks that call records are ever cleaned up.

  Two tests now catch all three, each brought back and seen to fail:

  - `agents_langgraph.test_an_answer_stored_before_a_crash_is_not_asked_for_again`.
  - `agents_tools.test_a_returned_call_is_kept_until_its_result_is_stored_and_forgotten_once_it_is`.
    It runs a turn on the made-up framework through a crash between a call's
    return and its stored result, then a retry under a new call id. It checks
    that the same idempotency key is used and that no record is left.

Run it with `uv run --no-sync python scripts/resurrect.py` (with Docker), or
pass mutant names to run some. Every future fix should add a mutant.

## What an integration does, and so what to test

Each integration is a thin layer between its framework's agent loop and
Restate. It has five jobs. Each job fails in its own way, and each is
cheapest to test at its own level.

| Job | Where | What goes wrong |
| --- | --- | --- |
| 1. Translate messages: framework message ↔ stored row, and what each holds (text, tool calls, tool results) | `_Items` in each `_agent.py` | a call is lost or misread after a reload |
| 2. Store each step as it happens, from the framework's hooks | OpenAI `_Hooks`, Pydantic AI `_Restate`, ADK `_Sessions`, LangGraph's nodes | a step is stored twice, or not at all, before a crash |
| 3. Resume: rebuild the framework's state, skip calls already made, don't ask again for a stored reply | OpenAI `_resume`, `_current`; ADK `_final`; `final_reply` | a tool runs twice, or the model is asked again |
| 4. Wrap tools: schema, argument validation, errors back to the model | `_function_tool`, `toolset`, `_AdkTool`, `_RestateTool` | a wrong schema, or bad arguments crash the turn |
| 5. Classify errors: refusal (424), retry, or terminal | `refusal_of`, `terminal_of`, each `_run`'s `except` | a bad key retried forever, or a blip fails the turn |

Of the 28 bugs that the fix commits describe (see "How good the suite is"
above), 12 are in job 5 and 8 are in job 3.

## The strategy: a pyramid

Today the pyramid is upside down for the integrations. Every framework
behaviour is tested against a real Restate server: 133 of the 240 tests in
`tests/agents_*.py` need Docker (the other 107 run in 24 seconds without it).
The fix is to move the contract down to the in-process level, where a turn
costs milliseconds and every crash point can be tried, and keep the server
for what only a server shows.

### Unit (about 70%): milliseconds, no server, no Docker, no network

- **(a) Pure functions, per framework (jobs 1, 4 and 5), as tables.**
  - Round trips: `load(dump(m)) == m` and `read(m)` for every kind of message
    (text, a call, parallel calls, a tool error, structured output, a
    handoff), generated by Hypothesis from the frameworks' Pydantic types.
  - Tool schemas, and bad arguments returned to the model.
  - **Error classification:** a table of exceptions (each provider's
    authentication error, rate limit, timeout, a `TerminalError`, each
    framework's own errors, a setup error, an `ImportError`) against the
    outcome: refused, retried or terminal. This is where most past bugs were.
- **(b) One turn, everything real except Restate (jobs 2 and 3).**
  - The real framework, the real integration and the real SQLite database,
    with `Host` as Restate and a scripted model, the teller. One turn is a
    function call.
  - The shared contract is written once, as a conformance suite run for every
    framework through a small adapter. The rows of the table above become its
    scenarios, including the missing cells.
  - **Crash sweeps:** each scenario runs clean, counting its syncs `N`, then
    once for each `n` in `0..N` with `crash_after(n)`, retried until it ends.
    The invariants: each prompt answered once, each ledger effect once, the
    teller not asked again for a stored step, the clean run's answer and
    `messages`.
  - Prerequisite: `Host` keeps a journal of completed `run` and `call` entries
    across attempts. Today it forgets more than Restate does.
- **(c) The core with the made-up framework** (`agents_conversation`,
  `agents_messages`, `agents_tools`), plus a Hypothesis state machine of the
  conversation and its `messages` view against a plain list.

### Integration (about 25%): seconds, a real Restate server

Only what `Host` fakes or cannot show, once per framework rather than once per
behaviour:

- HTTP ingress, registration, the plain-text `chat` body and the JSON shared
  with the other SDKs.
- Suspension over a long `wait_for_signal`; cancellation by `stop` and by the
  admin API; attach; the retry policy; idempotency keys; views during a turn.
- `restate.agents.client` and `python -m restate.agents chat`.
- A **differential test** that keeps `Host` honest: a few scenarios run on
  both, and their final states must match.

### End-to-end (about 5%): minutes

- **Chaos:** agents of every framework in their own processes, killed at
  seeded random points while the server restarts; every prompt must move
  money exactly once. A port of `sdk-sessions/packages/e2e/crash.ts`, whose
  driver may be reused, since the wire format is shared.
- **Nightly, never blocking:** a soak; the newest and oldest allowed version
  of each framework; Restate `latest`; one smoke test per framework on a real
  model.

## The techniques

These make the levels above find more than hand-picked cases do.

- **Bug resurrection** (`scripts/resurrect.py`, below). The bugs that the fix
  commits describe, brought back one at a time. It measures the suite against
  bugs that really happened, and grows with each fix.
- **Oracles that need no expected value**, on the crash sweeps:
  - *Crash invariance:* a crashed and retried run ends as the clean one.
  - *Same requests to the model:* after a resume, each request the teller
    gets equals the clean run's at that step: history, order of results,
    system prompt, current agent. This catches a resume that rebuilds the
    state slightly wrong even when the answer is still right, and guards the
    provider's prompt cache.
  - *Framework against framework:* the same script gives the same `messages`
    in all four, apart from documented differences.
- **Generated scripts** (Hypothesis stateful testing): the teller's script
  and the client's actions (prompt, crash, retry, stop, signal, clear, read
  with a cursor) are generated; the contract is checked after every step;
  failures shrink to the shortest script that breaks it.
- **Failpoints:** named points in the code (`failpoint("openai.after_llm_end")`,
  a dict lookup when off) where a test injects a crash, an error, a
  cancellation, the end of the attempt, or a delay. The sweep runs every point
  × every fault, including points between syncs (after a tool's effect,
  before its return) that `crash_after` cannot reach. They replace today's
  monkeypatches of private hooks, which break on refactors.
- **Mutation testing:** mutmut on `python/restate/agents`, on the unit level
  only, for changed lines on PRs and in full weekly; plus hand-written
  durability mutants (store after the effect, skip a sync, forget a stored
  result), as `scripts/resurrect.py` holds.
- **Later:** a seeded scheduler for asyncio (parallel tool calls, a stop
  racing a commit), recorded real responses to check the teller's shapes, and
  ids on the docstrings' promises cited by the tests.

## Hygiene, first

- `tests/conftest.py` with one session-scoped harness for every agent module,
  and markers: `server` (done), then `e2e` and `live`.
- `just test-fast` (`-m "not server"`, no Docker; done), `just test` (all but `e2e` and
  `live`), `just e2e`.
- Pin the Restate image by version or digest; update it on purpose (done: `tests/support.py`).
- A fixture that resets every gate and counter; thread-safe gates for what the
  test sets on the server's loop; no monkeypatched globals while a shared
  server runs.
- `until(condition)` instead of sleeps.
- Branch coverage of `python/restate/agents` per module in the job summary,
  with a ratchet rather than a target.
- Nightly `pytest -p randomly` and `--count=5` on the server level, to measure
  flakiness.

## The plan

In this order. "PR" runs on every push; "nightly" is scheduled, with logs
uploaded on failure.

Status:

- **#1 is done, but for its nightly job.** `scripts/resurrect.py` exists, and
  the tests for its three survivors.
- **#2 is begun.** Done so far:
  - The Restate image is pinned (`tests/support.py`, `RESTATE_IMAGE`
    overrides it).
  - Tests that need a server are marked `server` (`tests/conftest.py`), and
    `just test-fast` runs the 238 others without Docker in about 16 seconds.
  - `agents_object` opens its gate safely across threads.

  Still to do: one shared harness, coverage with a ratchet, resetting the
  `threading.Event`s of `agents_langgraph_nodes`, and a retry budget for
  tests.

| # | Change | Runs | Size |
| --- | --- | --- | --- |
| 1 | Bug resurrection: `scripts/resurrect.py` (done), its survivors turned into tests, and a nightly job that fails when a mutant survives | PR, nightly | Small |
| 2 | Hygiene: `conftest.py`, markers, pinned image, thread-safe gates, `just test-fast`, coverage with a ratchet | PR | Small |
| 3 | The teller, the adapters, the bank and ledger tools; the conformance suite at the unit level, with its missing cells; the server tests it replaces deleted | PR | Medium |
| 4 | `Host` with a journal; the crash sweeps, with the crash-invariance and same-requests oracles | PR, nightly | Medium |
| 5 | Error-classification and round-trip tables per framework; the Hypothesis model of the conversation | PR | Small |
| 6 | Generated scripts (Hypothesis stateful) on the sweeps | PR, nightly | Medium |
| 7 | Failpoints, replacing the monkeypatches | PR | Medium |
| 8 | Mutation testing, generic and durability, weekly | Weekly | Small |
| 9 | Golden wire-format fixtures, shared with `sdk-sessions`; the `Host` differential test | PR | Small |
| 10 | End-to-end chaos with `ABORT_SEED`; nightly soak, version and live-model jobs | PR (short), nightly | Large |

Not planned, on purpose: real models in PR CI (slow, paid, nondeterministic,
no secrets on forks); tests of Restate's own guarantees (its project's); token
streaming, which no integration offers yet.
