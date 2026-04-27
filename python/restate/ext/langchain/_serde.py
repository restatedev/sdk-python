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

import typing

from pydantic import TypeAdapter
from restate.serde import Serde

T = typing.TypeVar("T")


class PydanticTypeAdapter(Serde[T]):
    """A serializer/deserializer for Pydantic models, used to journal LangChain
    messages and middleware responses through Restate's run actions."""

    def __init__(self, model_type: typing.Any):
        self._model_type = TypeAdapter(model_type)

    def deserialize(self, buf: bytes) -> typing.Optional[T]:
        if not buf:
            return None
        return self._model_type.validate_json(buf.decode("utf-8"))

    def serialize(self, obj: typing.Optional[T]) -> bytes:
        if obj is None:
            return b""
        tpe = TypeAdapter(type(obj))
        return tpe.dump_json(obj)
