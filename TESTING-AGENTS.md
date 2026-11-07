# Testing `restate.agents`: where it stands, and the plan

`restate.agents` makes one promise for every framework it wraps: **each message
is answered once, and each tool call is made once, through crashes, retries,
cancellations and long waits.** The tests should be organised around that
promise. Today they are organised around the frameworks.

This document has three parts: what the tests cover now, the gaps, and a plan
in small PRs. It follows `sdk-sessions/coverage.md`, which did the same for the
TypeScript packages. Several of its tools carry over: the seeded crash
campaign, the e2e bank of agents, and the nightly soak.

## What is covered

There are about 6,000 lines of tests in 19 files, for 9,000 lines of code. They
run in two tiers.

| Tier | How | Used by |
| --- | --- | --- |
| In-process | `sessions_object.Host`: an attempt's context over an in-memory `Backend`, with no Restate | `agents_conversation`, `agents_messages`, `agents_tools`, and the crash tests that monkeypatch a hook |
| Real server | `restate.create_test_harness`: Restate in Docker, with the app on a thread of the test process | each framework's file, `agents_object`, `agents_signals`, `agents_webhook`, `examples_agents` |

Each framework's file runs its agent on a scripted model, which answers from
the first word of the prompt (`weather`, `crash`, `wait`, `refuse`, ...). CI
already tests the oldest LangGraph and Pydantic AI that the extras allow, and an
install with no extras.

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
| An answer stored before a crash is not asked for again | P | P | P | **—** | P (checkpointer only) |
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
| `background=` | **—** | **—** | **—** | **—** | **—** |
| `clear` / cursors / `sync` with this framework's items | **—** | **—** | **—** | **—** | **—** |

The shared core is tested once, through a made-up framework whose items are
plain dicts (`agents_conversation.ITEMS`):

- `agents_conversation`: run-once, and closing a run that stopped or failed.
- `agents_messages`: pages, cursors, drops and `clear`.
- `agents_object`: the views (`status`, `stop`, `signal`, `sync`).
- `agents_tools`: `call`, `wait_for_signal` and `subagent`.

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
   - Only LangGraph and Pydantic AI are tested on their oldest allowed version.
     None of the four is tested on its newest release before users get it.
8. **Most examples only load.**
   - `examples_agents.py` runs the tutorial, `refund_graph`, `loop` and
     `create_agent` end to end.
   - The basic OpenAI, Pydantic AI, ADK and LangGraph agents, and `support`,
     `followup`, `team`, `router` and `triage`, are served but never answer a
     message.
   - The tutorial in `modal-template/python` (lessons 09–16) uses these APIs and
     has no tests.
9. **The test hygiene adds flakiness and slows CI.**
   - **Reproducibility.** The harness runs `restatedev/restate:latest`, so a PR
     can turn red without a code change.
   - **Containers.** Each module starts its own session-scoped harness, so about
     12 Restate containers stay up for the whole run. There is no `conftest.py`.
   - **Sharing state across threads.**
     - `agents_object` sets an `asyncio.Event` (`gate`), awaited on the server
       thread's loop, from the test's loop.
     - `agents_langgraph_nodes` never resets its `threading.Event`s, so a test
       fails when rerun in the same process.
     - Monkeypatches of module globals (`DEFAULT_RECURSION_LIMIT`,
       `_graph.terminal_of`) apply while the shared server thread runs.
   - **Timing.** Fixed sleeps (0.5 s, 2.5 s) stand in for conditions, and the
     `blocking` tools fall back on the 60 s HTTP timeout if `stop` breaks.
   - **Running separately.** The tests can't run without Docker, can't run in
     parallel (xdist), and coverage is not measured.

## The strategy

There are five layers. Each one answers a question the others can't.

| Layer | Question | Runs | Speed |
| --- | --- | --- | --- |
| **1. Conformance suite** | Does every framework keep the contract? | PR, in-process and real server | seconds per framework |
| **2. Crash sweeps** | Does any single crash point break the contract? | PR (bounded), nightly (all) | seconds to minutes |
| **3. Model-based property tests** | Do the conversation storage and its views stay coherent under any sequence of operations? | PR | seconds |
| **4. E2E chaos** | Does it hold with real processes killed, a real server restarted, and several frameworks at once? | PR (short), nightly (long, soak) | minutes |
| **5. Drift canaries** | Do new framework, server and model versions still fit? | nightly, allowed to fail | minutes |

