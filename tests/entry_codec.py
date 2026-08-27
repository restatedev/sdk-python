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

"""Tests for the JournalValueCodec feature."""

import typing
from contextlib import asynccontextmanager

import pytest

import restate
from restate import Context, JournalValueCodec, Service, TerminalError
from restate.client import Client
from restate.handler import handler_from_callable, invoke_handler
from restate.serde import JsonSerde

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


# ----- Codec used across the tests

_MAGIC = b"\xca"


class MagicCodec(JournalValueCodec):
    """A tiny symmetric codec: prepends a magic byte on encode, strips+validates it on decode.

    Non-trivial enough that, if the codec were NOT applied on both sides, decoding would fail —
    which lets us prove the codec actually runs. Also exercises the empty-buffer contract:
    ``encode(b"")`` yields the (non-empty) magic byte, and ``decode`` strips it back to ``b""``.
    """

    def encode(self, buf: bytes) -> bytes:
        return _MAGIC + buf

    async def decode(self, buf: bytes) -> bytes:
        if not buf.startswith(_MAGIC):
            raise ValueError("missing magic prefix")
        return buf[len(_MAGIC) :]


# ============================================================
# Fast, docker-free unit tests of the encode/decode seams
# ============================================================


async def test_codec_roundtrip_and_empty_buffer():
    codec = MagicCodec()
    assert await codec.decode(codec.encode(b"hello")) == b"hello"
    # empty buffer must be handled gracefully
    assert await codec.decode(codec.encode(b"")) == b""


async def test_invoke_handler_decodes_input_and_encodes_output():
    svc = Service("greeter")

    @svc.handler()
    async def greet(ctx: Context, name: str) -> str:  # pylint: disable=unused-argument
        return f"hi {name}"

    handler = handler_from_callable(greet)
    codec = MagicCodec()

    # The buffer handed to invoke_handler is what the VM stores: the codec-encoded input.
    encoded_input = codec.encode(handler.handler_io.input_serde.serialize("bob"))

    out = await invoke_handler(handler=handler, ctx=None, in_buffer=encoded_input, journal_codec=codec)

    # Output must be codec-encoded; decoding it back must yield the serialized "hi bob".
    assert out.startswith(_MAGIC)
    decoded_out = await codec.decode(out)
    assert handler.handler_io.output_serde.deserialize(decoded_out) == "hi bob"


async def test_invoke_handler_bad_input_raises_terminal_400():
    svc = Service("greeter")

    @svc.handler()
    async def greet(ctx: Context, name: str) -> str:  # pylint: disable=unused-argument
        return f"hi {name}"

    handler = handler_from_callable(greet)
    codec = MagicCodec()

    # Raw (unencoded) input has no magic prefix -> decode must fail as a terminal 400.
    raw_input = handler.handler_io.input_serde.serialize("bob")
    with pytest.raises(TerminalError) as exc:
        await invoke_handler(handler=handler, ctx=None, in_buffer=raw_input, journal_codec=codec)
    assert exc.value.status_code == 400


class _RecordingClient(Client):
    """A Client that records the last request content and returns a canned response body."""

    def __init__(self, journal_codec, canned_response: bytes):
        super().__init__(client=None, journal_codec=journal_codec)  # type: ignore[arg-type]
        self.last_content: typing.Optional[bytes] = None
        self.canned_response = canned_response

    async def post(self, /, service, handler, send, content, **kwargs):  # type: ignore[override]
        self.last_content = content
        return self.canned_response


async def test_client_encodes_request_and_decodes_response():
    codec = MagicCodec()
    # Server would have stored an encoded success value; simulate that as the response body.
    response_body = codec.encode(JsonSerde[str]().serialize("pong"))
    client = _RecordingClient(codec, response_body)

    result: str = await client.do_raw_call(
        service="s",
        handler="h",
        input_param="ping",
        input_serde=JsonSerde[str](),
        output_serde=JsonSerde[str](),
    )

    # Request body was encoded by the codec...
    assert client.last_content is not None and client.last_content.startswith(_MAGIC)
    assert await codec.decode(client.last_content) == JsonSerde[str]().serialize("ping")
    # ...and the response was decoded before deserialization.
    assert result == "pong"


async def test_client_send_skips_response_decode():
    codec = MagicCodec()
    # A send returns the invocation-id envelope (plain JSON), which must NOT be codec-decoded.
    envelope = JsonSerde[dict]().serialize({"invocationId": "inv_123"})
    client = _RecordingClient(codec, envelope)

    result: dict = await client.do_raw_call(
        service="s",
        handler="h",
        input_param="ping",
        input_serde=JsonSerde[str](),
        output_serde=JsonSerde[dict](),
        send=True,
    )

    # Request is still encoded, but the response envelope is returned verbatim (no decode attempted).
    assert client.last_content is not None and client.last_content.startswith(_MAGIC)
    assert result == {"invocationId": "inv_123"}


# ============================================================
# End-to-end test against a real restate-server (needs docker)
# ============================================================


@asynccontextmanager
async def codec_harness(
    service: typing.Union[Service, restate.VirtualObject, restate.Workflow], codec: JournalValueCodec
) -> typing.AsyncIterator[restate.RestateClient]:
    """Spin up a harness where BOTH the endpoint and the ingress client share the same codec."""
    async with restate.create_test_harness(
        restate.app([service], journal_value_codec=codec),
        journal_value_codec=codec,
        restate_image="ghcr.io/restatedev/restate:latest",
    ) as harness:
        yield harness.client


async def test_codec_end_to_end():
    codec = MagicCodec()
    obj = restate.VirtualObject("codec_obj")

    @obj.handler()
    async def exercise(ctx: restate.ObjectContext, name: str) -> str:
        # state set + get round-trips through the codec
        ctx.set("who", name)
        stored = await ctx.get("who", type_hint=str)
        assert stored == name

        # ctx.run success result round-trips through the codec
        ran = await ctx.run_typed("compute", lambda: name.upper())
        assert ran == name.upper()

        # awakeable resolve + await round-trips through the codec
        awk_id, awk = ctx.awakeable(type_hint=str)
        ctx.resolve_awakeable(awk_id, "signal-value")
        assert await awk == "signal-value"

        return f"hi {name}"

    async with codec_harness(obj, codec) as client:
        # handler input + output round-trip through the codec on both client and server
        result = await client.object_call(exercise, key="k1", arg="bob")
        assert result == "hi bob"


async def test_codec_end_to_end_with_async_provider():
    codec = MagicCodec()
    provider_calls = 0

    async def provider() -> JournalValueCodec:
        nonlocal provider_calls
        provider_calls += 1
        return codec

    svc = Service("codec_svc")

    @svc.handler()
    async def greet(ctx: Context, name: str) -> str:  # pylint: disable=unused-argument
        return f"hi {name}"

    # The endpoint is configured with an ASYNC PROVIDER; the ingress client gets the built instance.
    async with restate.create_test_harness(
        restate.app([svc], journal_value_codec=provider),
        journal_value_codec=codec,
        restate_image="ghcr.io/restatedev/restate:latest",
    ) as harness:
        assert await harness.client.service_call(greet, arg="bob") == "hi bob"
        assert await harness.client.service_call(greet, arg="alice") == "hi alice"

    # The provider must have been resolved exactly once across multiple invocations.
    assert provider_calls == 1
