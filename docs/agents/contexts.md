# The agent's contexts: `AgentContext` and `AgentViewContext`

An agent's handler gets an `AgentContext`, its view an `AgentViewContext`.
They are the session contexts with a name that says which layer they belong
to, and they let a type checker catch a handler reaching up a layer — a
`wait_for_signal` from a plain session object, say — where today only the
running turn finds out.

They are a **signpost, not a fence**. Nothing new exists at runtime, nothing
is checked at runtime by them, and `NeedsAgent` / `NoCompanion` stay exactly
as they are: the guard that actually stops a wait from a session object.

## Why

`restate.agents` sits on `restate.sessions`. The lower tier is a session
object with handlers of its own: `ctx.db`, the Restate tools that need
nothing more (`tool`, `call`, `send`, `workflow`, `subagent`, and the call
records of `_calls.py`), and a framework driven by hand — pi-durable over
`openSqlKvStorage(ctx.db)` in TypeScript
([`packages/template/src/16_pi_durable.ts`](https://github.com/restatedev/sdk-sessions)),
`withPi` for more than a prompt. The upper tier is an agent: a conversation
of turns, the views, steering, signals, and the companion
(`<agent>.prebuilt.restate`) that holds them.

Both tiers hand a handler the same `SessionContext`, so the agent's own APIs
accept a context from either. Reaching up a layer is only found out when it
runs, and for a wait that means after a model round-trip, in the middle of a
turn, looking like a tool failure.

Naming the contexts puts the boundary in the signature, where the person
writing the handler reads it.

## What they are

| | Python | TypeScript |
| --- | --- | --- |
| The handler's | `AgentContext(SessionContext, Protocol)` | `interface AgentContext extends SessionContext` |
| The view's | `AgentViewContext(ViewContext, Protocol)` | `interface AgentViewContext extends ViewContext` |

**Python.** The session contexts are `typing.Protocol`s
(`restate/sessions/_api.py`), so the agent's are protocols that extend them
with one private marker member, declared and never present at runtime:

```python
class AgentContext(SessionContext, Protocol):
    """The context of an agent's handler: a session context, from an object that has a companion."""

    @property
    def _agent(self) -> None:
        """A marker, never present at runtime and never read: it is what makes a plain session context
        fail to be an ``AgentContext`` for a type checker."""
```

The marker is a fiction, and the rules that keep it honest are:

- **Nothing reads it.** No `ctx._agent`, no `getattr`, no `hasattr`, no
  `isinstance` (a protocol with a non-method member cannot be
  `runtime_checkable` anyway).
- **Nothing constructs it.** `restate/sessions/` does not change at all:
  `_context_of` keeps building `AttemptContext` and `AttemptViewContext`,
  and the agent's handlers receive those, as now.
- **No cast is needed at the boundary.** `SessionObject.handler` and `.view`
  are bound to `Callable[..., Awaitable[Any]]`, so a function annotated
  `AgentContext` registers with no complaint, and the runtime passes what it
  always passed.

**TypeScript.** Plain `extends` with no extra member and no brand. Two
interfaces that are structurally identical are mutually assignable, so this
does not stop anything — it names the layer in the signature, which is what
it is for.

## What changes

### Python

1. **Declare** `AgentContext` and `AgentViewContext` in the agents package
   (`python/restate/agents/_api.py`, beside the other public shapes), export
   them from `restate.agents`, and name them in
   `python/restate/agents/internal.py` for integrations.
2. **Narrow the agent's own surface** — what an agent registers, and what
   cannot run without the companion:

   | Where | What |
   | --- | --- |
   | `_object.py` | `chat()`'s `answer`, the built-in views, `_stopped`, and the private handlers (`erase`, `settle`, `compact`, `steered`, `clear`, `sync`, `messages`, `steer`) |
   | `_turn.py` | `chat_turn`, `publish`, `publish_steps`, `publish_end`, `cleared` |

3. **Move the one strict bound**: `A = TypeVar("A", bound=Callable[[SessionContext, str], Awaitable[Any]])`
   in `agents/_object.py` becomes `AgentContext`. Parameter types are
   contravariant, so this is the one place where a user's `answer` annotated
   `AgentContext` is rejected today.
4. **Leave the shared tier on `SessionContext`**: `tool()`, `call`, `send`,
   `workflow`, `subagent`, `run_tool`, everything in `_calls.py`, and
   **`ToolCall.ctx`**. A tool is a value, passed to a framework, which may be
   run from either tier — the type cannot reach through it, and
   `wait_for_signal`'s tool keeps `NeedsAgent` as its guard.
5. **Keep `waits_in(ctx: SessionContext, ...)`** as it is: it is the
   backstop, and it must go on taking the wider type to raise `NeedsAgent`
   rather than fail to compile.

### What stays wide, and why

A first draft of this note also listed the conversation, the turn's helpers,
the signal waits and the langgraph nodes. None of them can narrow, and the
reason is the same each time: **they are the lower tier's entry points, or
the probes that find out which tier they are on.**

- **`_conversation.py` — `answer_once`, `settle`, `compact`.** A handler of a
  plain session object drives them: `tests/agents_conversation.py` registers
  `answer_once` on a `sessions.SessionObject`, and each framework's public
  `prompt(ctx: SessionContext, ...)` — the documented "for a handler of your
  own" — funnels into it. `settings_of` returns "the defaults, for a session
  object that is no agent" by design.
- **`_turn.py` — `settings_of`, `stop_signal_of`, `inbox_of`, `settled`,
  `answering`, `tool_call_started`.** `answer_once` calls them with a
  session context, so they cascade from the point above.
- **`_tools.py` — `wait_for`**, and **`_steering.py` — `take_steers`**. Both
  are reached from a framework's `prompt()`. `take_steers` is written to be
  safe on a plain context: it returns `[]` before touching the companion
  unless it proves it is in a chat turn.
- **`_signals.py` — `companion_at`, `companion_in`, `deliver`.**
  `companion_at` is the probe (`"or None for another session object's"`),
  `waits_in` calls it, and `deliver` takes any Restate handler's context on
  purpose.
- **`langgraph/nodes.py`, `langgraph/_graph.py`.** The signal node takes its
  context from the graph runtime as `SessionContext | None`. Narrowing it
  would need a cast asserting "this is an agent" at the very spot whose job
  is to find out that it is not.

So the narrowed surface is the agent's registration surface, and the guard
for everything reached through a framework stays `NeedsAgent`. That is the
honest reach of a type that is only a signpost.

### TypeScript

The same split, and the same line: declare the two interfaces in
`packages/agents/src/common.ts`, export them from the package, and type the
agents' own surface with them — `sessionObject()`'s and `agent()`'s handlers
and views, `chatTurn`, `publish`, `tookSteers`, and the object's private
handlers. The tools (`tool`, `call`, `send`, `workflow`, `subagent`) and
`ToolCall`/`ToolContext` stay on `SessionContext`, as in Python.

TypeScript could narrow more than Python can, since nothing there is
structurally barred: `waitFor`, `companionHere` and `takeSteers` would
compile either way. They stay wide so that the two SDKs say the same thing
about the same function, and so that `companionHere` stays what `waits_in`
is — the raiser of `NoCompanion`, which needs the wider type to be reached
at all.

One asymmetry worth knowing: in TypeScript every object from
`@restatedev/agents`' `sessionObject()` carries a companion, including a
hand-driven one such as `packages/template/src/16_pi_durable.ts`. So
`AgentContext` on its handlers is accurate there in a way that Python's
`SessionObject` / `AgentObject` split is not.

## What must hold

- `restate/sessions/` and `packages/sessions/` are untouched: `git diff`
  shows no file under either.
- A handler annotated `SessionContext` still registers on an agent and still
  type-checks: a function taking the supertype satisfies a callback given
  the subtype. No user code breaks — the tutorial in modal-template annotates
  `sessions.SessionContext` throughout.
- A handler annotated `AgentContext` registers on a plain `SessionObject`
  too: nothing prevents it, and nothing should. The protocol says what the
  handler expects, not what the object is.
- `pyright` and `mypy` pass (both are dev dependencies, with `[tool.pyright]`
  in `pyproject.toml`), and `tsc -p .` passes in sdk-sessions.
- `NeedsAgent` and `NoCompanion` keep their tests: the wait from a plain
  session object still fails for good, with the same message.
- No new runtime behaviour: no class, no attribute, no check. A diff that
  adds one has gone wrong.

## Non-goals

- Signals for session objects. A session has no awakeable to park on, no
  `pending` to list a wait, and no `signal` view to resolve it, and this does
  not change that.
- Making the marker real. It stays a fiction; the runtime guard is
  `NeedsAgent`.
- A brand in TypeScript. Structural assignability is accepted: the names
  guide the reader.
