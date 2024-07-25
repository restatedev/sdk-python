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


ASGIReceiveEvent = Union[
    HTTPRequestEvent
]


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