Hygiene (shared fixtures, markers, pinned images, coverage) comes first,
because every layer depends on it.

### 1. One conformance suite, run against every framework

The contract is written once, as tests that take a **framework adapter**, and
pytest runs them for every framework:

```python
# tests/agents/conformance/adapter.py
class Framework(Protocol):
    name: str
    def agent(self, name: str, *, model: Teller, tools: list[Tool], **options) -> AgentObject: ...
    def prompt(self, ctx, text: str, *, model: Teller, **options) -> Awaitable[Any]: ...
    def own_agent(self, name: str, *, model: Teller, tools: list[Tool]) -> AgentObject: ...  # agent= / graph=
    # The framework's own crash points, for the sweeps: the hooks that store a step.
    store_points: Sequence[str]
```

- **The teller.** A single scripted model, `Teller`, reads a small script language from the prompt and turns each step
  into the framework's native response types. Examples: `say <text>`,
  `call <tool> <json>`, `call* <tool> <json> x3` (parallel calls), `refuse`,
  `fail-once`, `output <json>`.
  - Each adapter only converts the teller's steps into its framework's own
    response objects (`ModelResponse`, `LlmResponse`, `AIMessage`, Pydantic AI's
    `ModelResponse`).
  - The teller records every request, so a test can assert "asked once".
  - This mirrors the TypeScript `test/fakes/fakes.ts`.
- **The tools.** `get_weather`, `flaky`, `blocking` and `forbidden` move into
  the suite.
  - The bank (`transfer`, `audit`) is added, with the tool call id as the
    transfer id, as in TypeScript `test/bank.ts`.
  - Every effect is counted in a ledger object, by call id, so "made once" can
    be asserted.
- **The scenarios.** Every row of the table above becomes one test, applied to
  every column:
  - a scenario that doesn't apply to a framework is skipped, with the reason
    stated (for example, handoffs outside OpenAI and ADK);
  - a scenario that applies but isn't supported yet is marked xfail.
  - The rows include the ones missing today: Restate tools through each
    framework, `background=`, `clear` and cursors, and `PerSession` for ADK.
  - The holes then become visible in the test report.
- **What stays in each framework's file.** Only what belongs to that framework:
  OpenAI's guardrails and handoff filters, LangGraph's checkpointer and
  interrupts, Pydantic AI's capabilities, ADK's plugins and `SequentialAgent`.
- **Possibly public.** `restate.agents.testing`, offering `Teller` and an
  in-process `run_turn()`, would let users test their own agents the same way.
  The modal-template tutorial could then be tested with it, which closes gap 8
  there.

The existing per-framework contract tests are deleted once the suite covers
them. The goal is fewer lines in total, with more cells covered.

### 2. Crash sweeps: every crash point, not a chosen one

A scenario runs in-process on `Host` and counts the syncs of a clean run, `N`.
It then runs once for each `n` in `0..N`:

1. `crash_after(n)`, which loses that sync's writes as a crash would;
2. re-invoke with the same request, as Restate's retry does;
3. repeat until the invocation ends;
4. check the invariants.

The invariants are the contract, stated as checks on the final state:

- The `messages` view has each prompt once, in order, each followed by its
  answer. There are no orphan tool calls (a call without its result), except in
  a run that is closed on purpose.
- Each side effect happened once, as recorded by the ledger by call id.
- The teller was not asked again for a step whose reply was already stored. A
  reply cut off by a crash may be asked for again, which the sweep shows exactly.
- Ids only increase, and a cursor follower that read between the attempts sees
  every change once and never goes back.
- The final answer equals the answer of the clean run.

More about the sweeps:

- **Two crashes per run.** Double crashes (`n1`, `n2`) are swept on a seeded
  sample, since the pairs grow as N².
- **Where it runs.** In PR CI, every single crash point of the core scenarios
  runs for every framework (one answer, one tool call, two parallel tool calls,
  a refusal, a wait). Pairs and all scenarios run nightly. A failure prints its
  scenario, framework and `n` (or the seed), and those are enough to replay it.
