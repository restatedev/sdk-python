# LangGraph and LangChain

> **Status:** not started. This plan is for one agent. Read
> [`README.md`](README.md) first: the core contract, the rules for framework
> agents, and how to run the tests.

## Scope

- **Package:** `python/restate/agents/langgraph/`: `_agent.py`, `_graph.py`,
  `_checkpointer.py`, `_context.py`, `_errors.py` and `nodes.py`.
- **Tests:**
  - `tests/agents_langgraph.py`
  - `tests/agents_langgraph_graph.py`
  - `tests/agents_langgraph_checkpointer.py`
  - `tests/agents_langgraph_create_agent.py`
  - `tests/agents_langgraph_nodes.py`
- **Versions:**
  - The extra requires `langgraph>=1.1.5,<2` and `langchain-core>=1.0.0,<2`
    (`pyproject.toml:33-35`). CI tests the oldest versions they allow
    (`.github/workflows/test.yml:36-68`).
  - The venv has langgraph 1.1.9 and langchain 1.2.15 installed.
  - The latest are langgraph 1.2.20, langchain 1.4.9, langgraph-checkpoint 4.3.1,
    langgraph-checkpoint-sqlite 3.1.3 and langgraph-checkpoint-postgres 3.2.0.

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

### L1. One window for the graph agent

- **Today:**
  - `whole = max(window, 1)` is passed to `answer_once` per call
    (`langgraph/_agent.py:479-484`).
  - `compact` reloads with the agent's own window (`_conversation.py:1213-1216`).
- **Retention is already right.** With phase 1, it computes `window_from` from the
  window `answer_once` was given.
- **Check `compact`.** Find out whether `_graph_agent` gives the object a
  `summarize`.
  - If it doesn't, nothing reads the unwidened window, so there is nothing to do;
    say so in your report.
  - If it does, the widened window has to be the agent's own, so that every reader
    agrees. If that needs a change to `AgentObject` or `chat`, put it in your
    report as a core request.
- **Test:** a graph at window 0 and retention 0, over three turns, where turn 2 is
  killed or fails. Turn 3's graph still gets what closed turn 2. That means turn
  2's steps were not deleted before turn 3's run, because `window_from` counted
  the widened window.

### L2. The graph's own state (decision O5)

- **Retention can't reach it.** The latest checkpoint holds every message the
  graph's state keeps.
- **Recommended now: document that trimming it is the graph's job.**
  - Use LangChain's `SummarizationMiddleware`, which rewrites the state, or a node
    that returns `RemoveMessage`s.
  - Our `aprune(keep_latest)` then deletes the values that no remaining checkpoint
    references (`_checkpointer.py:438-455`).
  - Add a short example to the module docstring of `_graph.py`.
- **Test** that a trimmed message is really gone: a graph that removes old messages
  with `RemoveMessage`. After the turn's prune, the bytes of the removed messages
  are not in `restate_langgraph_channel_values`.
- **Later, if O5 asks for it:** an opt-in that removes the messages of pruned
  turns from the state after each turn. It would work only on a `messages` channel
  that uses `add_messages`, by id, through `aupdate_state`. It changes what the
  graph's model sees, so it has to be the graph author's choice.

### L3. Retention for the built-in agent

- **The same requests.** Every model request is the same at retention 0 as with a
  retention long enough that nothing is deleted.
- **The full view.** Pruned turns show as prompt and answer, with LangChain's
  messages.
- **The summary's form.** Keep the default.

### L4. The graph agent under retention (verification)

- **Deleting our steps leaves the graph's tables alone.** It touches none of the
  `restate_langgraph_*` tables.
- **Copying still works.** After a crash, a resume copies messages by id. It must
  still copy only what's new once earlier turns' steps are gone.
  - By default, a run copies the messages after its own input, which is
    unaffected.
  - A graph that trims its own input away gets copied by id, except for ids that
    belong to the loaded turns.
  - Turns outside the window were never loaded, so this is the limit #296
    documented already ("Guarantees and limits"), not a new one.
  - Test that a graph that keeps every message gets nothing copied twice over
    five turns at window 1 and retention 0.

### L5. Docs

- **The module docstrings of `_agent.py` and `_graph.py`:**
  - the graph's state is the graph's own, with how to trim it (L2);
  - what retention does to our copy.
- **Notes for TURN-SEMANTICS.md** go in your report.

## Acceptance

- L1's test and L4's no-duplicates test pass on the oldest LangGraph allowed and
  on the latest.
- L2's "trimmed means gone" test passes.
- L3's tests pass.
- Nothing outside this file's scope changed.

## Out of scope

- The opt-in that trims the graph's state (L2, later).
- Checkpoint TTLs.
- `DeltaChannel`: check only that nothing breaks if a graph uses it.
