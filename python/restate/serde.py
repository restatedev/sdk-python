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
import abc
import json
import typing

T = typing.TypeVar('T')
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


class Serde(typing.Generic[T], abc.ABC):
    """serializer/deserializer interface."""

    @abc.abstractmethod
    def deserialize(self, buf: bytes) -> typing.Optional[T]:
        """
        Deserializes a bytearray to an object.
        """

    @abc.abstractmethod
    def serialize(self, obj: typing.Optional[T]) -> bytes:
        """
        Serializes an object to a bytearray.
        """


class BytesSerde(Serde[bytes]):
    """A pass-trough serializer/deserializer."""

    def deserialize(self, buf: bytes) -> typing.Optional[bytes]:
        """
        Deserializes a bytearray to a bytearray.

        Args:
            buf (bytearray): The bytearray to deserialize.

        Returns:
            typing.Optional[bytes]: The deserialized bytearray.
        """
        return buf

    def serialize(self, obj: typing.Optional[bytes]) -> bytes:
        """
        Serializes a bytearray to a bytearray.

        Args:
            obj (bytes): The bytearray to serialize.

        Returns:
            bytearray: The serialized bytearray.
        """
        if obj is None:
            return bytes()
        return obj


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