- **Prerequisite: `Host` needs a journal across attempts.**
  - Today `Host` has no VM. `ctx.run` results and call answers are not kept
    from one attempt to the next, so a sweep would test a host that forgets more
    than Restate does.
  - `Host` should keep a journal of completed `run` and `call` entries, keyed
    by their order. A crash keeps everything up to the last completed entry.
  - The crash then lands where it does on a real server: after the journal and
    before the database sync, or the other way around.
  - This is the one real piece of infrastructure in the plan, and it is worth
    building with the sessions tests, which need it too.
- **Real-server sweeps.** On the real server, a nightly run of the conformance
  suite with `always_replay=True` and with `disable_retries=False` covers the
  replay path that `Host` can't.

### 3. Model-based property tests for the conversation

Use `hypothesis` stateful testing (`RuleBasedStateMachine`) on
`Conversation`, `_messages` and the start table.

- **Operations:** `ask`, `append` a reply with calls and results, `answered`,
  drop the reply at the end, `clear`, `restart`, and a crash, `Backend.lose()`,
  between any two of them.
- **The model:** a plain Python list of messages, a `seq` counter and a start
  position.
- **Invariants:**
  - `messages(limit, before)` pages the model's list, oldest first, with no
    gaps or duplicates.
  - A cursor follower that reads after every step ends up with exactly the
    model's messages, keyed by id.
  - `first` appears exactly after a `clear`.
  - `seq` never decreases, even after a drop.
  - Loading the conversation again gives back the same items.

This replaces hand-written edge cases with generated ones and finds the cursor
bugs that clients would hit. It runs in seconds and needs no framework.

### 4. End-to-end chaos: real processes, killed

Port the agents' part of `sdk-sessions/packages/e2e/crash.ts`. Better still,
reuse its driver, since it speaks HTTP and the wire format is shared.

- **Setup.**
  - Each framework's agent runs in its own `restate.serve` process, wrapped by
    a fault injector. With probability `ABORT_PER_MILLE`, it calls `os._exit(1)`
    on a state request or a journal write.
  - A ledger object holds the accounts.
  - A client sends prompts such as "move 5 from 1 to 2", and resends each one
    until it is answered.
  - The Restate server is killed and restarted twice during the run.
- **Checks.**
  - Every prompt moved money exactly once.
  - The total amount of money is unchanged.
  - Each conversation has each prompt once, in order.
  - `status` and `messages`, read throughout, only ever show committed states.
- **Seeds.** Every random choice comes from `ABORT_SEED`, which is printed at
  the start and on failure.
- **Where it runs.**
  - PR CI: about 3 minutes, a few keys per framework.
  - Nightly: a `SOAK_MINUTES=45` run with thousands of keys, a cap on each
    process's memory, a share of the invocations cancelled mid-turn, and some
    long `wait_for_signal` waits resolved late. Python's per-turn allocations,
    such as reloading the whole conversation every turn, would show up here.
- **Cross-SDK.** A TypeScript client following a Python agent pins gap 6 from
  the outside. Golden JSON fixtures of `Messages`, `Status`, `Waiting`,
  `Signal` and `Resolved`, kept in one place and checked by both SDKs, pin it
  from the inside.

### 5. Drift canaries (nightly, allowed to fail, with a summary)

- **Newest dependencies.** The conformance suite on the newest release, and the
  prereleases, of `openai-agents`, `pydantic-ai-slim`, `google-adk`, `langgraph`
  and `langchain-core`, with nothing pinned. A framework's breaking change shows
  up within a day, not at a user's upgrade.
- **Oldest dependencies.** Oldest-allowed jobs for `openai-agents` and
  `google-adk`, beside the existing LangGraph and Pydantic AI ones. The step is
  already generic: it reads the extra from `pyproject.toml`.
- **Server versions.** The real-server tier against the pinned Restate image
  and against `latest`.
- **Live models.**
  - When the keys are present (a nightly secret), one smoke scenario per
    framework runs on a real, cheap model: an answer, a tool call, structured
    output, and a refusal with a deliberately wrong key.
  - The assertions are structural only: a tool was called, the output
    validates, a 424 came back. They never compare text.
  - This is the only guard that the teller's response shapes match the real
    ones.
  - It is never in PR CI and never blocks a merge.

