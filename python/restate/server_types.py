#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Node.js/TypeScript,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""This module contains the ASGI types."""

from typing import Awaitable, Callable, Dict, Iterable, List, Tuple, Union
# Scope is a dictionary with string keys and values that can be any type.
Scope = Dict[str, Union[str, List[Tuple[bytes, bytes]]]]

# Message is a dictionary with string keys and values that can be any type.
Message = Dict[str,
               Union[str,
                     int,
                     bytes,
                     bool,
                     Dict[str, str],
                     Dict[bytes, bytes],
                     List[Tuple[bytes, bytes]]]]

# Receive is an asynchronous callable that returns a Message.
Receive = Callable[[], Awaitable[Message]]

# Send is an asynchronous callable that takes a Message as an argument.
Send = Callable[[Message], Awaitable[None]]

# The main ASGI application type is an asynchronous callable that takes a Scope,
# a Receive callable, and a Send callable.
ASGIApp = Callable[[Scope, Receive, Send], None]

def header_to_binary(headers: Iterable[Tuple[str, str]]) -> List[Tuple[bytes, bytes]]:
    """Convert a list of headers to a list of binary headers."""
    return [ (k.encode('utf-8'), v.encode('utf-8')) for k,v in headers ]

def binary_to_header(headers: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[str, str]]:
    """Convert a list of binary headers to a list of headers."""
    return [ (k.decode('utf-8'), v.decode('utf-8')) for k,v in headers ]
