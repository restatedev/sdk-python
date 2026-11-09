# Turn semantics

How an agent of `restate.agents` answers its messages: what a turn is, how it
starts and ends, what happens when it is retried, stopped or killed, and what
`status`, `messages` and a follower see along the way. What a turn stores and
what the model gets are in [The conversation](conversation.md); how each
framework plugs in, in [The frameworks](frameworks.md).

The code lives in:

- [`_turn.py`](../../python/restate/agents/_turn.py): `chat_turn`, the turn;
- [`_conversation.py`](../../python/restate/agents/_conversation.py): the
  conversation, and `answer_once`, which answers a prompt;
- [`_signals.py`](../../python/restate/agents/_signals.py): the companion;
- [`_steering.py`](../../python/restate/agents/_steering.py): the messages
  steered to a turn, which it takes at its steps;
- [`_object.py`](../../python/restate/agents/_object.py): the views (`status`,
  `stop`, `steer`, `clear`, `sync`, `watch`) and the private handlers
  (`settle`, `compact`, `erase`, `steered`);
- [`_messages.py`](../../python/restate/agents/_messages.py): the conversation as
  clients read it;
- [`_calls.py`](../../python/restate/agents/_calls.py): the Restate calls of a
  turn's tools.

The TypeScript SDK's agents follow the same turn and companion
(`sdk-sessions/packages/agents/TURN-SEMANTICS.md`). How the conversation is
stored and loaded, and the `settle` and `compact` handlers, are this SDK's
(see [Agents of the TypeScript SDK](#agents-of-the-typescript-sdk), and
[Porting to sdk-sessions](porting.md)).

## Words

- **Agent, key.** A session object of `restate.agents`, such as `assistant`,
  and one of its keys, such as `alice`: one conversation, in its own SQL
  database.
- **Message.** One call of the agent's `chat` handler: one Restate
  invocation, whose id (`inv_1f…`) is also the message's id.
- **Turn.** One prompt, and what answers it: the unit of the conversation
  ([The turn model](#the-turn-model)). Its id is the prompt's: the
  invocation's id, or `<id>:2`, `<id>:3` for later prompts of a handler that
  prompts more than once. A `chat` message is one turn.
- **Invocation.** The `chat` invocation answering a message, from its start
  to its end, across its attempts: what the companion follows (`status`
  calls it the turn being answered, `answering`) and what `stop` cancels.
  An invocation of a handler of your own can hold several turns.
- **Attempt.** One run of a turn's invocation. A crash, a lost connection or
  a suspension ends an attempt; Restate starts another, which runs the
  handler again from its first line and resumes from what the database has.
- **Companion.** The agent's `signals` object, keyed `<agent>:<key>` (such
  as `assistant:alice`). It is a journaled virtual object, so it can wait
  (awakeables, timers) and be written while the turn runs. It keeps the
  running turn, the waits of its tools, and the conversation's revision.
  Nothing in it is needed to complete a turn: it is the control plane.
- **Revision.** A number the companion increases at every change a client
  could see (`publish`). `watch` and `sync` wait for a newer one.
- **Transcript.** Per turn: the prompt and the turn's answer.
- **Trajectory.** Per turn: everything else it stored, the model's replies,
  the tool calls and their results, compaction items.
- **Carried state.** What a turn hands to the turns after it that is no
  message: the agent the conversation is with (after a handoff), and the
  framework's own state (ADK's session state).
- **Window.** The turns the model sees whole, with their trajectories
  ([Which turns a turn loads](conversation.md#which-turns-a-turn-loads)).
- **Step.** The point in a turn after the model's reply has a result for
  each of its tool calls, before the model's next request.
- **Steered message.** A message sent to the agent's `steer` view, which the
  turn being answered takes at its next step ([Steering a turn](#steering-a-turn)).

## One message at a time

A key answers one message at a time, in the order they came. `chat` is an
exclusive handler, and it keeps the key while it waits (suspended on a
signal, say): a message sent while the agent answers another waits in
Restate's queue for the key, and its turn starts when the one before it
ended. A message sent to `chat` never joins the turn being answered; one sent
to `steer` does, at the turn's next step ([Steering a turn](#steering-a-turn)).

So **turns never overlap.** At most one turn of a conversation has no
answer, and it is the latest. A turn that finds an earlier one without an
answer knows that turn's invocation is gone (killed, say): a live one would
still hold the key.

- **Answer now, or later.** `chat` answers with the answer. `chat/send`
  answers with the invocation's id at once, and
  `/restate/invocation/<id>/attach` waits for the answer, as often as you
  like.
- **Once.** A message sent with an idempotency key is one invocation,
  answered once, however often it is sent. A webhook's delivery (`deliver`)
  is a message of this kind, keyed by the delivery.
- **To change course,** send the turn a message with `steer`, which it
  takes at its next step; or stop the turn (`stop`) and send the message
  that replaces it: it is answered once the stopped turn ended, after any
  messages already waiting.

## A turn's life

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant A as Agent (chat)
  participant DB as Key's database
  participant S as Companion (signals)
  C->>A: chat("Plan my week")
  A->>S: start(turn, settles)
  S-)S: awaitTurn(turn)
  Note over S: status.answering = turn
  A->>S: publish
  A->>DB: the prompt's answer? (none yet)
  A->>DB: load the transcript and the window
  A->>DB: ask: the turn's row
  loop each step of the framework
    A->>DB: append the model's reply, the tool calls' results
    A->>S: publish (a tool call starts, ends)
  end
  A->>DB: answered: the answer, and what the turn carries
  opt the earlier turns outgrow the budget
    A-)A: compact (runs before the next message)
  end
  A->>S: close(turn)
  Note over S: status.answering = null, publish once
  S-->>A: closed
  A-->>C: the answer
  Note over S: awaitTurn: the turn ended as it should, nothing to do
```

1. **Start.** The turn calls the companion's `start(turn)`: from here,
   `status` says the agent answers it (`answering: {invocationId, since}`).
   It also says the agent has `settle` (see [Dead turns](#dead-turns)). A
   retried attempt calls `start` again with the same invocation, which
   changes nothing (it keeps when the turn started). `start` sends
   `awaitTurn(turn)`, the turn's watch.
2. **The prompt.** `answer_once` reads the prompt's answer by its id, one
   indexed read: a retried attempt of an answered turn returns it, and reads
   nothing more; one of a turn that was closed (cancelled, failed, refused, or
   ended by another invocation) fails as the prompt did, the note never
   taken for an answer. Otherwise it loads the conversation
   ([Which turns a turn loads](conversation.md#which-turns-a-turn-loads)), closes a turn
   left without an answer, and stores the prompt as the turn's row.
3. **Steps.** The framework answers: model calls and tool calls. Each step
   is stored as it comes (a reply in one transaction, each result as it
   comes back), and each tool call publishes as it starts and as it ends,
   so a follower sees the call start and its result.
4. **Answer.** The answer is stored on the turn's row, with what the turn
   carries. If the turns before the next turn's window outgrow the agent's
   budget, the agent sends itself `compact`
   ([Summaries](conversation.md#summaries)).
5. **End.** The turn calls the companion's `close(turn)` and waits for it.
   `close` clears the turn and publishes: one revision for the turn's end,
   after its last write. Then the handler returns the answer, or raises the
   turn's error.

A **background** agent (`background=True`, a subagent say) publishes its
turn's end only: no step, nor its start, wakes a follower.

## How a turn ends

| How | The handler | In the conversation, the turn's answer | Who closes the turn in the companion | Then |
| --- | --- | --- | --- | --- |
| **Answered** | returns the answer | the answer | the turn | idle; the next message starts |
| **Refused** (a wrong API key, an unknown model) | fails with HTTP 424, for good | `Unanswered: …` | the turn | idle; the next message is answered on its own |
| **Failed for good** (a terminal error) | fails with it | `Error: <message>` | the turn | idle |
| **Stopped** (`stop`, `clear`, or Restate's cancel) | fails with the cancellation (409) | `Cancelled: …` | the turn | idle; its tools' calls are cancelled, their waits forgotten |
| **Crashed** (a process died, a retryable error) | none yet: Restate runs it again | none yet: the turn goes on | nobody: the turn is not over | still answering; the next attempt goes on with it |
| **Killed** (Restate's admin API) | runs no code | `Ended: …`, by `settle` or the next message | its watch (`awaitTurn`), at once | idle; its tools' waits dropped |
| **Out of retries** (100 000 attempts, about 11 days) | runs no code | `Ended: …`, as killed | its watch, if Restate fails it | idle; a turn Restate pauses instead stays answered until an operator acts |

A turn that closes itself (answered, refused, failed, stopped) closes its
run in the conversation first: each tool call without a result gets one (the
error its Restate call returned, a note that it did happen if it returned,
or else the note that ends the turn), then the note is the turn's last
reply, and its answer. So the next turn's model never sees a tool call
without a result, and knows how the turn ended.

Whoever closes a turn in the companion, a close does the same: it clears the
turn (if the agent still answers it), drops the waits of the turn's tools
(rejecting their awakeables, so a `wait` still parked ends), and publishes
once if either changed something. A second close of the same turn changes
nothing, and publishes nothing.

## Dead turns

A turn closes itself only if its code runs to the end. A killed turn runs
none, so its turn has no answer, and must not be finished: it is over. Two
things close it, whichever comes first.

### The companion notices: `settle`

The companion watches each turn it starts: `awaitTurn(turn)`, a shared
handler, waits on the turn's invocation (`attach`), holding no process. If
the invocation failed without closing its turn, the companion closes it,
and sends the agent `settle(turn)`, a private handler that closes the turn
in the conversation.

```mermaid
sequenceDiagram
  autonumber
  participant O as Operator
  participant A as Agent (chat)
  participant DB as Key's database
  participant S as Companion (signals)
  participant T as Agent (settle)
  A->>S: start(turn, settles)
  S-)S: awaitTurn(turn)
  A->>DB: the prompt, a reply with a tool call
  O->>A: kill (admin API)
  Note over A: no code runs, the turn has no answer
  Note over S: awaitTurn: attach fails (killed)
  S-)S: ended(turn)
  Note over S: status.answering = null, the waits dropped, publish
  S-)T: settle(turn)
  T->>DB: the latest turn: this one, without an answer?
  T->>DB: a result for each open call, the note, answer = Ended
  T->>S: publish
  Note over S: a follower reads the turn ended, before any next message
