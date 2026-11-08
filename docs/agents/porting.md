# Porting to sdk-sessions

A guide for bringing this SDK's conversation model to the TypeScript agents of
[sdk-sessions](https://github.com/restatedev/sdk-sessions)
(`packages/agents`). It says where the two SDKs stand, what to port in which
order, what must hold afterwards, and which tests define it. It was written
against sdk-sessions at `6d5e9a2` (after v0.3.0).

## Where the two SDKs stand

Both SDKs share the turn's outer life: a key answers one message at a time
(`chat` is exclusive), a turn starts and closes in the companion (`signals`,
keyed `<agent>:<key>`, with the same handler names), `stop` cancels the
invocation, `status`, `pending`, `messages`, `sync` and `watch` mean the same,
and a tool's Restate calls are made once (`calls.ts` and `_calls.py` are the
same design). They differ in who owns the conversation:

| | sdk-sessions (TypeScript) today | This SDK (Python) |
| --- | --- | --- |
| The conversation | each framework's own tables: Claude's `restate_claude_messages` and `restate_claude_answers`, DeepSeek's sessions and events, opencode's and pi's stores, a LangGraph checkpointer | one store for every framework: turn rows and items ([The conversation](conversation.md)) |
| A turn in storage | an answer per prompt id, in the framework's tables | a row per turn, with a status, an answer, and what it carries |
| What clients read | `restate_agent_log` (`log.ts`), kept in step with the framework's transcript at each publish (`transcribing`) | the turn rows and items themselves: the full and summary views |
| What the model gets | what the framework keeps | `history()`: the turn projection, or history from snapshots |
| A dead turn | closed in the companion by `awaitTurn`; the conversation is left as the framework left it | closed in the conversation too, by `settle` or the next message: a result for each open call, `Ended: …` |
| Long conversations | the framework's own compaction, if any | summaries and `compact` (window, budget), or the framework's compaction, stored as snapshots |
| A framework's own approvals | — | `wait_for(..., approval=True)` (Pydantic AI's deferred calls) |

The map of the code:

