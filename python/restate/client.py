#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
This is a basic remote client for the Restate service.
"""

from datetime import timedelta
import httpx
import typing
from contextlib import asynccontextmanager

from .client_types import RestateClient, RestateClientSendHandle

from .context import HandlerType
from .serde import BytesSerde, JsonSerde, Serde
from .handler import handler_from_callable

I = typing.TypeVar("I")
O = typing.TypeVar("O")


class Client(RestateClient):
    """
    A basic client for connecting to the Restate service.
    """

    def __init__(self, client: httpx.AsyncClient, headers: typing.Optional[dict] = None):
        self.headers = headers or {}
        self.client = client

    async def do_call(
        self,
        tpe: HandlerType[I, O],
        parameter: I,
        key: typing.Optional[str] = None,
        send_delay: typing.Optional[timedelta] = None,
        send: bool = False,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
        force_json_output: bool = False,
    ) -> O:
        """Make an RPC call to the given handler"""
        target_handler = handler_from_callable(tpe)
        input_serde = target_handler.handler_io.input_serde
        input_type = target_handler.handler_io.input_type

        if input_type is not None and input_type.is_void:
            content_type = None
        else:
            content_type = target_handler.handler_io.content_type

        if headers is None:
            headers = {}
        if headers.get("Content-Type") is None and content_type is not None:
            headers["Content-Type"] = content_type

        service = target_handler.service_tag.name
        handler = target_handler.name
        output_serde = target_handler.handler_io.output_serde if force_json_output is False else JsonSerde()

        return await self.do_raw_call(
            service=service,
            handler=handler,
            input_param=parameter,
            input_serde=input_serde,
            output_serde=output_serde,
            key=key,
            send_delay=send_delay,
            send=send,
            idempotency_key=idempotency_key,
            headers=headers,
        )

    async def do_raw_call(
        self,
        service: str,
        handler: str,
        input_param: I,
        input_serde: Serde[I],
        output_serde: Serde[O],
        key: typing.Optional[str] = None,
        send_delay: typing.Optional[timedelta] = None,
        send: bool = False,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> O:
        """Make an RPC call to the given handler"""
        parameter = input_serde.serialize(input_param)
        if headers is not None:
            headers_kvs = list(headers.items())
        else:
            headers_kvs = []
        if send_delay is not None:
            ms = int(send_delay.total_seconds() * 1000)
        else:
            ms = None

        res = await self.post(
            service=service,
            handler=handler,
            send=send,
            content=parameter,
            headers=headers_kvs,
            key=key,
            delay=ms,
            idempotency_key=idempotency_key,
        )
        return output_serde.deserialize(res)  # type: ignore

    async def post(
        self,
        /,
        service: str,
        handler: str,
        send: bool,
        content: bytes,
        headers: typing.List[typing.Tuple[(str, str)]] | None = None,
        key: str | None = None,
        delay: int | None = None,
        idempotency_key: str | None = None,
    ) -> bytes:
        """
        Send a POST request to the Restate service.
        """
        endpoint = service
        if key:
            endpoint += f"/{key}"
        endpoint += f"/{handler}"
        if send:
            endpoint += "/send"
            if delay is not None:
                endpoint = endpoint + f"?delay={delay}"
        dict_headers = dict(headers) if headers is not None else {}
        if idempotency_key is not None:
            dict_headers["Idempotency-Key"] = idempotency_key
        res = await self.client.post(endpoint, headers=dict_headers, content=content)
        res.raise_for_status()
        return res.content

    @typing.final
    @typing.override
    async def service_call(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> O:
        coro = await self.do_call(tpe, arg, idempotency_key=idempotency_key, headers=headers)
        return coro

    @typing.final
    @typing.override
    async def service_send(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        send_delay: typing.Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateClientSendHandle:
        send_handle = await self.do_call(
            tpe,
            parameter=arg,
            send=True,
            send_delay=send_delay,
            idempotency_key=idempotency_key,
            headers=headers,
            force_json_output=True,
        )

        send = typing.cast(typing.Dict[str, str], send_handle)

        return RestateClientSendHandle(send.get("invocationId", ""), 200)  # TODO: verify

    @typing.final
    @typing.override
    async def object_call(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> O:
        coro = await self.do_call(tpe, arg, key, idempotency_key=idempotency_key, headers=headers)
        return coro

    @typing.final
    @typing.override
    async def object_send(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        send_delay: typing.Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateClientSendHandle:
        send_handle = await self.do_call(
            tpe,
            parameter=arg,
            key=key,
            send=True,
            send_delay=send_delay,
            idempotency_key=idempotency_key,
            headers=headers,
            force_json_output=True,
        )

        send = typing.cast(typing.Dict[str, str], send_handle)

        return RestateClientSendHandle(send.get("invocationId", ""), 200)  # TODO: verify

    @typing.final
    @typing.override
    async def workflow_call(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> O:
        return await self.object_call(tpe, key, arg, idempotency_key=idempotency_key, headers=headers)

    @typing.final
    @typing.override
    async def workflow_send(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        send_delay: typing.Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateClientSendHandle:
        return await self.object_send(
            tpe,
            key,
            arg,
            send_delay=send_delay,
            idempotency_key=idempotency_key,
            headers=headers,
        )

    @typing.final
    @typing.override
    async def generic_call(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> bytes:
        serde = BytesSerde()
        call_handle = await self.do_raw_call(
            service=service,
            handler=handler,
            input_param=arg,
            input_serde=serde,
            output_serde=serde,
            key=key,
            idempotency_key=idempotency_key,
            headers=headers,
        )
        return call_handle

    @typing.final
    @typing.override
    async def generic_send(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        send_delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateClientSendHandle:
        serde = BytesSerde()
        output_serde: Serde[dict] = JsonSerde()

        send_handle_json = await self.do_raw_call(
            service=service,
            handler=handler,
            input_param=arg,
            input_serde=serde,
            output_serde=output_serde,
            key=key,
            send_delay=send_delay,
            send=True,
            idempotency_key=idempotency_key,
            headers=headers,
        )

        return RestateClientSendHandle(send_handle_json.get("invocationId", ""), 200)  # TODO: verify


@asynccontextmanager
async def create_client(ingress: str, headers: typing.Optional[dict] = None) -> typing.AsyncGenerator[RestateClient]:
    """
    Create a new Restate client.
    """
    async with httpx.AsyncClient(base_url=ingress, headers=headers) as http_client:
        yield Client(http_client, headers)
