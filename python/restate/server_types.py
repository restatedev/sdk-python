#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
This module contains the ASGI types definitions.

:see: https://github.com/django/asgiref/blob/main/asgiref/typing.py
"""

import asyncio
from typing import (Awaitable, Callable, Dict, Iterable, List,
                    Tuple, Union, TypedDict, Literal, Optional, NotRequired, Any)

class ASGIVersions(TypedDict):
    """ASGI Versions"""
    spec_version: str
    version: Union[Literal["2.0"], Literal["3.0"]]

class Scope(TypedDict):
    """ASGI Scope"""
    type: Literal["http"]
    asgi: ASGIVersions
    http_version: str
    method: str
    scheme: str
    path: str
    raw_path: bytes
    query_string: bytes
    root_path: str
    headers: Iterable[Tuple[bytes, bytes]]
    client: Optional[Tuple[str, int]]
    server: Optional[Tuple[str, Optional[int]]]
    state: NotRequired[Dict[str, Any]]
    extensions: Optional[Dict[str, Dict[object, object]]]

class RestateEvent(TypedDict):
    """An event that represents a run completion"""
    type: Literal["restate.run_completed"]
    data: Optional[Dict[str, Any]]

class HTTPRequestEvent(TypedDict):
    """ASGI Request event"""
    type: Literal["http.request"]
    body: bytes
    more_body: bool

class HTTPResponseStartEvent(TypedDict):
    """ASGI Response start event"""
    type: Literal["http.response.start"]
    status: int
    headers: Iterable[Tuple[bytes, bytes]]
    trailers: bool

class HTTPResponseBodyEvent(TypedDict):
    """ASGI Response body event"""
    type: Literal["http.response.body"]
    body: bytes
    more_body: bool


ASGIReceiveEvent = HTTPRequestEvent


ASGISendEvent = Union[
    HTTPResponseStartEvent,
    HTTPResponseBodyEvent
]

Receive = Callable[[], Awaitable[ASGIReceiveEvent]]
Send = Callable[[ASGISendEvent], Awaitable[None]]

ASGIApp = Callable[
    [
        Scope,
        Receive,
        Send,
    ],
    Awaitable[None],
]

def header_to_binary(headers: Iterable[Tuple[str, str]]) -> List[Tuple[bytes, bytes]]:
    """Convert a list of headers to a list of binary headers."""
    return [ (k.encode('utf-8'), v.encode('utf-8')) for k,v in headers ]

def binary_to_header(headers: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[str, str]]:
    """Convert a list of binary headers to a list of headers."""
    return [ (k.decode('utf-8'), v.decode('utf-8')) for k,v in headers ]

class ReceiveChannel:
    """ASGI receive channel."""

    def __init__(self, receive: Receive) -> None:
        self._queue = asyncio.Queue[Union[ASGIReceiveEvent, RestateEvent]]()
        self._http_input_closed = asyncio.Event()
        self._disconnected = asyncio.Event()

        async def loop():
            """Receive loop."""
            while not self._disconnected.is_set():
                event = await receive()
                if event.get('type') == 'http.request' and not event.get('more_body', False):
                    self._http_input_closed.set()
                elif event.get('type') == 'http.disconnect':
                    self._http_input_closed.set()
                    self._disconnected.set()
                await self._queue.put(event)

        self._task = asyncio.create_task(loop())

    async def __call__(self) -> ASGIReceiveEvent | RestateEvent:
        """Get the next message."""
        what = await self._queue.get()
        self._queue.task_done()
        return what

    async def block_until_http_input_closed(self) -> None:
        """Wait until the HTTP input is closed"""
        await self._http_input_closed.wait()

    async def enqueue_restate_event(self, what: RestateEvent):
        """Add a message."""
        await self._queue.put(what)

    async def close(self):
        """Close the channel."""
        self._http_input_closed.set()
        self._disconnected.set()
        if self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