```

- **The calls' outcomes.** A tool call whose Restate call returned before
  the kill gets "done, but its result was not recorded" rather than the
  note: the model would take it for undone, and do it again. `settle` reads
  the outcomes the dead invocation stored, while they are still there (the
  next invocation of the key replaces them).
- **Idempotent.** `settle` closes the latest turn only if it is the dead
  invocation's and has no answer. A turn the next message closed first is
  left as it is.
- **An hour at most.** The watch races `attach` against an hour's sleep. A
  turn still answered then gets a new watch, and this one ends: no watch
  waits for weeks (a turn waiting for an approval, say) on the deployment it
  started on.
- **A cancelled watch** (an operator's) closes nothing: the turn may still
  run.

### The next message comes first

A message queued behind the killed turn may start before the watch closed
it. Its `start` closes the turn before it in the companion (and sends
`settle`, which waits behind it for the key), and its `answer_once` finds
the turn without an answer, and closes it the same way, before its own
prompt.

```mermaid
sequenceDiagram
  autonumber
  participant N as Next turn (chat)
  participant DB as Key's database
  participant S as Companion (signals)
  participant T as Agent (settle)
  N->>S: start(next turn)
  Note over S: a turn before it never closed: close it, send settle
  S-)T: settle(dead turn), queued behind the next turn
  N->>DB: load: the latest turn has no answer, and is another prompt's
  N->>DB: close it: results for its open calls, answer = Ended
  N->>DB: ask: the next turn's row
  N->>DB: the run, the answer
  N->>S: close(next turn)
  T->>DB: the latest turn: the next one, answered
  Note over T: nothing to do
