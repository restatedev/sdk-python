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
"""Restate's side of a session object's attempt, for tests: the service protocol on the wire, by hand.

The codec (frames, protobuf fields and the message types) and ``RestateSide``,
which plays Restate for one attempt of a session object's handler on the
explicit recovery VM: it holds the object's state, answers the reads, applies
the writes, acks the commands that asked to be acked, and answers calls. No
endpoint and no server: ``run`` drives the attempt through the object's
``_runner``, over an ASGI ``receive``/``send`` pair, as ``server.py`` does.
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


def field(number: int, wire: int, payload: bytes) -> bytes:
    """A field ``number`` of wire type ``wire`` (0, a varint; 2, length-delimited) with ``payload``."""
    if wire == 0:
        return _varint(number * 8) + payload
    return _varint(number * 8 + 2) + _varint(len(payload)) + payload


def uint(number: int, value: int) -> bytes:
    return field(number, 0, _varint(value))


def bytes_(number: int, value: Union[bytes, str]) -> bytes:
    return field(number, 2, value.encode() if isinstance(value, str) else value)


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


def value(content: Union[bytes, str]) -> bytes:
    """A ``Value`` message: its content is field 1."""
    return bytes_(1, content)


# --- Messages


class TYPE:  # pylint: disable=too-few-public-methods
    """The message types these tests send and read."""

    start = 0x0000
    error = 0x0002
    end = 0x0003
    command_ack = 0x0004
    awaiting_on = 0x0006
    get_state_ephemeral = 0x0200
    get_state_ephemeral_notification = 0x0300
    input = 0x0400
    output = 0x0401
    set_state = 0x0403
    clear_state = 0x0404
    get_state_keys = 0x0406
    call = 0x040D
    one_way_call = 0x040E
    cancel_invocation = 0x0410
    get_state_keys_completion = 0x8006
    call_completion = 0x800D
    call_invocation_id_completion = 0x800E
    signal = 0xFBFF


V8 = "application/vnd.restate.invocation.v8"

REQUESTED_ACK = 0x8000
"""The header bit a command sets to ask Restate to ack it once stored."""


def frame(type_: int, body: bytes) -> bytes:
    """A message: its type, 16 bits of flags (none), its length, and its body."""
    return struct.pack(">HHI", type_, 0, len(body)) + body


@dataclass(frozen=True)
class Message:
    """One message of an output buffer: its type, header flags and decoded fields."""

    type: int
    flags: int
    """The header's 16 bits of flags; ``REQUESTED_ACK`` is one of them."""
    fields: Fields

    def bytes_of(self, number: int) -> bytes:
        found = self.fields[number][0]
        assert isinstance(found, bytes)
        return found

    def int_of(self, number: int) -> int:
        found = self.fields[number][0]
        assert isinstance(found, int)
        return found


def messages(output: bytes) -> list[Message]:
    out: list[Message] = []
    at = 0
    while at < len(output):
        type_, flags, length = struct.unpack_from(">HHI", output, at)
        out.append(Message(type_, flags, decode(output[at + 8 : at + 8 + length])))
        at += 8 + length
    return out


def outcome(sent: list[Message]) -> Optional[Union[tuple[Literal["value"], Any], tuple[Literal["failure"], int, str]]]:
    """How the invocation ended: ``("value", v)`` with its output deserialized, ``("failure", code, message)``, or
    ``None`` if the attempt wrote no output (it was shut down)."""
    output = next((m for m in sent if m.type == TYPE.output), None)
    if output is None:
        return None
    if 14 in output.fields:
        content = decode(output.bytes_of(14)).get(1, [b""])[0]
        assert isinstance(content, bytes)
        return ("value", json.loads(content))
    failure = decode(output.bytes_of(15))
    code = failure[1][0]
    message = failure.get(2, [b""])[0]
    assert isinstance(code, int) and isinstance(message, bytes)
    return ("failure", code, message.decode())


def commits(sent: list[Message]) -> int:
    """How many of ``sent``'s commands asked for an ack: a commit each, on either store."""
    return sum(1 for m in sent if 0x0400 <= m.type < 0x8000 and m.flags & REQUESTED_ACK)


def output_of(sent: list[Message]) -> Any:
    """The handler's output, deserialized from the attempt's output message."""
    output = next(m for m in sent if m.type == TYPE.output)
    answered = decode(output.bytes_of(14))
    content = answered[1][0]
    assert isinstance(content, bytes)
    return json.loads(content)


