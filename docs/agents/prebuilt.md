# The agent's prebuilt object: `<agent>.prebuilt.restate`

Every agent brings one object of its own, `<agent>.prebuilt.restate`, keyed
as the agent. It replaces `signals`, the single object both SDKs ship today,
and it is the agent's alone: a plain session object has none, and needs none.

This note is the design for both SDKs. It was written against sdk-python
`c56a55b` and sdk-sessions `3b451c4` (the branches), with sdk-python
`4730b10` and sdk-sessions `5871ed4` (`main`) as the shipped state.

## Where the two SDKs stand

| | The object | Its key | Where |
| --- | --- | --- | --- |
| Shipped (both `main`s) | `signals`, one for the whole endpoint | `<agent>:<key>` (`assistant:alice`) | sdk-python `_signals.py` at `4730b10`; sdk-sessions `packages/agents/src/signals.ts` |
| The branches now | `<agent>.agentprebuilt.restate`, one per agent | the agent's own key (`alice`) | sdk-python `_signals.py`; sdk-sessions `packages/agents/src/companion.ts` |
| **Decided here** | **`<agent>.prebuilt.restate`, one per agent** | the agent's own key | the same files, renamed |

The move from one shared object to one per agent is already made on both
branches; what is left is the name, and taking the word `signals` out of the
code with it.

## Why the shared `signals` has to go

1. **Two endpoints, one name.** A Python endpoint and a TypeScript endpoint
   registered with the same Restate cluster each register a service called
   `signals`. There is one service of that name in a cluster, so the second
   registration takes it over: whichever SDK registered last serves the
   companion for both SDKs' agents, with its own handler set and its own
   payload details. Nothing warns about it. This is the collision that
   started this note, and no per-endpoint fix removes it — only a name that
   cannot be shared does.
2. **Serving a subset shares the name too.** One agent per image
   (sdk-sessions' `RESTATE_SERVE_ONLY`, or a `restate.app([one])` of its
   own in Python) means every image also serves `signals`, and every one of
   them claims the same service: the same clash inside a single language.
3. **It sits in the user's namespace.** `signals` is an ordinary name that a
   user may want for a service of their own. `<agent>.prebuilt.restate`
   cannot be mistaken for one, and cannot be taken by accident.
4. **The composite key hides the agent.** `<agent>:<key>` means one state
   table for every agent in the endpoint, and every query, cleanup or
   inspection has to parse keys apart. With one object per agent, the key is
   the agent's own key, and `service_name` tells the agent.
5. **Deployment follows the agent.** A companion served with its agent is
   versioned and rolled out with it; a shared one is a single service that
   every agent's deployment fights over.

## The name

`<agent>.prebuilt.restate`: the agent's name, then the marker. Restate
refuses service names that *start* with `restate`, so the marker is a
suffix and ends with it.

- **Not `.agentprebuilt.restate`** (what the branches carry): `agent` says
  nothing the agent's own name does not already say, and one marker is
  enough. Shorter reads better in a service list, in a state query and in an
  error message.
- **One prebuilt object per name.** The suffix is the agent's single reserved
  name. If anything else ever needs to live beside an agent, it goes *inside*
  this object rather than beside it under a second suffix.
- **The name is derived, never configurable**: `companion_name(agent)` /
  `companionName(agent)`, from the agent's name alone, in both SDKs. That is
  what keeps the two SDKs' names identical for the same agent.
- **Collisions.** An agent named `a` and an agent named `a.prebuilt` do not
  collide (`a.prebuilt.restate` against `a.prebuilt.prebuilt.restate`). An
  agent named `support.prebuilt.restate` would own another agent's companion
  name: refuse that name where the agent is constructed (`ValueError` in
  Python, a thrown `Error` in TypeScript), the way the built-in handler names
  are already refused.

## Only agents have one

A plain `SessionObject` gets no prebuilt object. Sessions have no signals, no
turn and no revision — the database is a session's only state — and nothing
in `restate.sessions` or `@restatedev/sessions` changes here. A
`wait_for_signal` from a plain session object's handler keeps failing for
good, with `NeedsAgent` (Python) and `NoCompanion` (TypeScript), which both
branches already do (sdk-python `4268641`).

**The word `signals` goes; the word `signal` stays.** The public vocabulary —
the `signal` view, `pending`, `wait_for_signal`, `waitFor` — is unchanged.
What disappears is the *object* called `signals`, and the module and test
names that follow from it.

## What the object holds

Unchanged by this rename, and listed here so the rename can be checked
against it.

| State | What it is | Written by |
| --- | --- | --- |
| `waiting` | the tools' waits, oldest first, each with its awakeable | `listen`, `resolve`, `forget`, `close` |
| `kept` | signals that arrived with nobody waiting, by name (`keep`) | `resolve`, `listen` |
| `turn` | the message the agent is answering (`Started`) | `start`, `close` |
| `inbox` | messages steered to the turn being answered, taken or not | `steer`, `taken`, `close` |
| `revision`, `watching` | the conversation's revision, and the `watch`es waiting for a newer one | every publish; `subscribe`, `unsubscribe` |