```

### Which closes it, a table

| The dead turn is closed by | When | Its calls' outcomes |
| --- | --- | --- |
| `settle`, from the companion's `awaitTurn` | as soon as Restate fails the invocation | read, if still stored |
| `settle`, from the next turn's `start` | after the next turn, which closed it first | (the next turn did) |
| the next turn's `answer_once` | before its prompt | read, if still stored (the next turn stored none yet) |

## Attempts: crashes and retries

A crash (a process that dies, a lost connection, a retryable error) ends an
attempt, not the turn. Restate runs the handler again from its first line,
and the turn goes on from what the database has:

```mermaid
sequenceDiagram
  autonumber
  participant A1 as Attempt 1
  participant A2 as Attempt 2
  participant DB as Key's database
  participant S as Companion
  A1->>S: start(turn)
  A1->>DB: the turn's row, the model's reply, a tool call
  Note over A1: the process dies
  A2->>S: start(turn): the same turn, nothing changes
  A2->>DB: the prompt's answer? none
  A2->>DB: load: the latest turn is this prompt's, without an answer
  A2->>A2: the cut-off tool call runs again, or the model makes it again
  A2->>DB: the call's result, the next reply, the answer
  A2->>S: close(turn)
```

And a crash after the answer was stored:

```mermaid
sequenceDiagram
  autonumber
  participant A1 as Attempt 1
  participant A2 as Attempt 2
  participant DB as Key's database
  participant S as Companion
  A1->>DB: the answer
  Note over A1: the process dies before the turn closes
  A2->>S: start(turn): the same turn
  A2->>DB: the prompt's answer? found
  Note over A2: nothing else is read, the model is not asked
  A2->>S: close(turn)
