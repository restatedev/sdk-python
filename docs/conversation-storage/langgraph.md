# LangGraph and LangChain

> **Status:** done, and merged into #296 on 2026-11-08: L0 to L5; L1 needed
> nothing. Beyond the plan: the prune keeps what a `DeltaChannel` is rebuilt
> from.

## Scope

- **Package:** `python/restate/agents/langgraph/`: `_agent.py`, `_graph.py`,
  `_checkpointer.py`, `_context.py`, `_errors.py` and `nodes.py`.
- **Tests:**
  - `tests/agents_langgraph.py`
  - `tests/agents_langgraph_graph.py`
  - `tests/agents_langgraph_checkpointer.py`
  - `tests/agents_langgraph_create_agent.py`
  - `tests/agents_langgraph_nodes.py`
- **Versions:** only the latest releases (README, D4).
  - Phase 1 moves the floors from `langgraph>=1.1.5` and `langchain-core>=1.0.0`
    (`pyproject.toml:33-35`) to langgraph 1.2.20 and langchain 1.4.9.
  - The research also read langgraph-checkpoint 4.3.1,
    langgraph-checkpoint-sqlite 3.1.3 and langgraph-checkpoint-postgres 3.2.0.
  - #296 was tested on langgraph 1.1.9 and langchain 1.2.15.

## What LangGraph expects

### Storage

- **Checkpointers save snapshots of the state, not a transcript.**
  - `SqliteSaver` has two tables, `checkpoints` and `writes`. Each checkpoint row
    holds the whole state, the full message list included.
  - `PostgresSaver` adds `checkpoint_blobs`, one per channel that changed. Each
    blob still holds the channel's whole value.
- **One checkpoint per super-step, plus one for the input** (`pregel/_loop.py`).
- **Durability modes:**
  - `sync` and `async`;
  - `exit`, which saves once, when the graph exits.
- **`DeltaChannel`**, a beta that is newer than 1.1.9, stores writes plus snapshots
  taken from time to time.
- **There is no turn.**
  - A thread is a chain of checkpoints.
  - Messages have ids. `RemoveMessage(id)` removes one, and
    `RemoveMessage(REMOVE_ALL_MESSAGES)` removes them all
    (`langgraph/graph/message.py`).
- **What an integrator implements:**
  - required: `get_tuple`, `list`, `put` and `put_writes`, plus their async
    versions;
  - optional: `delete_thread`, `delete_for_runs`, `copy_thread` and `prune`
    (`langgraph/checkpoint/base/__init__.py`).

### Compaction: LangChain 1.x middleware

- **`SummarizationMiddleware` rewrites the state.** It writes
  `RemoveMessage(REMOVE_ALL_MESSAGES)`, then a summary message, then the messages
  it keeps (20 by default). The checkpoints before it still hold the old messages
  until they are pruned.
- **These change only the model's input, not the state:**
  - `ContextEditingMiddleware` with `ClearToolUsesEdit`, which by default starts
    at 100k tokens and keeps the last 3 tool results;
  - `trim_messages`, which returns a new list;
  - a prebuilt `pre_model_hook` that returns `llm_input_messages`. One that
    returns `messages` changes the state instead.

### Retention

- **`delete_thread`** exists on both savers.
- **`prune`**, keeping the latest checkpoint or deleting, exists only on Postgres,
  and refuses threads that use `DeltaChannel`.
- **A checkpoint TTL** exists only on the hosted Agent Server, set in
  `langgraph.json`.
- **`ShallowPostgresSaver`** is deprecated in favour of `durability="exit"`.

## How `restate.agents` maps onto it

There are two agents:

| | The built-in agent: `langgraph.agent(model=...)` | Your graph: `langgraph.agent(graph=...)` |
| --- | --- | --- |
| The model's input | `history()` (`langgraph/_agent.py:363`) | the graph's own state |
| Checkpoints | none | `SqlCheckpointSaver`, in the key's database: `restate_langgraph_checkpoints`, `restate_langgraph_channel_values` and `restate_langgraph_writes`. Pruned to the latest after each turn (`aprune`, `_graph.py:360,396`). |
| Our items | the run's messages | a copy of the messages after the run's input, matched by id (`_graph.py:33-46`) |
| The window | the agent's | at least 1, set per call (`langgraph/_agent.py:479-484`): the graph reads what closed the previous turn |
| Summaries | ours (`summarize=_summarizer(model)`, `langgraph/_agent.py:614`) | they don't reach the graph's model, which reads its own state. Check whether `_graph_agent` sets `summarize` at all. |