@dataclass(frozen=True)
class Made:
    """A call or a send the attempt made, as it went on the wire."""

    service: str
    handler: str
    key: str
    idempotency_key: Optional[str]
    input: str
    """The input, as the text of its bytes."""
    invocation_id: Optional[str] = None
    """The invocation id Restate gave a call; a send's is never answered here."""

    @property
    def target(self) -> str:
        """``Service/handler``."""
        return f"{self.service}/{self.handler}"


def _made(message: Message, key_field: int, idempotency_field: int, invocation_id: Optional[str] = None) -> Made:
    def text(number: int) -> Optional[str]:
        found = message.fields.get(number)
        return None if found is None else typing.cast(bytes, found[0]).decode()

    return Made(
        service=text(1) or "",
        handler=text(2) or "",
        key=text(key_field) or "",
        idempotency_key=text(idempotency_field),
        input=text(3) or "",
        invocation_id=invocation_id,
    )


CANCEL_SIGNAL = frame(TYPE.signal, uint(2, 1) + bytes_(4, b""))
"""Restate cancelling the invocation: the built-in signal 1, with an empty (void) value."""

HAND_OFF_TURNS = 10
"""Turns of the event loop that a message takes from the request body to the driver's tick, with margin.

The ``ReceiveChannel``'s loop takes it, the input pump hands it to the VM and
schedules a tick, and the tick acts on it: three turns.
"""


# --- Restate's side