```

- **The prompt, once.** Every attempt asks for the same prompt id, which
  finds the turn stored, answered or unfinished, rather than asking the
  model again. A run that stored its final reply but not its answer answers
  with that reply.
- **Tool calls, once.** A tool call has an idempotency key of the
  invocation and its call id, `<invocation>:<call id>`. Restate's own tools
  (`call`, `send`, `workflow`, `subagent`, `wait_for_signal`) make their call
  once however often the tool call runs: a framework that gives a cut-off
  call a new id gets the earlier call's id back
  ([`_calls.py`](../../python/restate/agents/_calls.py)).
- **Waits survive.** A tool waiting for a signal waits in the companion
  (`wait`), by that key: the next attempt rejoins the same wait, and the
  turn is still answered meanwhile. A framework's own tool call that waits
  for something outside (Pydantic AI's deferred tool calls, say) waits the
  same way, through `wait_for(ctx, call_id, signal, input)` in
  `restate.agents.internal`. Such a wait is its tool call's alone: the
  framework stored the call before it waits, so a new call with the same
  input (the model asking again after the first's answer) waits anew, and
  never gets an earlier call's approval. An approval (`approval=True`) is
  not the call: a turn stopped before the call's result is stored reports
  the call as not done, whatever its approval returned.
- **A dead attempt stops.** An attempt that ends while its process lives on
  (Restate suspended it, gave up on it, or lost its connection) stops its
  work, a model's request included: it can store nothing more, and the next
  attempt goes on. This is not a cancellation: the turn goes on.

## Stopping a turn

`stop` cancels the invocation of the message being answered: Restate's
cancellation, as its admin API's `PATCH /invocations/<id>/cancel`.

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant V as Agent (stop view)
  participant S as Companion
  participant A as Agent (chat)
  participant DB as Key's database
  C->>V: stop
  V->>S: status (keyed by the stop request)
  S-->>V: answering: turn
  V-)A: cancel(turn)
  V-->>C: stopped: turn
  Note over A: the framework stops, a model request too
  Note over A: its tools' Restate calls are cancelled, a wait forgets itself
  A->>DB: results for the cut-off calls, answer = Cancelled
  Note over A: sealed: a task of the run left behind stores nothing more
  A->>S: close(turn)
  Note over S: idle, publish
  Note over A: fails with the cancellation, the next message starts
```