### Hygiene first

- A `tests/conftest.py` with **one session-scoped harness** for all agent
  modules, and markers:
  - `inprocess`: no Docker;
  - `server`: needs the real-server tier;
  - `e2e`;
  - `live`.
- New Justfile recipes, so a contributor without Docker can still run the
  in-process tests and the sweeps:

  | Recipe | Runs |
  | --- | --- |
  | `just test-fast` | `-m inprocess` |
  | `just test` | everything except `e2e` and `live` |
  | `just e2e` | the e2e tier |
- **Pin the Restate image** in the harness, by digest or version, and update it
  on purpose, like any other dependency.
- **State shared with the server thread:**
  - a fixture that resets every gate and counter;
  - `asyncio.Event`s that the test sets for the server's loop replaced with a
    thread-safe gate (`loop.call_soon_threadsafe`, or a `threading.Event`
    polled by the tool);
  - no monkeypatching of globals while a shared server runs. Use per-agent
    options, or a harness of the test's own.
- **Polling, not sleeping.** `until(condition)` everywhere. A "nothing more
  arrives" check waits on a sentinel event sent after the event in question,
  instead of sleeping.
- **Coverage.**
  - `coverage run --branch` on `python/restate/agents`, reported per module in
    the job summary.
  - A ratchet, so coverage can't drop, rather than a target number.
  - The untested branches listed in gap 5 are the first to close.
- **Flakiness.** Nightly `pytest -p randomly` and `--count=5` on the
  real-server tier, so flakiness is measured instead of discovered.

## The plan

The work comes in small PRs, in this order. "PR" means the job runs on every
push; "nightly" means a scheduled workflow that uploads its logs on failure.

| # | Change | Closes | Runs | Size |
| --- | --- | --- | --- | --- |
| 1 | Hygiene: `conftest.py` with one shared harness, markers, pinned Restate image, thread-safe gates, `just test-fast` | 9 | PR | Small |
| 2 | Branch coverage in CI, with a ratchet | 9 | PR | Small |
| 3 | `Teller` and the adapters for the four frameworks, plus the bank and ledger tools | 3 | PR | Medium |
| 4 | The conformance suite: today's nine contract tests, then the missing cells (Restate tools through each framework, `background`, `clear` and cursors, `PerSession` for ADK, a LangGraph tool's `TerminalError`, a stored answer not asked again for LangGraph); delete what it replaces | 3, 4 | PR | Medium |
| 5 | Tests of the untested core branches (gap 5) | 5 | PR | Small |
| 6 | `Host` with a journal across attempts | 1 (prerequisite) | PR | Medium |
| 7 | Crash sweeps over the conformance scenarios: single points in PR, pairs and seeds nightly | 1 | PR and nightly | Medium |
| 8 | `hypothesis` state machine for the conversation and `messages` | (new) | PR | Small |
| 9 | Golden wire-format fixtures, shared with `sdk-sessions` | 6 | PR | Small |
| 10 | E2E chaos: agents in killed processes, with the server restarted, `ABORT_SEED` | 2 | PR (short) | Large |
| 11 | Nightly workflow: soak, `always_replay`, newest and oldest dependency jobs, Restate `latest`, random order and repeats | 2, 7 | Nightly | Small |
| 12 | Live-model smoke tests, with keys from a secret | 7 | Nightly | Small |
| 13 | The serve-only examples run their commented flows on the teller; `restate.agents.testing` made public if the API holds, and the modal-template tutorial tested with it | 8 | PR | Medium |

**Order of value.**

- #1 and #2 take an afternoon, and they make every later failure readable.
- #3 and #4 are where most of the missing cells close.
- #6 and #7 find the bugs that nobody has thought of.
- #10 is the end-to-end proof, and the most expensive.
- #11 to #13 can come whenever convenient.

**Not planned, on purpose.**

- Real models in PR CI: they are slow, cost money, give nondeterministic text,
  and need secrets on forks.
- Tests of Restate's own guarantees: those belong to the server's project. This
  suite tests what the package builds on them.
- Token streaming: no integration offers it yet. When one does, it joins the
  conformance suite as a column of behaviours, not as a file of its own.