class RestateSide:
    """Restate's side of one attempt: it holds the object's state, answers the reads, applies the writes, and acks.

    Args:
        state: The object's state, which the attempt reads and writes. The
            start carries the head of the database alone, as Restate preloads
            it for a session object, and the attempt reads the rest on the wire.
        cancel_on_commit: The first time a command asks for an ack (``True``),
            or the ``n``-th time (from 1), Restate sends the cancel signal,
            and the ack once the attempt has acted on the signal: a
            cancellation that lands with the database waiting on Restate.
        cancel_on_read: At the ``n``-th read of the state on the wire, Restate
            sends the cancel signal, and the read's value once the attempt has
            acted on the signal.
        cancel_on_keys: At the first listing of the state's keys, Restate
            sends the cancel signal, and the listing once the attempt has
            acted on the signal.
        cancel_on_call: At the first call, Restate sends the cancel signal
            ``"before_id"``, the call's invocation id once the attempt has
            acted on it; or ``"after_id"``, the signal once the attempt has
            the id.
        cancel_at_start: A retry of a cancelled invocation: the start counts
            two known entries, and the signal follows the input, so the VM
            knows of the cancellation before the handler starts.
        cancel_before_answer: Just before Restate answers the ``n``-th thing
            the attempt asks for (from 1; see ``asks``), it sends the cancel
            signal, and the answer once the attempt has acted on the signal.
        withhold_answer: The ``n``-th thing the attempt asks for (from 1) is
            never answered: a test shuts the attempt down meanwhile.
        hold: Calls to these targets (``Service/handler``) get their
            invocation id, and their result only once ``release``d.
        fail: Calls to these targets fail with this terminal failure,
            ``(code, message)``.
        id_per_call: Give each call an invocation id of its own (``inv_<n>``)
            rather than ``inv_started`` for all.

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
        id_per_call: bool = False,
    ) -> None:
        self.state: dict[str, bytes] = {} if state is None else state
        # The ack (from 1) that the signal goes in with, or 0 for none.
        self._cancel_on_commit = int(cancel_on_commit)
        self._cancel_before_answer = cancel_before_answer
        self._withhold_answer = withhold_answer
        self._hold = set(hold or [])
        self._fail = dict(fail or {})
        self._id_per_call = id_per_call
        # The results withheld, by target, oldest first: (the result's completion id, the input echoed).
        self._held_results: dict[str, list[tuple[int, bytes]]] = {}
        self._acks = 0
        self.asks: list[str] = []
        """What the attempt asked Restate for, in order, which Restate answers: ``read <key>``, ``ack``, or ``call
        <Service/handler>`` (a call that is neither held nor to key ``k``)."""
        self.calls: list[Made] = []
        """The calls the attempt made, in order, with the invocation id each got."""
        self.sends: list[Made] = []
        """The sends the attempt made, in order."""
        self.cancels: list[str] = []
        """The invocation ids the attempt sent a cancellation to, in order."""
        self._cancel_on_read = cancel_on_read
        self._cancel_on_keys = cancel_on_keys
        self._cancel_on_call = cancel_on_call
        self._cancel_at_start = cancel_at_start
        self._inbound: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self.head: dict[str, Any] | None = None
        self.ended = False
        self.reads = 0
        """How many reads of the state the attempt made on the wire."""
        self.signalled_at: int | None = None
        """How many messages the attempt had written when the cancel signal went in, if it did."""
        # The journal index of the last command seen. Restate numbers *every* command, so an ack
        # must count the calls too, not only the writes. The input is the first, at index 0.
        self._index = 0
        # How many of the attempt's messages have been answered.
        self._answered = 0
        # What is pushed after a message that the attempt must act on first (``_push_alone``),
        # until it has.
        self._held: list[bytes] | None = None
        self._releasing: asyncio.Task[None] | None = None

    # --- The ASGI pair the attempt runs over

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
        if self._held is not None:
            self._held.append(chunk)
            return
        self._inbound.put_nowait({"type": "http.request", "body": chunk, "more_body": True})

    def _push_alone(self, chunk: bytes) -> None:
        """Push ``chunk``, and hold back what is pushed after it until the attempt has acted on it."""
        self._push(chunk)
        if self._held is None:
            self._held = []
            self._releasing = asyncio.ensure_future(self._release())

    async def _release(self) -> None:
        await self.handed_off()
        held, self._held = self._held or [], None
        for chunk in held:
            self._push(chunk)

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

    def _signal(self) -> None:
        """Send the cancel signal, and hold back what follows until the attempt has acted on it."""
        self.signalled_at = len(self.sent())
        self._push_alone(CANCEL_SIGNAL)

    def _close(self) -> None:
        self._inbound.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        # Ends the receive channel's loop, which reads on after the body ends.
        self._inbound.put_nowait({"type": "http.disconnect"})

    # --- What tests call

    def sent(self) -> list[Message]:
        """Everything the attempt wrote, as messages."""
        return messages(b"".join(self._chunks))

    def start(self, key: str, input_: Any) -> None:
        """Start the invocation for ``key`` with ``input_``."""
        preloaded = {k: v for k, v in self.state.items() if k == HEAD_KEY}
        entries = b"".join(field(4, 2, bytes_(1, k) + bytes_(2, v)) for k, v in preloaded.items())
        self._push(
            frame(
                TYPE.start,
                bytes_(1, bytes([1, 2, 3, 4]))
                + bytes_(2, "inv_test")
                # Known entries: the input, and for a retry of a cancelled invocation the signal the
                # earlier attempt got. The VM is not ready to execute until it has them.
                + uint(3, 2 if self._cancel_at_start else 1)
                + entries
                # Partial state: a key missing from the entries is read from Restate.
                + uint(5, 1)
                + bytes_(6, key),
            )
        )
        self._push(frame(TYPE.input, bytes_(14, value(json.dumps(input_)))))
        # A retry of a cancelled invocation: the signal is among the entries of the earlier
        # attempts, so the VM knows before the handler starts.
        if self._cancel_at_start:
            self._push(CANCEL_SIGNAL)

    async def cancel(self) -> None:
        """Restate cancels the invocation now; return once the attempt has acted on it."""
        self._signal()
        await self.handed_off()

    def shut_down(self) -> None:
        """Restate closes the request body, as its inactivity timeout does: the attempt is shut down."""
        self._close()

    def release(self, target: str) -> None:
        """Answer the oldest withheld call to ``target`` (``Service/handler``), echoing its input."""
        withheld = self._held_results.get(target)
        if not withheld:
            raise AssertionError(f"no call to {target} is withheld")
        result, echoed = withheld.pop(0)
        self._push(frame(TYPE.call_completion, uint(1, result) + field(5, 2, value(echoed))))

    def has_called(self, target: str) -> bool:
        """Whether the attempt has called ``target`` (``Service/handler``), as far as Restate has taken it in."""
        return any(c.target == target for c in self.calls)

    async def until(self, condition: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
        """Return once ``condition`` holds, letting the attempt run; fail after ``timeout``, which only a hang reaches."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError(f"waited {timeout}s for {what}")
            await asyncio.sleep(0.001)

    def _asked(self, ask: str) -> bool:
        """Before Restate answers ``ask``: the signal, if this ask is the one; whether to answer it.

        The answer pushed after the signal is held until the attempt has acted on the signal (``_signal``).
        """
        self.asks.append(ask)
        if self._withhold_answer == len(self.asks):
            return False
        if self._cancel_before_answer == len(self.asks) and self.signalled_at is None:
            self._signal()
        return True

    def _over(self, sent: list[Message]) -> bool:
        return any(m.type in (TYPE.end, TYPE.error) for m in sent)

    async def settle(self, timeout: float = 20.0) -> None:
        """Answer what the attempt asks for until it ends, then close the request body.

        Closing it earlier is what Restate's inactivity timeout does, and would shut the attempt down.
        Between rounds the event loop runs: the database's store waits in Rust, and gets its tick
        through the VM's scheduler.
        """
        deadline = time.monotonic() + timeout
        while not self.ended and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
            sent = self.sent()
            for message in sent[self._answered :]:
                self._answered += 1
                self._answer(message)
            # The attempt is over once it has said so; nothing more will come.
            if self._over(sent):
                break
        if not self.ended and not self._over(self.sent()):
            raise AssertionError(f"the attempt did not end within {timeout}s")
        self._close()

    def _answer(self, message: Message) -> None:
        # Commands are journaled and numbered; ephemeral commands (0x02xx) and notifications
        # (0x8xxx) are not.
        if 0x0400 <= message.type < 0x8000:
            self._index += 1
            # Restate acks a command only if it asked to be acked, and the ack of one covers every
            # command before it.
            if message.flags & REQUESTED_ACK:
                self._acks += 1
                # A withheld ack still stores the command, below.
                if self._asked("ack"):
                    # Restate signals a cancellation once, not on every commit: what follows is
                    # acked as usual.
                    if self._acks == self._cancel_on_commit and self.signalled_at is None:
                        self._signal()
                    self._push(frame(TYPE.command_ack, uint(1, self._index)))
        if message.type == TYPE.get_state_ephemeral:
            self.reads += 1
            if self.reads == self._cancel_on_read and self.signalled_at is None:
                self._signal()
            key = message.bytes_of(2).decode()
            if not self._asked(f"read {key}"):
                return
            held = self.state.get(key)
            self._push(
                frame(
                    TYPE.get_state_ephemeral_notification,
                    uint(1, message.int_of(1)) + (field(4, 2, b"") if held is None else field(5, 2, value(held))),
                )
            )
        elif message.type == TYPE.set_state:
            key = message.bytes_of(1).decode()
            wrapped = decode(message.bytes_of(3))
            content = wrapped[1][0] if 1 in wrapped else b""
            assert isinstance(content, bytes)
            self.state[key] = content
        elif message.type == TYPE.clear_state:
            self.state.pop(message.bytes_of(1).decode(), None)
        elif message.type == TYPE.one_way_call:
            self.sends.append(_made(message, 6, 7))
        elif message.type == TYPE.cancel_invocation:
            self.cancels.append(message.bytes_of(1).decode())
        elif message.type == TYPE.call:
            id_ = f"inv_{len(self.calls) + 1}" if self._id_per_call else "inv_started"
            made = _made(message, 5, 6, id_)
            self.calls.append(made)
            invocation_id = frame(TYPE.call_invocation_id_completion, uint(1, message.int_of(10)) + bytes_(16, id_))
            if self._cancel_on_call is not None and self.signalled_at is None:
                if self._cancel_on_call == "before_id":
                    self._signal()
                self._push_alone(invocation_id)
                if self._cancel_on_call == "after_id":
                    self._signal()
            else:
                self._push(invocation_id)
            result = message.int_of(11)
            echoed = typing.cast(bytes, message.fields.get(3, [b""])[0])
            if made.key == "k":
                return
            if made.target in self._hold:
                self._held_results.setdefault(made.target, []).append((result, echoed))
                return
            if not self._asked(f"call {made.target}"):
                return
            failure = self._fail.get(made.target)
            if failure is None:
                self._push(frame(TYPE.call_completion, uint(1, result) + field(5, 2, value(echoed))))
            else:
                code, text = failure
                self._push(frame(TYPE.call_completion, uint(1, result) + field(6, 2, uint(1, code) + bytes_(2, text))))
        elif message.type == TYPE.get_state_keys:
            if self._cancel_on_keys and self.signalled_at is None:
                self._signal()
            keys = b"".join(bytes_(1, k) for k in self.state)
            self._push(frame(TYPE.get_state_keys_completion, uint(1, message.int_of(11)) + field(17, 2, keys)))


async def run(obj: SessionObject, handler_name: str, side: RestateSide) -> None:
    """Drive one attempt of ``obj``'s ``handler_name`` through its runner, with ``side`` playing Restate.

    Call ``side.start`` first; this answers the attempt until it ends.
    """
    entries = ENTRIES.of(obj)
    assert entries is not None
    entry = entries[handler_name]
    handler = obj.handlers[handler_name]
    request_headers = [("content-type", V8)]
    serving = asyncio.ensure_future(
        _runner(entry)(
            handler,
            request_headers=request_headers,
            attempt_headers=dict(request_headers),
            receive=ReceiveChannel(side.receive),
            send=side.send,
            bidirectional=True,
            server_headers=[],
        )
    )
    try:
        await side.settle()
    except BaseException:
        serving.cancel()
        raise
    await asyncio.wait_for(serving, 20)