- The view asks `status` with an idempotency key of its request: a `stop`
  that Restate runs again gets the same turn, and cancels it again (which
  does nothing), never the message that started after it.
- The turn ends cancelled whenever the cancellation comes, and the next
  message starts afresh: the cancelled prompt is over.
- A cancellation that comes once the turn answered changes nothing: the
  answer stands.
- A tool cut short by the stop raises the stop's reason, so the model never
  carries on with it. If the framework cancelled the tool's own task, the
  tool ends cancelled instead: a framework that cancels a task and waits for
  it (ADK 2's runner) expects nothing else. The turn ends cancelled either
  way.
- `clear` stops the turn the same way, then forgets the conversation (the
  key's database, the companion's waits and kept signals). A message sent
  before `clear` is answered first, then forgotten too. Message ids and
  cursors carry on.

## Steering a turn

A message sent to the agent's `steer` view, rather than to `chat`, joins the
turn being answered: the turn takes it at its next **step** (once the
model's reply has a result for each of its tool calls, before the model's
next request), and the model's next request has it, after those results.
The turn's answer then answers the turn's messages so far.

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant V as Agent (steer view)
  participant S as Companion
  participant A as Agent (chat)
  participant P as Agent (steered)
  participant DB as Key's database
  C->>A: chat/send("Plan my week")
  A->>S: start(turn, steers)
  A->>DB: the model's reply, a tool call
  C->>V: steer("Skip Friday")
  V->>S: steer(id, text)
  S-)P: steered(id, text): queued now, behind the turn
  Note over S: inbox: the message, for this turn
  A->>S: publish (the tool call ends)
  S-->>A: revision, steering: [the message]
  A->>DB: the tool's result
  A->>DB: the message, a step of the turn, and taken(id) in the transaction's outbox
  DB--)S: taken(id), once the transaction commits
  A->>A: the model's next request has it
  A->>DB: the answer
  A->>S: close(turn)
  Note over P: runs once the turn ended: the message is stored, so it does nothing