| Concern | Python | TypeScript today |
| --- | --- | --- |
| The turn's wrapper: start, close, cancellation, attempt end | `_turn.py` (`chat_turn`, `answering`) | `common.ts` (`chatHandler`) |
| The companion | `_signals.py` | `signals.ts` |
| The views and private handlers | `_object.py` (`settle`, `compact`, `erase`) | `object.ts` |
| The conversation | `_conversation.py` | — (each framework's store) |
| What clients read | `_messages.py` (from the turns) | `log.ts`, `Transcript` in `common.ts` |
| The call records | `_calls.py` | `calls.ts` |
| Tools | `_tools.py` | `tools.ts` |
| Per-attempt state | `ScopeKey` (`restate.sessions.internal`) | `ScopeKey` (`@restatedev/sessions/internal`) |
| Schemas | `Schema` (`restate.sessions.internal`) | `Schema` (`@restatedev/sessions/internal`) |
| The SQL engine | sdk-shared-core | sdk-shared-core: the same limits apply |

## What to port, in order

Each step stands on its own, with its tests, and the earlier ones are small.

1. **Fixes that apply as they are.** They don't depend on the rest:
   - **Call records** (`calls.ts`, as `_calls.py`): match a record by the
     tool call's id *and* the tool; a framework's own wait is matched by its
     id only, never rejoined across attempts by its input; mark approvals,
     and leave them out of `outcomes`. See
     [Tool calls, once](conversation.md#tool-calls-once-the-call-records).
     `callOf` in `calls.ts` matches by id alone today, and rejoins by input.
   - **The LangGraph checkpointer** (`langgraph/checkpointer.ts`): match
     channel versions as a list of rows, `(channel, version) IN (VALUES (?,
     ?), ...)`, not one `OR` per pair, which the database refuses past 100
     terms (`checkpointer.ts` builds such `OR`s); and, if the TypeScript
     LangGraph has `DeltaChannel`, keep what it is rebuilt from when pruning
     (`_kept` in `_checkpointer.py`).
2. **The companion's `settle`.** A turn's start says whether its agent
   settles (`Started.settles`); when `awaitTurn` finds the invocation gone,
   the companion closes the turn and sends such an agent `settle(turn)`, a
   private handler that closes the turn in the conversation
   ([Dead turns](turns.md#dead-turns)). The handler names stay shared, and an
   agent that doesn't say it settles is sent nothing, so the two SDKs' agents
   can share a deployment's companion while one has it and the other
   doesn't. Port it with the conversation (step 3), since `settle` closes the
   turn there.
3. **The conversation store.** The tables, `Conversation`, `Turn` and
   `answerOnce`, as [The conversation](conversation.md) and
   [The turn model](turns.md#the-turn-model) describe them, with an `Items`
   per framework. Keep the table and schema names (`restate_agents.turns`),
   so that the data layout, and any tool that reads it, is the same in both
   SDKs. Then:
   - `messages` and `sync` read the turns (full and summary views), with the
     wire format they have today (ids, `seq` cursors, `first` after a
     `clear`): clients don't change. `log.ts` and `transcribing` go, as the
     conversation is the log.
   - `settle` (call records) checks the turn's stored results rather than a
     transcript (`Turn.stored`).
   - A dead turn is closed before the next prompt (`answerOnce`), and by
     `settle`.
   - **Decide first** what happens to conversations stored by released
     versions (v0.3.0 and before): migrate them into turns, or, as this SDK
     did (D1 in [What we learned](learnings.md#decisions)), start new
     databases only.
4. **Each framework onto the store.** For each, pick one of the two ways to
   give the model its history, with the criteria of
   [What the model gets](conversation.md#what-the-model-sees):
   - **The turn projection** where the integration builds the model's input
     each turn: a framework whose loop takes the messages it is given (as
     Claude's tool runner takes its params today). Its items are its
     messages; earlier turns reach the model as prompt and answer, the
     window whole.
   - **History from snapshots** where the framework keeps and compacts its
     own history: it stores what its compaction leaves (`Turn.snapshot`),
     and gets the latest snapshot and every row after it.

   Research each framework first, as [The frameworks](frameworks.md) did for
   ours: how it stores a conversation, compacts it and deletes it, what a
   turn must carry for it to go on, and what it keys on things that change
   per attempt (run ids, timestamps).
5. **Summaries and `compact`**, for the turn projection: `window` and
   `budget` options, `Items.summary` in the framework's own form, `compact`
   sent after a turn's answer when the earlier turns outgrow the budget
   ([Summaries](conversation.md#summaries)).
6. **Asking again within a turn** (`check` on `answerOnce`, `Turn.askAgain`),
   if the TypeScript agents have `output=` or guardrails that ask again.
7. **A framework's own approvals,** with `waitFor(..., { approval: true })`
   for any framework whose tool calls can wait for a person.

## What must hold afterwards

The guarantees of [Turn semantics](turns.md#guarantees-and-limits), and in
particular:

- [ ] Every row belongs to one turn; only the latest turn, while it has no
      answer, is written, by its own run; closing a dead turn writes only
      its closing record.
- [ ] At most one turn has no answer, and a new turn is refused while
      another has none.
- [ ] A turn ends once, with a status, and always with an assistant message
      (its last reply, the note that closed it, or its answer stored as its
      reply), whose position is `answered_at`.
- [ ] A retried attempt of an answered turn reads only its answer, by the
      prompt's id; a retried attempt of an unfinished turn carries on from
      its stored steps, without asking the model again for a stored reply.
- [ ] A turn reads a bounded part of the conversation: the transcript the
      budget holds, and the window (or the latest snapshot and what follows).
- [ ] The window holds every turn its framework didn't answer (the one being
      answered, and any that another invocation ended), then the latest
      `window` answered turns.
- [ ] A dead turn is closed, never finished, by `settle` or the next
      message, with a result for each open call: the call's error, "done,
      but not recorded" if its Restate call returned, or the note.
- [ ] Message ids are positions; `seq` only grows, and a client reading from
      a cursor gets each new or changed message once; `clear` keeps where
      ids and cursors got to.
- [ ] Each Restate call of a tool is made once per tool call; a framework's
      wait is its tool call's alone; an approval is never the call's outcome.

## The tests that define it

Port these as the specification; each was checked to fail without the code it
tests. Test names are this SDK's.

| Behaviour | Where |
| --- | --- |
| The turn model: scope, ownership, the window rule by status, asking again, a turn's end, bounded loading, summaries, dead turns and `settle`, the summary view | `tests/agents_conversation.py`, `tests/agents_messages.py`, `tests/agents_object.py` |
| Snapshots, `asked_at`, `wait_for` (a wait per tool call, approvals not outcomes, an approval and its tool's call apart) | `tests/agents_snapshots.py` |
| Call records: made once, rejoined under a new id, kept until stored, forgotten after | `tests/agents_tools.py` (`test_a_returned_call_is_kept_until_its_result_is_stored_and_forgotten_once_it_is`) |
| The companion: start, close, `awaitTurn`, the `settle` gate, waits and signals | `tests/agents_signals.py`, `tests/agents_object.py` |
| Per framework: answer and the full view after it, a crash in a tool, a stored answer not asked again, refused, stopped, `output=`, several prompts | each framework's `tests/agents_<framework>*.py` |
| The LangGraph checkpointer: resume, history, prune, `DeltaChannel`, many channels | `tests/agents_langgraph_checkpointer.py` |

And the mutants of `scripts/resurrect.py` ([Testing](testing.md)): each
brings back one bug that a fix describes, or breaks one rule of the turn. A
port can take the list as a checklist: each mutant names the bug, and the
port should have a test that fails for it.

## Python and TypeScript, side by side

- **Per-attempt state.** Both SDKs have `ScopeKey`; Python's tools find
  their run through a `contextvars.ContextVar` (`tools_in`), where
  TypeScript would use `AsyncLocalStorage` or pass the context.
- **Cancellation.** Python cancels asyncio tasks, and a tool whose own task
  is cancelled as the turn stops must end cancelled (`Tool.run`); TypeScript
  aborts an `AbortSignal` (`stopSignalOf`), and the same rule holds for any
  framework that waits for the work it aborted.
- **Errors.** A `TerminalError` from a tool is its answer to the model; any
  other error fails the attempt; only a model request's error can be a
  refusal (424). The classification is the same in both SDKs, and is where
  most past bugs were ([What we learned](learnings.md#what-testing-taught-us)).
- **The database.** The same engine (sdk-shared-core), so the same limits:
  no expression more than 100 deep; and the same `Schema` mechanism, one
  named schema per library. And the same costs: a `SELECT` of several
  aggregates scans its table, which a cold process reads whole; see
  [Access patterns](access-patterns.md) for what each statement costs.
