# What we learned

The research behind the design, the decisions and why, the lessons from
building it, the bugs a review found in it, and the questions still open.
Written on 8 November 2026, when this work landed (restatedev/sdk-python#296).

## The research

We read three open-source coding agents, each at its latest commit (Codex,
`openai/codex`; opencode v2.2.5, `anomalyco/opencode`, plus v1.19.5 for its old
prune; pi, `earendil-works/pi`), and the four frameworks we integrate with, at
their latest releases.

### The coding agents

| | Codex | opencode v2 | pi |
| --- | --- | --- | --- |
| Storage | JSONL rollout per thread, projected into SQLite: `thread_turns`, `thread_items` | SQLite: an event log, projected into message and part rows | JSONL per session: a tree of entries (`id`, `parentId`) |
| A turn | a row: status, error, times, first user item, final answer item | none: an `idle` message ends it | none stored |
| Earlier turns, to the model | as stored, since the last compaction; tool output capped when recorded (10k tokens) | as stored, since the last compaction; tool output capped when produced (2,000 lines or 50 KiB) | as stored, from the compaction's first kept entry |
| Compaction | at 90% of the window: user messages (≤20k tokens) plus a summary, as a user message with a fixed prefix | at the window minus max(15%, 16k): a summary row, plus the last ~15k tokens as text | at the window minus 16k: an entry with the summary and the first entry kept (~20k tokens) |
| Old tool output | not cleared | v1 cleared it, opt-in; v2 removed that | not cleared; an appended `context_edit` can hide an entry from the model |
| Retention | rollouts kept; `history.max_bytes` trims only the prompt log | sessions kept; spilled tool-output files deleted after 7 days | sessions kept |
| A client sees | turns with `itemsView: full` or `summary` (first user message plus final answer) | every row, and a divider at a compaction | the compacted view; the raw entries over RPC |

What this tells us:

- **Codex's layout is ours.** Its `thread_turns` table holds status, error,
  times, and the ids of the first user item and the final answer; its
  `thread_items` table holds each item's JSON, tagged with its turn and
  ordered. Its client protocol's `itemsView: summary` is our
  `view: "summary"`. (`codex-rs/state/thread_history_migrations/0001_thread_history.sql`,
  `codex-rs/app-server-protocol/src/protocol/turn_items_view.rs`.)
- **Each of the three** keeps an append-only log; sends the model the
  history since the last compaction, unchanged, which suits the provider's
  prompt cache; records a compaction by appending a marker; and deletes only
  whole sessions, when asked.
- **Each sends its summary as a user message, with no assistant reply after
  it.**
- **None deletes the steps its model stops seeing.** opencode v1's opt-in
  prune hid old tool outputs from the model (keeping the last two user turns
  and the newest 40,000 tokens of output), and v2 removed it.

### The frameworks

| | Its unit | A turn | Compaction | Deletes stored history | Should it dictate our layout? |
| --- | --- | --- | --- | --- | --- |
| Pydantic AI | a run's messages; a `Conversation` | none: a run | capabilities: the provider's, or the harness's | no: its store "only ever grows" | no: it is "deliberately unopinionated about your database" |
| ADK | events | `invocation_id` | an appended event; raw events kept | only whole sessions | no |
| LangGraph / LangChain | checkpoints (state snapshots) | none | `SummarizationMiddleware` rewrites the state | when that rewrites it; whole threads | no |
| OpenAI Agents | Responses items | a turn number (advanced session only) | the compaction session rewrites rows | when that rewrites them | no |

Each framework's details are in [The frameworks](frameworks.md).

### What we take from it

1. **Turns are the record and items the detail,** which is Codex's choice
   too, and every framework's unit maps onto it.
2. **Frameworks dictate two things: the item format, and what a turn
   carries** for the framework to go on: OpenAI's current agent, ADK's
   session state. We store each framework's own items, and carry one slot
   per turn for each.
3. **A framework's own features come first.** Where a framework does what
   our code does, the integration uses the framework's feature, and
   `restate.agents` supplies what it needs (storage, durable waits). This is
   how Pydantic AI leans on Pydantic AI Harness (D5).

## Decisions

| | Decision | Why |
| --- | --- | --- |
| D1 | **The schema is written for new databases:** no `DROP TABLE`, no migration from the 1.4.x layout, no compatibility code. | `restate.agents` shipped in 1.4.0 to 1.4.2, but no deployment uses it yet. A key whose database 1.4.x made would fail (one of its tables has the name the new schema creates), and that was accepted. From the next release on, schema changes are appended migrations. |
| D2 | **Keep the layout:** turn rows are the record, items the detail. | The research: Codex's layout, and no framework that dictates another. |
| D3 | **Frameworks plug in through hooks** (`Items.summary`, `Turn.carry`, and what a framework asks the core for). | One mechanism per concern across integrations; a framework that needs more asks for it. |
| D4 | **Only the latest release of each framework**, with no guards for older ones. | `restate.agents` is new: it supports what is current, uses the newest APIs directly, and CI tests that release only. |
| D5 | **Pydantic AI leans on Pydantic AI Harness**, wherever a capability does what our code does. | The harness's compaction, planning, memory, search and deferred calls are better than what we would write, and need only storage and durable waits from us. |
| Q1 | **Pydantic AI's usage limits are per turn**, not per conversation. | As they were; a budget per conversation would be the harness's `Spend`, later. |
| Q3 | **An `Agent` of your own with no compaction capability gets the default compaction.** | Otherwise its history would grow without bound. |
| — | **The summary's form stays per framework.** OpenAI's is one user message; ADK and LangGraph keep a user message then "Understood.". | OpenAI's was checked against both its APIs; the others run on scripted models in the tests, which can't tell what Gemini or other chat models accept. |
| D6 | **Steering is a view of its own, `steer`; `chat` keeps one message per turn.** | A client that sends to `chat` gets the answer to its message as `chat`'s response, which joining another turn would take from it. A UI that wants one way in sends everything to `steer`, which starts a turn when none is answered. |
| D7 | **A steered message is queued as a turn of its own the moment it is sent** (the agent's `steered`), and the turn that takes it makes that turn do nothing. | Its place in the key's queue is kept with the messages sent after it, a turn that dies leaves no message behind, and the companion forwards nothing at a turn's close. The cost is one short invocation per steered message. |
| D8 | **The turn learns of steered messages from `publish`**, which it makes at each tool call's start and end, and takes them with an outbox send. | A turn that nobody steers makes no call more: the check is free. A call of its own at each step would cost one round trip per tool round of every turn. |
| D9 | **Each framework takes a steered message in place**, in the request about to go out (OpenAI's input filter, Pydantic AI's `enqueue`, ADK's session events, LangGraph's model node); none restarts its run. | Restarting a run from the stored steps (the crash path) reran input guardrails, reset `max_turns`, usage limits and recursion limits, and left the old run able to write. |
| D10 | **A message taken late is the next turn**: one that comes after the turn's last step is answered next, not folded into the turn. | The turn's answer stays the answer to what it was asked; folding it in would hide that answer from the summary view. |
| — | **Steps retention is future work** ([its design](steps-retention.md)). | Nothing reads a turn's steps once it leaves the window, but deleting them changes what the full view and conversation search can show; it can come later, as an option. |

## Lessons from building it

**The turn.**

- **Make the turn the unit everywhere:** in storage (every row belongs to
  one turn), in code (`Turn` reads and writes its own turn only), and in
  views (summary and full). Composing turns into one flat list happens in
  one place, `history()`. Before, the conversation was one table of messages
  that every turn read whole, and the model got all of it.
- **At most one turn has no answer, the latest.** `chat` is exclusive, and
  keeps the key while it waits, so turns never overlap. A turn that finds an
  earlier one without an answer knows its invocation is gone. That one fact
  makes dead turns decidable.
- **A dead turn is closed, never finished.** Its run is gone, so finishing it
  would answer a prompt nobody waits for, with a guess. It gets a result for
  each open call and the note `Ended: …`, by `settle` (as soon as the
  companion notices) or by the next message (whichever comes first), and the
  next turn loads it whole, whatever its window, since what it carried is a
  guess.
- **A turn always ends with an assistant message,** whose id is the answer's
  in both views: so the summary view and the full view agree, and the next
  model never sees a turn without an end.
- **Asking again is a step of the turn,** not a turn of its own: the summary
  shows the person's message and the answer.

**Durability.**

- **Our attempts resume from committed state; they don't replay.** A
  retried attempt runs the handler from its first line and finds what the
  database has. This rules out anything that assumes deterministic replay
  (Pydantic AI's durability layer), and it means a new run id per attempt.
- **Every write must be the turn's own, made once:** the prompt by its
  unique id (a retry finds the answer with one indexed read); Restate's
  calls by the tool call's idempotency key; a framework's writes by the tool
  call, never by something an attempt makes up (Pydantic AI Harness keys some
  writes on the run id).
- **If a retry replays writes by position, it must replay reads too.** A
  tool call that runs again reads its own earlier writes, now committed, and
  may then make different writes, which no longer line up with those it made
  (the plan store; see the bugs below).
- **A wait belongs to one tool call.** Rejoining a wait that another call
  made reuses that call's answer: an approval given for one call must never
  approve another.
- **An approval is not the call.** A turn closed after an approval returned
  must not report the call as done: it may have been denied, or not run yet.
- **A cancelled task must end cancelled.** A framework that cancels its
  agent's task and waits for it (ADK 2) hangs if the task raises something
  else. A tool cut short by `stop` raises the stop's reason otherwise, so the
  model never carries on with it.

**What the model gets.**

- **Two ways, one per agent.** The turn projection (a summary, earlier turns
  as prompt and answer within a budget, the window whole) for frameworks
  whose input we build; snapshots (the framework's own compaction, stored)
  for a framework that compacts its history itself. Snapshots keep the
  provider's prompt cache between compactions, which the projection breaks
  at each turn as it trims.
- **Rebuilding a framework's session each turn means its re-sorts and
  timestamps apply to items we made:** ADK re-sorted by time once a
  compaction event was present, and our made-up events were stamped "now".
- **A summary is a user message,** as every coding agent sends it; OpenAI's
  APIs accept it with no made-up reply after it.

**Storage.**

- **The database refuses expressions more than 100 deep.** Match many pairs
  with a list of rows (`(a, b) IN (VALUES ...)`), never an `OR` of each; tens
  of thousands of parameters are fine.
- **Keep only what is read.** Only the latest snapshot is ever read, so it
  takes the place of the earlier ones.
- **An item is the framework's string.** `Items.dump` needn't return JSON, so
  a list of items is stored as a JSON array of strings.
- **A library's tables live in a named schema**, applied wherever they are
  used, by a view too (in a throwaway session).

## Bugs a review found

A review of the finished work, on 8 November 2026, found these, each now
fixed, with a test that fails without its fix and a mutant in
`scripts/resurrect.py` ([Testing](testing.md)). They are worth knowing for a
port, since each came from a reasonable-looking shortcut:

| Bug | The shortcut | The fix |
| --- | --- | --- |
| A `DeltaChannel` graph failed every turn from about its eleventh | matching the kept channel versions with one `OR` per pair | a list of rows; reading a checkpoint of more than ~95 channels had the same bug |
| A later identical tool call could get an earlier call's approval or denial without asking | `wait_for` reused the call matching machinery of Restate's tools, which rejoins a call another attempt made with the same input | a framework's wait is matched by its tool call's id only |
| A turn stopped after a denial reported the denied call as done | an approval wait was recorded as the tool call's own Restate call | approvals are marked, and don't count as the call's outcome |
| An approved tool's own Restate call shared its approval's record | call records were matched by the tool call's id alone | matched by the id and the tool |
| After a crash, a plan's status change skipped a write, and a step stayed blocked | the plan store replayed a tool call's writes by position, but read live | the tool call's reads are journaled too |
| After a crash, a memory edit that was made was reported as failed | the harness names an operation by the run id, which changes per attempt | the operation is named by the tool call |
| Every snapshot was kept, though only the latest is read | insert only | the latest takes the place of the others |
| Without Pydantic AI Harness, the import named no extra | the import's check knew only Pydantic AI | it checks the harness too |

## What testing taught us

From bringing past bugs back with `scripts/resurrect.py` (details in
[Testing](testing.md#how-good-the-suite-is-past-bugs-brought-back)):

- **Every past bug is caught, but almost only by its own regression test.**
  The suite records the bugs it has met, and finds few new ones: the case for
  shared, framework-independent tests (a conformance suite, error tables,
  crash sweeps).
- **Error classification is where bugs live:** 12 of the first 28 were a
  refusal, a retry or a terminal error decided wrongly, and 8 more were in
  resuming.
- **"Retried forever" bugs fail slowly,** by timing out; a small retry
  budget in tests, or an assertion on the number of attempts, fails them at
  once.
- **Every fix gets a mutant.** It keeps the regression test honest: a test
  that no longer fails without its fix is found the next time the mutants
  run.

## Open questions

- **LangGraph:** the latest checkpoint of each subgraph namespace stays, one
  per turn that ran a subgraph. Delete them at the turn's end?
- **LangGraph:** a LangChain tool that returns a `Command`, under the
  built-in agent, would be stored as an item.
- **ADK:** a plugin's `before_run_callback` can still turn events compaction
  on for an invocation.
- **Pydantic AI:** capabilities at the agent's own layer run before
  `_Restate` stores new tool results, so one that edits the newest results
  could change them first. Tools that ran beside a deferred call are stored
  only at the next request, so a crash during a long wait runs them again
  (with the same call ids).
- **Pydantic AI, core:** an `answer_once(..., snapshots=...)` override for
  `prompt()` in an object that isn't an agent, and a public accessor for the
  running handler's context.
- **Approvals for the other frameworks:** OpenAI's `needs_approval` and ADK's
  tool confirmations could wait the way Pydantic AI's deferred calls do
  (`wait_for(..., approval=True)`).