```

- **Where it waits.** The companion's `steer` sends the agent's private
  `steered` handler the message at once, so it takes its place in the key's
  queue as it is sent; and, if the turn being answered takes steered
  messages (its `start` says so, `Started.steers`), it holds the message for
  that turn (`inbox`).
- **How the turn learns of it.** From `publish`, which the turn makes at
  each tool call's start and end anyway: the companion answers it with the
  messages waiting for the turn. A turn that nobody steers makes no call
  more.
- **Taken once.** At a step, the framework's integration calls
  `take_steers`: the turn stores each waiting message by its id, once, as a
  message of the user that is a step of the turn, and the integration puts it
  in the request that is about to go out, in place: the framework's run goes
  on, with its limits, guardrails and state. The transaction that stores the
  messages sends the companion `taken` from its outbox, once, with the
  commit.
- **Or answered as a turn of its own.** Once the turn ended, `steered` runs.
  It does nothing if the turn stored the message, and otherwise answers it
  as a turn of its own: a message that came after the turn's last step
  (while the model wrote its final answer), one sent to a turn that takes no
  steered messages, or past the cap, or one sent with no turn being answered
  (it starts one, as `chat/send` does). So each message is answered once, in
  the order it was sent with the messages after it.
- **A taken message is its turn's.** A turn killed after it took a message
  ends `ended`, and the message with it. A reply dropped to ask the model
  again drops the messages taken after it, which `steered` then answers.
- **What the model sees.** In the window, the message is where it was taken.
  Later, an earlier turn is its message, the messages steered to it, and its
  answer; budgets and summaries count them. `latest_ask` skips them: the
  turn goes on from its prompt (an ADK invocation, a graph's run).
- **Limits.** A step comes once a reply's tool calls all have results: a
  tool that waits a day for an approval holds the message until it
  returns (stop the turn to change course sooner). A turn takes at most 16
  messages (`STEERS`); past them, each is a turn of its own. A LangGraph
  graph (`graph=`) and a background agent take none.

## The turn model

A turn is the unit of the conversation, in storage, in code and in views.
Turns are composed into one flat list in one place only: what the model gets.

- **Identity.** A turn's id is its prompt's id. Every row of the
  conversation belongs to one turn: the turn's row, or one of its items.
- **Ownership.** Only the latest turn, while it has no answer, is written,
  and only by its own run. The one exception is closing a turn whose
  invocation is gone (`ended`): `settle`, or the next turn, writes its
  closing record (a result for each open call, the note, the answer), and
  nothing else.
- **Scope.** A turn's run reads and writes its own turn: its open calls, its
  replies, its final reply, its drops. Earlier turns are read-only context.
- **Lifecycle.** A turn is being answered (no status), then ends once, with a
  status: `answered` by its run, or closed as `cancelled`, `failed`,
  `refused` or `ended`. The status, the answer, the answer's message and
  what the turn carries are stored in one update of its row.
- **Two projections.** The transcript (each turn's message and answer) and a
  turn's detail (its steps), read separately: the `messages` view's
  `summary` and `full` views.
- **One composition.** `Conversation.history()` composes the turns into what
  the model gets: the summary, the earlier turns' messages and answers, the
  window's turns whole. Nothing else flattens turns.

In code ([`_conversation.py`](../../python/restate/agents/_conversation.py)):

```
Conversation                      the key's conversation, loaded for a turn
  transcript                      every loaded turn's row: prompt, status, answer, carried
  turns                           the window's turns, whole
  turn                            the latest turn: the one a run answers
  history()                       what the model gets: the one place turns are composed
  knows_result(), new_results()   what the model was given, across turns (to tell new results)
  ask(prompt_id, text) -> Turn    start the next turn: refused while another has no answer

Turn                              one prompt, and what answers it
  id, n, status, answer, prompt   its row
  entries, items                  its prompt and steps
  open_calls(), replies(), last_reply(), final_reply(), stored()   its own state, never another turn's
  latest_ask()                                                      its latest message of the user
  append(), append_reply(), drop(), carry(), ask_again(text)        its run's writes
  answered(answer), close(ctx, status, note)                        its end: once