| Handlers | |
| --- | --- |
| Signals | `wait`, `resolve`, `pending`, `listen`, `forget` |
| The turn | `start`, `close`, `ended`, `awaitTurn` |
| Steering | `steer`, `taken` |
| Notifications | `publish`, `notifications`, `watch`, `subscribe`, `unsubscribe` |
| The rest | `status`, `reset` |

All of them stay ingress-private: clients reach them through the agent's own
views (`status`, `pending`, `signal`, `steer`, `watch`, `sync`). The handler
names, `awaitTurn` among them, stay as they are — they are the wire the two
SDKs share.

## No migration

Nothing carries the companion's state over, and nothing needs to. The
shared `signals` and `<agent>.agentprebuilt.restate` are both preview, and
neither per-agent companion has been released: the new object starts empty
for every key, and whatever a running `signals` holds — waits, the turn
being answered, the steering inbox, the revision — is dropped with it.

The conversation itself is untouched. It lives in the key's SQL database:
messages, turns, statuses and cursors all survive the rename, since the
companion holds none of them.

## The change, file by file

The wire name is the only thing that changes in behaviour. In the code, keep
**companion** as the word for the thing — `companion_of` / `companionOf`,
`COMPANION_SUFFIX`, `companion.ts` — and let `prebuilt` be the wire name
alone.

### sdk-python

| File | Change |
| --- | --- |
| `python/restate/agents/_signals.py` | `COMPANION_SUFFIX = ".prebuilt.restate"`, and the module docstring; **rename the file to `_companion.py`** (it matches `companion.ts`, and it is the last place called `signals`) |
| `python/restate/agents/internal.py` | the import, and the paragraph naming `<agent>.agentprebuilt.restate` |
| `python/restate/agents/_object.py`, `servable.py`, `endpoint.py` | the name in the docstrings |
| `docs/agents/README.md`, `turns.md`, `conversation.md`, `porting.md`, `access-patterns.md`, `README.md` (root) | the name, and `porting.md`'s table row, which still describes `signals` as the TypeScript state |
| `tests/agents_companion.py` (11), `tests/agents_signals.py` (4), `agents_object.py`, `agents_steering.py`, `agents_snapshots.py`, `agents_tools.py`, `servable.py` | the expected names; **rename `tests/agents_signals.py` to `tests/agents_companion_server.py`**, the against-a-server twin of `agents_companion.py` |
| `scripts/steering_traffic.py`, `scripts/check_no_extras.py` | the name |

### sdk-sessions

| File | Change |
| --- | --- |
| `packages/agents/src/companion.ts` | `COMPANION_SUFFIX = '.prebuilt.restate'`, and the header comment |
| `packages/agents/src/object.ts` | the name in the comment |
| `packages/agents/README.md` (8), `docs/README.md`, `turns.md`, `conversation.md`, `access-patterns.md`, `plan.md` | the name; `plan.md` row D2 records the decision |
| `packages/agents/test/companion.test.ts` (10), `webhook.test.ts`, `tools.test.ts`, `layers.test.ts`, `calls.test.ts`, `steering.test.ts`, `support.ts` | the expected names |
| `packages/e2e/restate-tools.ts` | the `/services` assertion, the `state` query by `service_name`, and the per-handler ingress checks |

`packages/agents/src/signals.ts` is already gone on the branch, replaced by
`companion.ts`; on `main` it is what this whole note deletes.

## What must hold afterwards

- `companion_name("one") == "one.prebuilt.restate"` in both SDKs, from the
  agent's name alone, with no normalisation beyond it (`Odd-Name_1` keeps its
  spelling).
- An endpoint serving agents `one` and `two` serves exactly `one`,
  `one.prebuilt.restate`, `two`, `two.prebuilt.restate`, and no `signals`.
- Serving one agent of several serves it with its own companion and nothing
  else: `RESTATE_SERVE_ONLY=two` in sdk-sessions, `restate.app([two])` in
  sdk-python (`__restate_services__`).
- Every companion handler is refused by the ingress, by name, in the server
  tests of both SDKs.
- The companion's state keys are the agent's keys (`alice`), not
  `<agent>:<key>` — asserted against `state` in the e2e.
- An agent named `x.prebuilt.restate` is refused where it is constructed.
- Both repos' e2e assert that no service named `signals` is registered.

## Open questions

1. **The Python file names** (`_companion.py`, `agents_companion_server.py`):
   confirm, or keep `_signals.py` as the file name and change only the
   strings.
2. **Which repo lands first.** The name must be identical in both before
   either is released; sdk-python's branch is the one the TypeScript port
   follows (`docs/agents/porting.md`), so it goes first, and sdk-sessions
   follows in the same week.
