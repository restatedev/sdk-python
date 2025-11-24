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
"""This module contains functions for serializing and deserializing data."""

import abc
import json
import typing

from dataclasses import asdict, is_dataclass

T = typing.TypeVar("T")
I = typing.TypeVar("I")
O = typing.TypeVar("O")


def try_import_pydantic_base_model():
    """
    Try to import PydanticBaseModel from Pydantic.
    """
    try:
        from pydantic import BaseModel  # type: ignore # pylint: disable=import-outside-toplevel

        return BaseModel
    except ImportError:

        class Dummy:  # pylint: disable=too-few-public-methods
            """a dummy class to use when Pydantic is not available"""

        return Dummy


def try_import_from_dacite():
    """
    Try to import from_dict from dacite.
    """
    try:
        from dacite import from_dict  # type: ignore # pylint: disable=import-outside-toplevel

        def _to_dict(obj: typing.Any) -> dict[typing.Any, typing.Any]:
            return asdict(obj)

        def _from_dict(data_class: typing.Any, data: typing.Any) -> typing.Any:
            return from_dict(data_class, data)

        return _to_dict, _from_dict

    except ImportError:

        def _to_dict(obj: typing.Any) -> dict[typing.Any, typing.Any]:
            """a dummy function when dacite is not available"""
            raise RuntimeError(
                "Trying to deserialize into a @dataclass."
                "Please add the optional dependencies needed."
                "use pip install restate-sdk[serde] "
                "or"
                " pip install restate-sdk[all] to install all dependencies."
            )

        def _from_dict(data_class: typing.Any, data: typing.Any) -> typing.Any:  # pylint: disable=too-few-public-methods,unused-argument
            """a dummy function when dacite is not available"""

            raise RuntimeError(
                "Trying to deserialize into a @dataclass."
                "Please add the optional dependencies needed."
                "use pip install restate-sdk[serde] "
                "or"
                " pip install restate-sdk[all] to install all dependencies."
            )

        return _to_dict, _from_dict


class MsgspecJsonAPI:
    def is_struct(self, annotation: typing.Any) -> bool:
        return False

    def decode(self, buf: bytes, type: typing.Type[T]) -> T:
        raise NotImplementedError("Please use msgspec as a conditional dependency to use msgspec features.")

    def encode(self, obj: typing.Any) -> bytes:
        raise NotImplementedError("Please use msgspec as a conditional dependency to use msgspec features.")

    def json_schema(self, type: typing.Type[T]) -> dict[str, typing.Any]:
        raise NotImplementedError("Please use msgspec as a conditional dependency to use msgspec features.")


def try_import_msgspec_api():
    """
    Try to import msgspec API.
    """
    try:
        from msgspec import Struct  # type: ignore # pylint: disable=import-outside-toplevel
        import msgspec

        class MsgspecImpl(MsgspecJsonAPI):
            def is_struct(self, annotation: typing.Any) -> bool:
                try:
                    return issubclass(annotation, Struct)
                except TypeError:
                    # annotation is not a class or a type
                    return False

            def decode(self, buf: bytes, type: typing.Type[T]) -> T:
                return msgspec.json.decode(buf, type=type)

            def encode(self, obj: typing.Any) -> bytes:
                return msgspec.json.encode(obj)

            def json_schema(self, type: typing.Type[T]) -> dict[str, typing.Any]:
                return msgspec.json.schema(type)

        return MsgspecImpl()

    except ImportError:
        return MsgspecJsonAPI()


PydanticBaseModel = try_import_pydantic_base_model()
Msgspec = try_import_msgspec_api()
# pylint: disable=C0103
DaciteToDict, DaciteFromDict = try_import_from_dacite()

# disable to few parameters
# pylint: disable=R0903


def is_pydantic(annotation) -> bool:
    """
    Check if an object is a Pydantic model.
    """
    try:
        return issubclass(annotation, PydanticBaseModel)
    except TypeError:
        # annotation is not a class or a type
        return False


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


