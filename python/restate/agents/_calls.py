#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""Track the Restate calls that an invocation's tools make, so each is made only once.

The tools built on Restate (``call``, ``send``, ``workflow``, ``subagent``,
``wait_for_signal``) record their calls in the object's database, in
``restate_agent_calls``, so that a call is made once however often its tool
call runs.

A crash or a suspension restarts the handler. The framework then either runs
the interrupted tool call again with the same id, or tells the model it was
interrupted, and the model makes it again under a new id. Either way, a call
with the same tool and input as one an earlier attempt made reuses that
call's id, and so its idempotency key: it is the same Restate call, which it
waits for again or whose result it gets.

A call is kept until the framework's transcript has its result under the
latest id it was made with (``settle`` checks this at each publish and before
each tool call). An error in the transcript does not count as the result,
unless the call itself returned that error.
"""

from __future__ import annotations

import json
import typing
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from restate.sessions import SessionContext
from restate.sessions.internal import Schema, ScopeKey

SCHEMA = Schema(
    "restate-agents.calls",
    [
        """CREATE TABLE restate_agent_calls (
            row INTEGER PRIMARY KEY CHECK (row = 0),
            invocation TEXT NOT NULL,
            calls TEXT NOT NULL
        )"""
    ],
)


class _Made(typing.TypedDict):
    """A tool's call: the tool, its input as JSON, the tool call ids it was made under (the first is its own), and its attempt."""

    tool: str
    input: str
    ids: list[str]
    attempt: str


class _Call(_Made, total=False):
    """A tool's call, and whether it has returned."""

    done: bool
    """Whether the call has returned; kept until the transcript has its result."""
    failed: str
    """The terminal error the call returned (``Error: <message>``), which is its result in the transcript."""


@dataclass
class _Calls:
    """The calls an invocation's tools made whose results are not yet in the transcript."""

    invocation: str
    calls: list[_Call] = field(default_factory=list[_Call])


async def _store(ctx: SessionContext, state: _Calls) -> None:
    """Store ``state``; it is committed before the call it records is made."""
    await ctx.db.execute(
        "INSERT OR REPLACE INTO restate_agent_calls (row, invocation, calls) VALUES (0, ?, ?)",
        [state.invocation, json.dumps(state.calls)],
    )


async def _load(ctx: SessionContext, invocation: str) -> _Calls:
    """Load the stored calls of ``invocation``; none if the row belongs to another invocation."""
    await SCHEMA.ensure(ctx.db)
    row = await ctx.db.get("SELECT invocation, calls FROM restate_agent_calls")
    if row is None or row["invocation"] != invocation:
        return _Calls(invocation=invocation)
    return _Calls(invocation=invocation, calls=json.loads(typing.cast(str, row["calls"])))


@dataclass
class _Attempt:
    id: str
    calls: _Calls | None = None


_ATTEMPT: ScopeKey[_Attempt] = ScopeKey("the calls of an attempt")


async def _calls_of(ctx: SessionContext) -> tuple[_Attempt, _Calls]:
    """Return the current attempt and the calls of its invocation."""
    attempt = _ATTEMPT.of(ctx, lambda: _Attempt(id=str(uuid.uuid4())))
    if attempt.calls is None:
        attempt.calls = await _load(ctx, ctx.request.id)
    return attempt, attempt.calls


@dataclass
class Call:
    """The call a tool call makes: its id (possibly from an earlier attempt), and a way to record that it returned."""

    id: str
    _ctx: SessionContext
    _state: _Calls
    _call: _Call

    async def answered(self, failed: str | None = None) -> None:
        """Record that the call returned, with ``failed`` if it returned a terminal error."""
        self._call["done"] = True
        if failed is not None:
            self._call["failed"] = failed
        await _store(self._ctx, self._state)


async def call_of(ctx: SessionContext, tool: str, input: object, call_id: str) -> Call:
    """Return the call that tool call ``call_id`` makes to ``tool`` with ``input``.

    That is the call it made before (if it runs again), or else the call an
    earlier attempt made with the same tool and input (if the model made it
    again under a new id), or else a new call.
    """
    attempt, state = await _calls_of(ctx)
    encoded = json.dumps(input, sort_keys=True)
    call = next((c for c in state.calls if call_id in c["ids"]), None) or next(
        (c for c in state.calls if c["attempt"] != attempt.id and c["tool"] == tool and c["input"] == encoded),
        None,
    )
    if call is not None:
        call["attempt"] = attempt.id
        if call_id not in call["ids"]:
            call["ids"].append(call_id)
    else:
        call = _Call(tool=tool, input=encoded, ids=[call_id], attempt=attempt.id)
        state.calls.append(call)
    await _store(ctx, state)
    return Call(id=call["ids"][0], _ctx=ctx, _state=state, _call=call)


async def settle(ctx: SessionContext, stored: Callable[[str, str | None], bool]) -> None:
    """Forget the calls that returned and whose results the framework has stored.

    A call's result is stored if ``stored(id, failed)`` is true, where ``id``
    is the latest id the call was made under and ``failed`` is the error it
    returned, if any.
    """
    _, state = await _calls_of(ctx)
    kept = [c for c in state.calls if not c.get("done") or not stored(c["ids"][-1], c.get("failed"))]
    if len(kept) == len(state.calls):
        return
    state.calls = kept
    await _store(ctx, state)