```

A turn always ends with a message of the assistant: its last reply (with no
tool calls, and no call or result after it), the note that closed it, or
else its answer, stored as its reply, from the agent the turn is with (a
turn that ended with a tool's output that is the answer, say). That message
is the answer's in the summary, under the same id. Only an empty answer gets
no message. A LangGraph graph, which keeps its own messages, is given only a
closed turn's closing record, never an answer the conversation stored.

**Asking again within a turn.** A turn can ask the model again before it
answers, once: `answer_once`'s `check` reads the run's answer, and if it
returns a message, the turn stores that message as a step of its own (a
message of the user, `Turn.ask_again`) and runs again. The turn's answer is
the reply to it. `output=` asks so for an answer that does not validate
("Reply again, with JSON only: ..."). It is not a turn: the summary shows
the person's message and the answer, and the full view shows the ask as a
step of the turn. A framework that runs from a message of its own (ADK's
invocation, a LangGraph graph's input) runs from the turn's latest message of
the user (`latest_ask`). A retried attempt that finds the ask stored does
not ask again. The check reads the reply to the ask too (`last`): a
`TerminalError` it raises fails the turn, which is closed `failed` with it,
as `output=` does for a reply that still does not validate.

**Names.** A turn is the conversation's (`restate.agents.internal.Turn`).
What `status` reports, `answering`, is the invocation being answered
(`restate.agents.Answering`: its wire format, `invocationId` and `since`, is
shared with Restate's other SDKs).

## Several prompts in one handler

A handler of your own may prompt more than once (`openai.prompt(ctx, …)`
twice, say): each prompt is a turn of the conversation, `<id>`, then
`<id>:2`. A retried attempt makes them in the same order, so each finds its
own answer, and the companion's turn is the invocation, all of them.

## What a client sees

| View | What it gives |
| --- | --- |
| `status` | `answering`: the turn from its `start` to its close, or null; `waiting`: its tools' waits; `steering`: the messages steered to the turn that it has not taken yet (absent if none) |
| `pending` | the waits, oldest first, each with its id (`<turn>:<call>`) for `signal` |
| `messages` | the conversation as stored. `view: "full"` (the default): every turn, with its tool calls and their outputs, the messages steered to it where it took them, and how a closed turn ended. `view: "summary"`: each turn's message, the messages steered to it, and its answer, under the same ids |
| `notifications`, `watch` | the revision; `watch` waits (up to a minute) for a newer one |
| `sync` | the revision, `status`, and the messages since a cursor, at once: what a follower reads |

The revision increases at:

- a turn's start, and each of its tool calls' start and end (not in the
  background);
- a turn's close: its own, its watch's, or the next turn's `start` closing
  it;
- `settle`, once it closed a dead turn in the conversation;
- a wait that starts, is resolved by a `signal`, or is forgotten (a
  cancelled turn's);
- a message steered to the turn, and the turn taking it (`taken`);
- `clear`.

So a follower that reads each new revision sees every turn begin, its tool
calls, its end, and a killed turn end.

## Agents of the TypeScript SDK

The companion's handler names are shared with the TypeScript SDK, so one
deployment's `signals` may hold the turns of the other's agents. An agent
says it has `settle` as it starts a turn (`Started.settles`, which `status`
does not show), and only such a turn is sent it:

- a TypeScript agent's turn that this SDK's companion holds is closed in the
  companion, and its agent is sent nothing;
- this SDK's agent whose turn the TypeScript companion holds has a dead turn
  closed by its next message.

A turn says it takes steered messages the same way (`Started.steers`): the
companion holds messages only for such a turn, and every other gets them as
turns of their own. A TypeScript companion, which has no `steer`, answers
`publish` with no `steering`, so this SDK's turns take none from it; and the
`steer` view fails there, as the companion has no such handler.

## Guarantees and limits

- **One turn at a time per key**, in the order messages came. A turn is
  joined only by a message sent to `steer`, at a step.
- **A steered message is answered once**: in the turn that took it, or as a
  turn of its own, in the order it was sent with the messages after it.
- **A turn is completed from its own rows**, and reads a bounded part of the
  conversation: the transcript the budget holds, and the window.
- **`status` says the turn being answered** from its start to its close,
  however it ends: answered, refused, stopped, killed, or out of retries
  (if Restate fails it). A crashed turn is still being answered: it is.
- **A dead turn is closed, never finished**, by `settle` or the next
  message, with a result for each of its open calls.
- **A turn's waits end with it**: a cancelled turn's forget themselves; a
  killed turn's are dropped when it closes.
- **A turn's work is done once** across attempts: its prompt by its id,
  Restate's calls by their idempotency keys. A tool of your own does
  something once if it uses the idempotency key it is given.
- **Limits:**
  - A LangGraph graph that trims its run's prompt away has the messages it
    keeps copied by id: those of the loaded turns are not copied again.
  - An agent without a summary trims its earliest turns one at a time, so
    the start of the model's input changes at each turn, and a provider's
    prompt cache misses.
  - A crash right after an answer is stored leaves the turn's finished
    Restate calls recorded until the key's next invocation replaces them.
  - A summary that keeps failing is tried again after each turn.
  - Waits started by a handler other than `chat` (your own handler that
    prompts a framework with a `wait_for_signal` tool) are not turns: if
    that handler is killed, its waits stay in `pending` until a `signal`
    resolves them or `clear` forgets them.
