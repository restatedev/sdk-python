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
""" This module contains functions for serializing and deserializing data. """

import json
import typing

from restate.context import Serde

I = typing.TypeVar('I')
O = typing.TypeVar('O')

# disable to few parameters
# pylint: disable=R0903

class SerializerType(typing.Generic[O]):
    """A type definition for a serializer"""
    __call__: typing.Callable[[typing.Optional[O]], bytes]

class DeserializerType(typing.Generic[I]):
    """A type definition for a deserializer"""
    __call__: typing.Callable[[bytes], typing.Optional[I]]


class JsonSerde(Serde[I]):
    """A JSON serializer/deserializer."""

    def deserialize(self, buf: bytes) -> typing.Optional[I]:
        """
        Deserializes a bytearray to a JSON object.

        Args:
            buf (bytearray): The bytearray to deserialize.

        Returns:
            typing.Optional[I]: The deserialized JSON object.
        """
        if not buf:
            return None
        return json.loads(buf)

    def serialize(self, obj: typing.Optional[I]) -> bytes:
        """
        Serializes a JSON object to a bytearray.

        Args:
            obj (I): The JSON object to serialize.

        Returns:
            bytearray: The serialized bytearray.
        """
        if obj is None:
            return bytes()

        return bytes(json.dumps(obj), "utf-8")


def deserialize_json(buf: typing.ByteString) -> typing.Optional[O]:
    """
    Deserializes a bytearray to a JSON object.

    Args:
        buf (bytearray): The bytearray to deserialize.

    Returns:
        typing.Optional[O]: The deserialized JSON object.
    """
    if not buf:
        return None
    return json.loads(buf)

def serialize_json(obj: typing.Optional[O]) -> bytes:
    """
    Serializes a JSON object to a bytearray.

    Args:
        obj (O): The JSON object to serialize.

    Returns:
        bytearray: The serialized bytearray.
    """
    if obj is None:
        return bytes()

    return bytes(json.dumps(obj), "utf-8")