class DefaultSerde(Serde[I]):
    """
    The default serializer/deserializer used when no explicit type hints are provided.

    Behavior:
    - Serialization:
        - If the object is an instance of Pydantic's `BaseModel`,
            it uses `model_dump_json()` for serialization.
        - Otherwise, it falls back to `json.dumps()`.
    - Deserialization:
        - Uses `json.loads()` to convert byte arrays into Python objects.
        - Does **not** automatically reconstruct Pydantic models;
            deserialized objects remain as generic JSON structures (dicts, lists, etc.).

    Serde Selection:
    - When using the `@handler` decorator, if a function's type hints specify a Pydantic model,
      `PydanticJsonSerde` is automatically selected instead of `DefaultSerde`.
    - `DefaultSerde` is only used if no explicit type hints are provided.

    This serde ensures compatibility with both structured (Pydantic) and unstructured JSON data,
    while allowing automatic serde selection based on type hints.
    """

    def __init__(self, type_hint: typing.Optional[typing.Type[I]] = None):
        super().__init__()
        self.type_hint = type_hint

    def with_maybe_type(self, type_hint: typing.Type[I] | None = None) -> "DefaultSerde[I]":
        """
        Returns a new instance of DefaultSerde with the provided type hint.
        This is useful for creating a serde that is specific to a certain type.
        NOTE: This method does not modify the current instance.
        Args:
            type_hint (Type[I] | None): The type hint to use for serialization/deserialization.
        Returns:
            DefaultSerde[I]: A new instance of DefaultSerde with the provided type hint.
        """
        return DefaultSerde(type_hint)

    def deserialize(self, buf: bytes) -> typing.Optional[I]:
        """
        Deserializes a byte array into a Python object.

        Args:
            buf (bytes): The byte array to deserialize.

        Returns:
            Optional[I]: The resulting Python object, or None if the input is empty.
        """
        if not buf:
            return None
        hint = self.type_hint
        if not hint:
            return json.loads(buf)
        if Msgspec.is_struct(hint):
            return Msgspec.decode(buf, type=hint)
        if is_pydantic(hint):
            return hint.model_validate_json(buf)  # type: ignore
        if is_dataclass(hint):
            data = json.loads(buf)
            return DaciteFromDict(hint, data)
        # although we have a type hint, we fall back to json.loads because we were not able to
        # identify a specific deserialization method, perhaps the user specified a default type
        # for another reason than serialization/deserialization.
        return json.loads(buf)

    def serialize(self, obj: typing.Optional[I]) -> bytes:
        """
        Serializes a Python object into a byte array.
        If the object is a msgspec Struct or Pydantic BaseModel, uses their respective methods.

        Args:
            obj (Optional[I]): The Python object to serialize.

        Returns:
            bytes: The serialized byte array.
        """
        if obj is None:
            return bytes()
        hint = self.type_hint
        if not hint:
            return json.dumps(obj).encode("utf-8")
        if Msgspec.is_struct(hint):
            return Msgspec.encode(obj)
        if is_pydantic(hint):
            return obj.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]
        if is_dataclass(obj):
            data = DaciteToDict(obj)  # type: ignore
            return json.dumps(data).encode("utf-8")
        # although we have a type hint, we fall back to json.dumps because we were not able to
        # identify a specific serialization method, perhaps the user specified a default type
        # for another reason than serialization/deserialization.
        return json.dumps(obj).encode("utf-8")


class PydanticJsonSerde(Serde[I]):
    """
    Serde for Pydantic models to/from JSON
    """

    def __init__(self, model):
        self.model = model

    def deserialize(self, buf: bytes) -> typing.Optional[I]:
        """
        Deserializes a bytearray to a Pydantic model.

        Args:
            buf (bytearray): The bytearray to deserialize.

        Returns:
            typing.Optional[I]: The deserialized Pydantic model.
        """
        if not buf:
            return None
        return self.model.model_validate_json(buf)

    def serialize(self, obj: typing.Optional[I]) -> bytes:
        """
        Serializes a Pydantic model to a bytearray.

        Args:
            obj (I): The Pydantic model to serialize.

        Returns:
            bytearray: The serialized bytearray.
        """
        if obj is None:
            return bytes()
        json_str = obj.model_dump_json()  # type: ignore[attr-defined]
        return json_str.encode("utf-8")


class MsgspecJsonSerde(Serde[I]):
    """
    Serde for msgspec Structs to/from JSON
    """

    def __init__(self, model):
        self.model = model

    def deserialize(self, buf: bytes) -> typing.Optional[I]:
        """
        Deserializes a bytearray to a msgspec Struct.

        Args:
            buf (bytearray): The bytearray to deserialize.

        Returns:
            typing.Optional[I]: The deserialized msgspec Struct.
        """
        if not buf:
            return None

        return Msgspec.decode(buf, type=self.model)

    def serialize(self, obj: typing.Optional[I]) -> bytes:
        """
        Serializes a msgspec Struct to a bytearray.

        Args:
            obj (I): The msgspec Struct to serialize.

        Returns:
            bytearray: The serialized bytearray.
        """
        if obj is None:
            return bytes()
        return Msgspec.encode(obj)
