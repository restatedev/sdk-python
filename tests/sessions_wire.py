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
from dataclasses import dataclass
from typing import Any, Union

from restate.server_types import ASGIReceiveEvent, ASGISendEvent, ReceiveChannel
from restate.sessions import SessionObject
from restate.sessions._object import ENTRIES, _runner

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
    suspension = 0x0001
    error = 0x0002
    end = 0x0003
    command_ack = 0x0004
    get_state_ephemeral = 0x0200
    get_state_ephemeral_notification = 0x0300
    input = 0x0400
    output = 0x0401
    set_state = 0x0403
    clear_state = 0x0404
    get_state_keys = 0x0406
    call = 0x040D
    one_way_call = 0x040E
    run = 0x0411
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


def output_of(sent: list[Message]) -> Any:
    """The handler's output, deserialized from the attempt's output message."""
    output = next(m for m in sent if m.type == TYPE.output)
    answered = decode(output.bytes_of(14))
    content = answered[1][0]
    assert isinstance(content, bytes)
    return json.loads(content)


CANCEL_SIGNAL = frame(TYPE.signal, uint(2, 1) + bytes_(4, b""))
"""Restate cancelling the invocation: the built-in signal 1, with an empty (void) value."""


# --- Restate's side


class RestateSide:
    """Restate's side of one attempt: it holds the object's state, answers the reads, applies the writes, and acks.

    Args:
        state: The object's state, which the attempt reads and writes.
        cancel_on_commit: The first time a command asks for an ack, Restate
            sends the cancel signal and, a moment later, the ack: a
            cancellation that lands with the database waiting on Restate.
        cancel_at_start: A retry of a cancelled invocation: the start counts
            two known entries, and the signal follows the input, so the VM
            knows of the cancellation before the handler starts.
    """

    def __init__(
        self,
        state: dict[str, bytes] | None = None,
        *,
        cancel_on_commit: bool = False,
        cancel_at_start: bool = False,
    ) -> None:
        self.state: dict[str, bytes] = {} if state is None else state
        self._cancel_on_commit = cancel_on_commit
        self._cancel_at_start = cancel_at_start
        self._inbound: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self.head: dict[str, Any] | None = None
        self.ended = False
        # The journal index of the last command seen. Restate numbers *every* command, so an ack
        # must count the calls too, not only the writes. The input is the first, at index 0.
        self._index = 0
        # How many of the attempt's messages have been answered.
        self._answered = 0
        # Whether the one cancel signal has been sent (``cancel_on_commit``).
        self._signalled = False

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
        self._inbound.put_nowait({"type": "http.request", "body": chunk, "more_body": True})

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
        entries = b"".join(field(4, 2, bytes_(1, k) + bytes_(2, v)) for k, v in self.state.items())
        self._push(
            frame(
                TYPE.start,
                bytes_(1, bytes([1, 2, 3, 4]))
                + bytes_(2, "inv_test")
                # Known entries: the input, and for a retry of a cancelled invocation the signal the
                # earlier attempt got. The VM is not ready to execute until it has them.
                + uint(3, 2 if self._cancel_at_start else 1)
                + entries
                + bytes_(6, key),
            )
        )
        self._push(frame(TYPE.input, bytes_(14, value(json.dumps(input_)))))
        # A retry of a cancelled invocation: the signal is among the entries of the earlier
        # attempts, so the VM knows before the handler starts.
        if self._cancel_at_start:
            self._push(CANCEL_SIGNAL)

    def cancel(self) -> None:
        """Restate cancels the invocation now."""
        self._push(CANCEL_SIGNAL)

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
                # Restate signals a cancellation once, not on every commit: what follows is acked
                # as usual.
                ack = frame(TYPE.command_ack, uint(1, self._index))
                if self._cancel_on_commit and not self._signalled:
                    self._signalled = True
                    self._push(CANCEL_SIGNAL)
                    # The ack follows after a moment: together, the attempt would read the ack first
                    # and the cancellation would find the commit done, with nothing waiting.
                    asyncio.get_running_loop().call_later(0.02, self._push, ack)
                else:
                    self._push(ack)
        if message.type == TYPE.get_state_ephemeral:
            key = message.bytes_of(2).decode()
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
        elif message.type == TYPE.call:
            self._push(
                frame(TYPE.call_invocation_id_completion, uint(1, message.int_of(10)) + bytes_(16, "inv_started"))
            )
            # A call to key ``k`` is never answered: what a started call, whose result nobody
            # holds, looks like to the handler.
            if message.fields.get(5) != [b"k"]:
                echoed = message.bytes_of(3)
                self._push(frame(TYPE.call_completion, uint(1, message.int_of(11)) + field(5, 2, value(echoed))))
        elif message.type == TYPE.get_state_keys:
            keys = b"".join(bytes_(1, k) for k in self.state)
            self._push(frame(TYPE.get_state_keys_completion, uint(1, message.int_of(11)) + field(5, 2, keys)))


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