## Work items

### L0. What the new releases broke (first)

Phase 1 moved the floor to the latest release, and 3 or 4 of this framework's
tests fail there, before any other change. The README's "Phase 1: what was
built" lists them, with the type errors. Fix them first, in this package (or
in its tests, where a test relied on how an older release behaved), and
report each cause. A cause in the core is a core request (rule 3).

### L1. One window for the graph agent

- **Today:**
  - `whole = max(window, 1)` is passed to `answer_once` per call
    (`langgraph/_agent.py:479-484`);
  - `compact` reloads with the agent's own window (`_conversation.py:1213-1216`).
- **Check `compact`.** Find out whether `_graph_agent` gives the object a
  `summarize`.
  - If it doesn't, nothing reads the unwidened window, so there is nothing to do.
    Say so in your report.
  - If it does, `compact` could summarize the previous turn while the graph also
    loads it whole. The widened window then has to be the agent's own, so that
    every reader agrees. If that needs a change to `AgentObject` or `chat`, put it
    in your report as a core request.
- **Test**, only if the graph agent summarizes: at window 0, the previous turn is
  never both summarized and loaded whole.

### L2. The graph's own state keeps growing (docs)

- **Why:**
  - The latest checkpoint holds every message the graph's state keeps.
  - Our checkpointer deletes the older checkpoints after each turn
    (`aprune(keep_latest)`, `_graph.py:360,396`).
  - So what grows is the state itself, and it grows unless the graph trims it.
- **Document that trimming it is the graph's job:**
  - LangChain's `SummarizationMiddleware`, which rewrites the state;
  - or a node that returns `RemoveMessage`s.

  Our `aprune(keep_latest)` then deletes the values that no remaining checkpoint
  references (`_checkpointer.py:438-455`). Add a short example to the module
  docstring of `_graph.py`.
- **Test that a trimmed message is gone:** a graph removes old messages with
  `RemoveMessage`. After the turn's prune, the bytes of the removed messages are
  not in `restate_langgraph_channel_values`.

### L3. The built-in agent's summary form

Keep the default: a user message, then "Understood." If the OpenAI agent switches
to a single user message (`openai.md`, O1), check whether that works for the chat
models we test, and report whether LangChain should follow.

### L4. Guards for older versions (D4)

- Remove any code in the package that exists only for LangGraph or LangChain
  versions older than the floor.
- CI's job for the oldest LangGraph skips the `create_agent` tests, because
  `create_agent` needs a newer langchain-core (`.github/workflows/test.yml:59`).
  With the floor at the latest release that skip no longer applies; check that
  those tests run.
- List each removal in your report.

### L5. Docs

- **The module docstrings of `_agent.py` and `_graph.py`:** the graph's state is
  the graph's own, and how to trim it (L2).
- **Notes for TURN-SEMANTICS.md** go in your report.

## Acceptance

- L1 is settled: either there is nothing to do, or it is fixed and tested.
- L2's "trimmed means gone" test passes.
- L4 is done.
- Nothing outside this file's scope changed.

## Later, with steps retention

These checks belong to the README's future work, not to this plan:

- **The widened window is honoured.** Retention computes `window_from` from the
  window given to `answer_once`. At window 0, when turn 2 is killed, turn 3's
  graph still gets what closed turn 2.
- **The built-in agent.** The model's requests don't change, and the full view
  shows deleted turns as prompt and answer.
- **The graph agent:**
  - deleting our steps leaves the `restate_langgraph_*` tables alone;
  - copying by id copies nothing twice over five turns.
- **An opt-in to trim the graph's state.** It would remove deleted turns'
  messages from a `messages` channel that uses `add_messages`, through
  `aupdate_state`. Whether to add it is a decision for then.

## Out of scope

- Checkpoint TTLs.
- `DeltaChannel`: check only that nothing breaks if a graph uses it.
