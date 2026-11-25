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
"""Restate's side of a session object's invocation, for tests: the shared core's fake runtime of the explicit
recovery protocol (``restate._internal._FakeRuntime``), driven over an ASGI pair.

``RestateSide`` holds one invocation of a session object's handler, over as many attempts as a test runs. The fake
runtime applies what the attempt sends, answers it, keeps the object's state and the invocation's, decides between
restart and suspend after a shutdown, and checks everything the attempt sends against the protocol: a test fails on
any violation. This module adds what a test scripts around it: callees that echo their input, calls held or failed,
the cancel signal at a chosen point (before the answer to the ``n``-th thing the attempt asks for), an answer never
given. No endpoint and no server: ``run`` drives an attempt through the object's ``_runner``, as ``server.py`` does.
"""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import asyncio
import json
import struct
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from restate._internal import FakeRuntimeViolation, PyFailure, _FakeRuntime  # pylint: disable=no-name-in-module
from restate.server_types import ASGIReceiveEvent, ASGISendEvent, ReceiveChannel
from restate.sessions import SessionObject
from restate.sessions._object import ENTRIES, HEAD_KEY, _runner

# --- Protocol buffers, as much as the messages here need


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


Fields = dict[int, list[Union[int, bytes]]]
"""A decoded message: its fields' values by number, an int or the bytes of each."""


def decode(buffer: bytes) -> Fields:
    fields: Fields = {}
    at = 0

    def read_varint() -> int:
        nonlocal at
        value = 0
        shift = 0
        while True:
            byte = buffer[at]
            at += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7

    while at < len(buffer):
        tag = read_varint()
        number = tag >> 3
        value: Union[int, bytes]
        if tag & 7 == 0:
            value = read_varint()
        elif tag & 7 == 2:
            length = read_varint()
            value = bytes(buffer[at : at + length])
            at += length
        else:
            raise ValueError(f"wire type {tag & 7} of field {number}")
        fields.setdefault(number, []).append(value)
    return fields


# --- Messages of the explicit recovery protocol, version 1


class TYPE:  # pylint: disable=too-few-public-methods
    """The message types (``explicit_recovery.proto``)."""

    start = 0x0000
    shutdown = 0x0001
    error = 0x0002
    end = 0x0003
    heartbeat = 0x0004
    get_state = 0x0410
    get_state_keys = 0x0411
    set_state = 0x0412
    clear_state = 0x0413
    clear_all_state = 0x0414
    call = 0x0420
    attach_invocation = 0x0421
    cancel_invocation = 0x0422
    read_signal = 0x0430
    send_signal = 0x0431
    flush = 0x0440
    atomic_group = 0x0441
    span_start = 0x0450
    span_end = 0x0451
    result = 0x8000
    signal = 0x8001


GROUP_MEMBERS = {
    1: TYPE.set_state,
    2: TYPE.clear_state,
    3: TYPE.clear_all_state,
    4: TYPE.call,
    5: TYPE.cancel_invocation,
    6: TYPE.send_signal,
    7: TYPE.span_start,
    8: TYPE.span_end,
}
"""The members of an ``AtomicGroupActionMessage.Action``, by the field number of its ``oneof``."""

RESULT_ID = 11
"""The field of an action's result id (``ActionTemplate``): absent (0) when no result is wanted."""


@dataclass(frozen=True)
class Message:
    """One message the attempt wrote: its type and decoded fields."""

    type: int
    fields: Fields
    raw: bytes = b""

    def bytes_of(self, number: int) -> bytes:
        found = self.fields[number][0]
        assert isinstance(found, bytes)
        return found

    def int_of(self, number: int, default: int = 0) -> int:
        found = self.fields.get(number)
        if not found:
            return default
        assert isinstance(found[0], int)
        return found[0]

    def text_of(self, number: int) -> Optional[str]:
        found = self.fields.get(number)
        return None if found is None else typing.cast(bytes, found[0]).decode()

    @property
    def is_action(self) -> bool:
        return 0x0400 <= self.type < 0x8000

    @property
    def wants_result(self) -> bool:
        """Whether the action asks for its result: a commit or a flush asks to be told it is stored."""
        return self.int_of(RESULT_ID) != 0

    @property
    def is_commit(self) -> bool:
        """A commit of the database (an atomic group whose result is wanted), or a flush: what tells the attempt that
        its writes are stored."""
        return self.type in (TYPE.atomic_group, TYPE.flush) and self.wants_result

    def members(self) -> list[Message]:
        """An atomic group's actions, in order; ``[self]`` for any other message."""
        if self.type != TYPE.atomic_group:
            return [self]
        out: list[Message] = []
        for action in self.fields.get(1, []):
            assert isinstance(action, bytes)
            ((number, values),) = decode(action).items()
            out.append(Message(GROUP_MEMBERS[number], decode(typing.cast(bytes, values[0]))))
        return out


