# OpenAI Agents SDK

> **Status:** done, and merged into #296 on 2026-11-08: O1 to O4. The summary is
> one user message, no guard for older SDKs is left, and a hand-built `Handoff`
> no longer crashes a run. This plan is for one agent. Read
> [`README.md`](README.md) first: the core contract, the rules for framework
> agents, and how to run the tests.

## Scope

- **Package:** `python/restate/agents/openai/` (`_agent.py`).
- **Tests:** `tests/agents_openai.py`, plus the OpenAI cases in
  `tests/examples_agents.py`.
- **Versions:** only the latest release (README, D4). Phase 1 moves the floor
  from `openai-agents>=0.6.1` (`pyproject.toml:31`) to 0.24.0. #296 was tested on
  0.22.2.

## What the OpenAI Agents SDK expects

### Storage

- **The `Session` protocol is a flat list of Responses items.** It has
  `session_id`, `get_items(limit)`, `add_items`, `pop_item` and `clear_session`
  (`agents/memory/session.py`).
- **`SQLiteSession` and `SQLAlchemySession`** have two tables:
  - `agent_sessions`;
  - `agent_messages`, with one row per item, in JSON.
- **When it saves.** The Runner saves after each of its own "turns". Its turn is
  one model call and its tools, not our turn.
- **`AdvancedSQLiteSession`** adds two tables:
  - `message_structure`, with `user_turn_number`, branch numbers and `tool_name`;
  - `turn_usage`.

  Each user message starts a turn, and branches share message rows.
- **`OpenAIConversationsSession`** keeps the items on OpenAI's servers.

### Compaction

- **`OpenAIResponsesCompactionSession` rewrites the stored history.** By default it
  starts at 10 candidate items.
  - On 0.24's SQLite and SQLAlchemy stores, it replaces the stretch of rows that
    matches the last model exchange, and keeps the older rows.
  - Elsewhere it calls `clear_session`, then `add_items`. That was its only way in
    0.22.
- **Server-side compaction** (`ModelSettings.context_management`) returns a
  `compaction` item, which is stored like any other item. Items before it may be
  left out of later requests.
- **These change only the model's input:** `session_input_callback`, and
  `ToolOutputTrimmer`, which trims older tool outputs.

### Retention

- `pop_item` and `clear_session`.
- Redis sessions take a TTL; the SQL sessions have none.

## How `restate.agents` maps onto it

| OpenAI Agents | `restate.agents` |
| --- | --- |
| a session's items | a turn's items, plus the earlier turns as prompt and answer |
| `Session` | not used: `history()` is the run's input (`openai/_agent.py:501-507`) |
| the current agent, after handoffs | carried on the turn row: written by `turn.carry(agent=...)` (`_agent.py:290,500`), read back at `_agent.py:239` |
| server-side compaction | the built-in agent gets it where the SDK has it (`_agent.py:575-584`). Its item is stored, and the input starts from the latest one (`_since_compaction`, `_agent.py:531-534`). |
| the compaction session | not used: it deletes stored items |
| `user_turn_number` | our turn |

## Work items

### O1. The summary's form

- **Consider implementing `summary(text)`** to return a single user message,
  without the made-up "Understood." reply.
  - Codex sends its summary as a user message that starts with a fixed prefix
    (`codex-rs/core/src/compact.rs`, `SUMMARY_PREFIX`).
  - The Responses API accepts consecutive user items.
- **Decide with tests:**
  - the stub model accepts the request;
  - the items are valid input items for the Responses API, and for Chat
    Completions through the SDK's converter.
- **Report your choice.** If you keep the default, say why.

### O2. Notes for users

Add these to the module docstring:

- Don't use `OpenAIResponsesCompactionSession` with our agents. It rewrites the
  stored items, which only our turns write.
- To clear old tool outputs within the window, add the SDK's `ToolOutputTrimmer`
  as an input filter. It runs after ours (`_agent.py:258-268`).

### O3. Guards for older SDKs (D4)

- Remove the check for `context_management` (`_agent.py:575-584`). On 0.24 the
  built-in agent always gets server-side compaction.
- Remove any other guard that exists only for SDKs older than the floor.

### O4. Docs

- **The module docstring:** O1's choice and O2's notes.
- **Notes for TURN-SEMANTICS.md** go in your report.

## Acceptance

- O1's choice is made, and tested.
- O3 is done: no guard for older SDKs is left.
- Nothing outside this file's scope changed.

## Later, with steps retention

These checks belong to the README's future work, not to this plan:

- **Handoffs.** At window 0, the handoff tests pass with steps deleted, including
  the failed, killed, crashed and tool-ended turns. The current agent is carried
  on the turn row.
- **Server-side compaction.** When a turn holding a compaction item has its steps
  deleted, the next request is built from the transcript, and
  `_since_compaction` never cuts at a stale compaction item.
- **The same requests and the full view.** The model's requests don't change, and
  the full view shows deleted turns as prompt and answer.

## Out of scope

- Using the SDK's `Session` stores for storage.
- Usage per turn, as `AdvancedSQLiteSession` keeps it.
- Branches.