def messages(output: bytes) -> list[Message]:
    out: list[Message] = []
    at = 0
    while at + 8 <= len(output):
        type_, _flags, length = struct.unpack_from(">HHI", output, at)
        if at + 8 + length > len(output):
            break
        out.append(Message(type_, decode(output[at + 8 : at + 8 + length]), output[at : at + 8 + length]))
        at += 8 + length
    return out


def flat(sent: list[Message]) -> list[Message]:
    """``sent`` with each atomic group's actions in its place, after the group itself."""
    return [member for m in sent for member in ([m, *m.members()] if m.type == TYPE.atomic_group else [m])]


def outcome(sent: list[Message]) -> Optional[Union[tuple[Literal["value"], Any], tuple[Literal["failure"], int, str]]]:
    """How the invocation ended: ``("value", v)`` with its output deserialized, ``("failure", code, message)``, or
    ``None`` if the attempt wrote no ``EndMessage`` (it was shut down, or failed)."""
    end = next((m for m in sent if m.type == TYPE.end), None)
    if end is None:
        return None
    if 1 in end.fields:
        content = decode(end.bytes_of(1)).get(1, [b""])[0]
        assert isinstance(content, bytes)
        return ("value", json.loads(content))
    failure = decode(end.bytes_of(2))
    code = failure.get(1, [0])[0]
    message = failure.get(2, [b""])[0]
    assert isinstance(code, int) and isinstance(message, bytes)
    return ("failure", code, message.decode())


def commits(sent: list[Message]) -> int:
    """How many commits (and flushes) of ``sent`` asked to be told they are stored."""
    return sum(1 for m in sent if m.is_commit)


def output_of(sent: list[Message]) -> Any:
    """The handler's output, deserialized from the attempt's ``EndMessage``."""
    ended = outcome(sent)
    assert ended is not None and ended[0] == "value", f"the attempt ended with {ended}"
    return ended[1]


@dataclass(frozen=True)
class Made:
    """A call or a send the attempt made, as Restate applied it."""

    service: str
    handler: str
    key: str
    idempotency_key: Optional[str]
    input: str
    """The input, as the text of its bytes."""
    invocation_id: str
    """The invocation id Restate gave it."""
    is_send: bool = False

    @property
    def target(self) -> str:
        """``Service/handler``."""
        return f"{self.service}/{self.handler}"


@dataclass(frozen=True)
class _WireCall:
    """A call or send as it went on the wire, before Restate applied it."""

    service: str
    handler: str
    key: str
    idempotency_key: Optional[str]
    is_send: bool


def _wire_call(m: Message) -> _WireCall:
    return _WireCall(
        service=m.text_of(1) or "",
        handler=m.text_of(2) or "",
        key=m.text_of(3) or "",
        idempotency_key=m.text_of(6),
        is_send=not m.wants_result,
    )


HAND_OFF_TURNS = 10
"""Turns of the event loop that a message takes from the request body to the driver's tick, with margin.

The ``ReceiveChannel``'s loop takes it, the input pump hands it to the VM and
schedules a tick, and the tick acts on it: three turns.
"""


def _database_opens() -> list[asyncio.Task[Any]]:
    """The attempts' databases that are opening now (``Attempt._start``): see ``RestateSide.quiet``."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and getattr(task.get_coro(), "__qualname__", "") == "Attempt._start"
    ]


OBJECT_PRELOAD = [HEAD_KEY]
"""What Restate preloads of a session object's state: the database's head alone (lazy, the head whitelisted)."""


# --- Restate's side


class RestateSide:
    """Restate's side of one invocation of a session object's handler: the fake runtime, and the test's script.

    Args:
        state: The object's state, which the attempts read and write. The
            dict is kept in sync with the fake runtime's, in place, so a test
            holds the state an invocation left. The start carries the head
            of the database alone, as Restate preloads it for a session
            object, and the attempt reads the rest on the wire.
        cancel_on_commit: At the first commit (``True``), or the ``n``-th
            (from 1), Restate sends the cancel signal, and applies the commit
            once the attempt has acted on the signal: a cancellation that
            lands with the database waiting on Restate.
        cancel_on_read: At the ``n``-th read of the state on the wire, the
            cancel signal, and the read's answer once the attempt acted on it.
        cancel_on_keys: At the first listing of the state's keys, the same.
        cancel_on_call: At the first call, the cancel signal ``"before_id"``,
            the call's invocation id once the attempt acted on it; or
            ``"after_id"``, the signal once the attempt has the id.
        cancel_at_start: A retry of a cancelled invocation: an attempt that
            ran nothing was lost, the cancel signal came, and the attempt the
            test runs starts knowing of it (``StartMessage.cancelled``).
        cancel_before_answer: Just before Restate answers the ``n``-th thing
            the attempt asks for (from 1; see ``asks``), the cancel signal,
            and the answer once the attempt has acted on the signal.
        withhold_answer: The ``n``-th thing the attempt asks for (from 1) is
            not applied, and so not answered, nor is anything after it, until
            the attempt ends: a test shuts the attempt down meanwhile.
        hold: Calls to these targets (``Service/handler``) get their
            invocation id, and their result only once ``release``d.
        fail: Calls to these targets fail with this terminal failure,
            ``(code, message)``.
        invocation_id: The invocation's id, ``ctx.request.id``.

    A call to key ``k`` is never answered: what a started call, whose result
    nobody holds, looks like to the handler. Every other call is answered with
    its input.
    """

    def __init__(
        self,
        state: dict[str, bytes] | None = None,
        *,
        cancel_on_commit: bool | int = False,
        cancel_on_read: int | None = None,
        cancel_on_keys: bool = False,
        cancel_on_call: Literal["before_id", "after_id"] | None = None,
        cancel_at_start: bool = False,
        cancel_before_answer: int | None = None,
        withhold_answer: int | None = None,
        hold: list[str] | None = None,
        fail: dict[str, tuple[int, str]] | None = None,
        invocation_id: str = "inv_test",
        id_per_call: bool = True,  # pylint: disable=unused-argument
    ) -> None:
        # Every call gets an invocation id of its own: `id_per_call` is how the old harness asked for that.
        self.state: dict[str, bytes] = {} if state is None else state
        self.invocation_id = invocation_id
        self._cancel_on_commit = int(cancel_on_commit)
        self._cancel_on_read = cancel_on_read
        self._cancel_on_keys = cancel_on_keys
        self._cancel_on_call = cancel_on_call
        self._cancel_at_start = cancel_at_start
        self._cancel_before_answer = cancel_before_answer
        self._withhold_answer = withhold_answer
        self._hold = set(hold or [])
        self._fail = dict(fail or {})
        self.rt: _FakeRuntime | None = None
        self._key: str | None = None
        self._input: Any = None
        # The calls and sends on the wire, in the order Restate applies them, without those that reach an earlier
        # one by its idempotency key: the fake's records match them one to one.
        self._wire_calls: list[_WireCall] = []
        # The calls this side answered or holds, by invocation id.
        self._settled_calls: set[str] = set()
        self._held_results: dict[str, list[str]] = {}
        self._commits = 0
        self._calls_seen = 0
        self.asks: list[str] = []
        """What the attempt asked Restate for, in order, which Restate answers: ``read <key>``, ``ack`` (a commit or
        a flush), or ``call <Service/handler>`` (a call that is neither held nor to key ``k``)."""
        self.reads = 0
        """How many reads of the object's state the attempt made on the wire."""
        self.signalled_at: int | None = None
        """How many messages the attempt had written when the cancel signal went in, if it did."""
        self.head: dict[str, Any] | None = None
        self.ended = False
        """Whether the attempt closed its response."""
        self.channel: ReceiveChannel | None = None
        """The receive channel of the running attempt: a test notifies it of a SIGTERM."""
        self._reset_attempt()

    def _reset_attempt(self) -> None:
        self._inbound: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self._parsed = 0
        self._frames: asyncio.Queue[Message] = asyncio.Queue()
        self._delivering = 0
        self._pushed = 0
        self._closed_input = False
        self._withheld = False
        self.ended = False

    # --- The ASGI pair an attempt runs over

    async def receive(self) -> ASGIReceiveEvent:
        return await self._inbound.get()

    async def send(self, event: ASGISendEvent) -> None:
        if event["type"] == "http.response.start":
            self.head = dict(event)
        elif event["type"] == "http.response.body":
            body = event.get("body", b"")
            if body:
                self._chunks.append(bytes(body))
            if not event.get("more_body", False):
                self.ended = True

    def _push(self, chunk: bytes) -> None:
        if chunk:
            self._pushed += 1
            self._inbound.put_nowait({"type": "http.request", "body": chunk, "more_body": True})

    def _push_output(self) -> None:
        assert self.rt is not None
        self._push(self.rt.take_output())

    async def handed_off(self) -> None:
        """Return once the attempt has acted on everything pushed so far.

        The attempt has taken every message once the request body's queue is
        empty; ``HAND_OFF_TURNS`` turns of the event loop later, its input pump
        has handed them to the VM and its driver has ticked on them. Turns, not
        time: on a slow machine the attempt still acts on the message before
        anything pushed after this returns can reach it.
        """
        while not self._inbound.empty() and not self.ended:
            await asyncio.sleep(0)
        for _ in range(HAND_OFF_TURNS):
            await asyncio.sleep(0)

    async def _signal(self) -> None:
        """Send the cancel signal, and return once the attempt has acted on it."""
        assert self.rt is not None
        self.signalled_at = len(self.sent())
        self.rt.cancel()
        self._push_output()
        await self.handed_off()

    def _close_input(self) -> None:
        if self._closed_input:
            return
        self._closed_input = True
        self._inbound.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        # Ends the receive channel's loop, which reads on after the body ends.
        self._inbound.put_nowait({"type": "http.disconnect"})

    # --- What tests call

    def sent(self) -> list[Message]:
        """Everything the attempt wrote, as messages."""
        return messages(b"".join(self._chunks))

    def start(self, key: str, input_: Any) -> None:
        """Start the invocation for ``key`` with ``input_``, which ``run`` then runs."""
        self._key = key
        self._input = input_

    async def cancel(self) -> None:
        """Restate cancels the invocation now; return once the attempt has acted on it."""
        await self._signal()

    def shut_down(self, reason: Literal["inactivity", "own"] = "own") -> None:
        """Restate shuts the attempt down: it closes the request body, and the attempt answers with its count."""
        assert self.rt is not None
        self.rt.shut_down(reason)
        self._close_input()

    def release(self, target: str) -> None:
        """Answer the oldest withheld call to ``target`` (``Service/handler``), echoing its input."""
        withheld = self._held_results.get(target)
        if not withheld:
            raise AssertionError(f"no call to {target} is withheld")
        invocation_id = withheld.pop(0)
        self._answer_call(invocation_id)
        self._push_output()

    @property
    def calls(self) -> list[Made]:
        """The calls Restate applied, in order, with the invocation id each got."""
        return [made for made in self._made() if not made.is_send]

    @property
    def sends(self) -> list[Made]:
        """The sends Restate applied, in order."""
        return [made for made in self._made() if made.is_send]

    @property
    def cancels(self) -> list[str]:
        """The invocation ids the attempts sent a cancellation to, in order."""
        return [] if self.rt is None else self.rt.cancels_sent()

    def has_called(self, target: str) -> bool:
        """Whether Restate applied a call of the attempt to ``target`` (``Service/handler``)."""
        return any(c.target == target for c in self.calls)

    async def quiet(self) -> None:
        """Return once Restate took in everything the attempt wrote, and the attempt acted on every answer.

        Also once the database the attempt opens as it starts (``Attempt.open_now``, an exclusive handler's) is open:
        the open runs partly in Rust, off the event loop, so its migrations' commit can go out after any number of
        turns, and with it a notification the attempt would not have processed at a shutdown (rule 3).
        """
        while True:
            opening = _database_opens()
            if opening:
                await asyncio.wait(opening)
                continue
            pushed = self._pushed
            await self.handed_off()
            # An answer pushed while the attempt was handed what came before it is not handed off yet: once more.
            if (
                self._pushed == pushed
                and self._frames.empty()
                and self._delivering == 0
                and self._parsed == len(self.sent())
                and not _database_opens()
            ):
                return

    async def until(self, condition: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
        """Return once ``condition`` holds, letting the attempt run; fail after ``timeout``, which only a hang reaches."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError(f"waited {timeout}s for {what}")
            await asyncio.sleep(0.001)

    # --- The fake runtime

    def runtime(self, obj: SessionObject, handler_name: str) -> _FakeRuntime:
        """The invocation's fake runtime, made for ``obj``'s ``handler_name`` on its first attempt."""
        if self.rt is not None:
            return self.rt
        entries = ENTRIES.of(obj)
        assert entries is not None and self._key is not None, "call start() first"
        rt = _FakeRuntime(
            invocation_id=self.invocation_id,
            service=obj.name,
            handler=handler_name,
            key=self._key,
            shared=entries[handler_name].view,
            input=json.dumps(self._input).encode(),
            object_preload=OBJECT_PRELOAD,
        )
        for key, value in self.state.items():
            rt.set_object_state(key, value)
        if self._cancel_at_start:
            # A first attempt that ran nothing is lost; the cancel signal arrives; the next starts cancelled.
            rt.start_attempt()
            rt.lose_attempt()
            rt.cancel()
        self.rt = rt
        return rt

    def _made(self) -> list[Made]:
        if self.rt is None:
            return []
        made: list[Made] = []
        for wire, record in zip(self._wire_calls, self.rt.calls()):
            made.append(
                Made(
                    service=record["service"],
                    handler=record["handler"],
                    key=record["key"],
                    idempotency_key=record["idempotency_key"],
                    input=bytes(record["parameter"]).decode(),
                    invocation_id=record["invocation_id"],
                    is_send=wire.is_send,
                )
            )
        return made

    def _sync_state(self) -> None:
        assert self.rt is not None
        stored = dict(self.rt.object_state())
        self.state.clear()
        self.state.update(stored)

    def _answer_call(self, invocation_id: str) -> None:
        assert self.rt is not None
        record = next(r for r in self.rt.calls() if r["invocation_id"] == invocation_id)
        target = f"{record['service']}/{record['handler']}"
        failure = self._fail.get(target)
        if failure is None:
            self.rt.complete_call(invocation_id, bytes(record["parameter"]))
        else:
            self.rt.complete_call(invocation_id, PyFailure(failure[0], failure[1]))

    def _answer_calls(self) -> None:
        """Answer the calls Restate applied since the last time: echo, fail, hold, or never (key ``k``)."""
        for made in self._made():
            if made.is_send or made.invocation_id in self._settled_calls:
                continue
            self._settled_calls.add(made.invocation_id)
            if made.key == "k":
                continue
            if made.target in self._hold:
                self._held_results.setdefault(made.target, []).append(made.invocation_id)
                continue
            self._answer_call(made.invocation_id)

    def _note_calls(self, m: Message) -> list[_WireCall]:
        """The calls and sends of ``m`` (a call, or a group with some), recorded in the order Restate applies them."""
        found: list[_WireCall] = []
        for member in m.members():
            if member.type != TYPE.call:
                continue
            wire = _wire_call(member)
            again = wire.idempotency_key is not None and any(
                (w.service, w.handler, w.key, w.idempotency_key)
                == (wire.service, wire.handler, wire.key, wire.idempotency_key)
                for w in self._wire_calls
            )
            if not again:
                self._wire_calls.append(wire)
            found.append(wire)
        return found

    def _receive(self, m: Message) -> None:
        """Hand ``m`` to the fake runtime, answer what it applied, and push what it sends."""
        assert self.rt is not None
        try:
            self.rt.receive(m.raw)
        except FakeRuntimeViolation:
            # Kept in ``violations()``; ``run`` fails the test with them.
            pass
        self._answer_calls()
        self._push_output()
        self._sync_state()

    def _asked(self, ask: str) -> None:
        self.asks.append(ask)

    async def _deliver(self, m: Message) -> None:
        """Restate takes ``m`` in: the cancel signal first, if this is where it goes; withheld, if so."""
        assert self.rt is not None
        wire_calls = self._note_calls(m)
        signal_before = False
        ask: str | None = None
        if m.type == TYPE.get_state:
            self.reads += 1
            ask = f"read {m.bytes_of(2).decode()}"
            signal_before = self.reads == self._cancel_on_read
        elif m.type == TYPE.get_state_keys:
            signal_before = self._cancel_on_keys
        elif m.is_commit:
            self._commits += 1
            ask = "ack"
            signal_before = self._commits == self._cancel_on_commit
        call_signal: str | None = None
        if m.type == TYPE.call and not wire_calls[0].is_send:
            self._calls_seen += 1
            target = f"{wire_calls[0].service}/{wire_calls[0].handler}"
            if wire_calls[0].key != "k" and target not in self._hold:
                ask = f"call {target}"
            if self._calls_seen == 1:
                call_signal = self._cancel_on_call
        if ask is not None:
            self._asked(ask)
            if self._cancel_before_answer == len(self.asks):
                signal_before = True
            if self._withhold_answer == len(self.asks):
                # Restate holds it, and what follows, unapplied until the attempt ends.
                self._withheld = True
                self.rt.set_auto_apply(False)
        if signal_before and self.signalled_at is None and not self._withheld:
            await self._signal()
        if call_signal == "before_id" and self.signalled_at is None:
            await self._signal()
        if call_signal == "after_id" and self.signalled_at is None:
            # The invocation id goes out; the signal once the attempt has it; then the answer.
            held = self._hold
            self._hold = held | {f"{wire_calls[0].service}/{wire_calls[0].handler}"}
            self._receive(m)
            self._hold = held
            await self.handed_off()
            await self._signal()
            for target, ids in list(self._held_results.items()):
                if target not in held and ids:
                    self._answer_call(ids.pop(0))
            self._push_output()
            return
        self._receive(m)

    async def _deliver_frames(self) -> None:
        """Restate takes the attempt's messages in, one at a time, in order."""
        while True:
            m = await self._frames.get()
            self._delivering += 1
            try:
                await self._deliver(m)
            finally:
                self._delivering -= 1
                self._frames.task_done()

    def _take_written(self) -> None:
        """Split what the attempt wrote since the last time into messages, for Restate to take in."""
        sent = self.sent()
        for m in sent[self._parsed :]:
            self._frames.put_nowait(m)
        self._parsed = len(sent)

    async def settle(self, timeout: float = 20.0) -> None:
        """Play Restate's side until the attempt closed its response, then tell the fake runtime.

        Between rounds the event loop runs: the database's store waits in Rust, and gets its tick through the VM's
        scheduler.
        """
        assert self.rt is not None
        deadline = time.monotonic() + timeout
        delivering = asyncio.ensure_future(self._deliver_frames())
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.001)
                self._take_written()
                if self._frames.empty() and self._delivering == 0:
                    if self.rt.is_closed():
                        # After the attempt's last message, or a shutdown: Restate closes its side first.
                        self._close_input()
                    if self.ended:
                        break
            else:
                raise AssertionError(f"the attempt did not end within {timeout}s")
        finally:
            delivering.cancel()
        self._take_written()
        while not self._frames.empty():
            self._receive(self._frames.get_nowait())
        try:
            self.rt.sdk_closed()
        except ValueError:
            # The fake already saw the end of a stream it broke.
            pass
        if self._withheld:
            self.rt.set_auto_apply(True)
        self._sync_state()

    def violations(self) -> list[str]:
        return [] if self.rt is None else self.rt.violations()


async def run(obj: SessionObject, handler_name: str, side: RestateSide) -> None:
    """Drive one attempt of ``obj``'s ``handler_name`` through its runner, with ``side`` playing Restate.

    Call ``side.start`` first; this plays Restate's side until the attempt closes its response, and fails on any
    violation of the protocol the fake runtime found.
    """
    entries = ENTRIES.of(obj)
    assert entries is not None
    entry = entries[handler_name]
    handler = obj.handlers[handler_name]
    rt = side.runtime(obj, handler_name)
    side._reset_attempt()  # pylint: disable=protected-access
    side._push(rt.start_attempt())  # pylint: disable=protected-access
    request_headers = rt.request_headers()
    side.channel = ReceiveChannel(side.receive)
    serving = asyncio.ensure_future(
        _runner(entry)(
            handler,
            request_headers=request_headers,
            attempt_headers=dict(request_headers),
            receive=side.channel,
            send=side.send,
            server_headers=[],
        )
    )
    try:
        await side.settle()
    except BaseException:
        serving.cancel()
        raise
    await asyncio.wait_for(serving, 20)
    assert not side.violations(), f"the attempt violated the protocol: {side.violations()}"
