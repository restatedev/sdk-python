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
"""This module contains the journal value codec interface."""

import abc
import typing

# disable too few public methods
# pylint: disable=R0903


class JournalValueCodec(abc.ABC):
    """
    Journal values codec.

    This allows to transform journal values after being serialized, before writing them to the
    wire, and vice-versa. It sits *between* the ``Serde`` layer and the journal/wire.
    The canonical use case is to encrypt or compress everything the SDK persists to the journal.

    Values that are passed through the codec:

    * Handlers input and success output
    * ``ctx.run`` success results
    * Awakeables/Promise success results
    * State values
    * Call/send request parameters and call responses

    Failures are never passed through the codec.

    NOTE: This is preview and may change in future releases.
    """

    @abc.abstractmethod
    def encode(self, buf: bytes) -> bytes:
        """
        Encodes the given buffer. This will be applied *after* serialization.

        Args:
            buf: The buffer to encode. Empty byte buffers should be appropriately handled as well.

        Returns:
            The encoded buffer.
        """

    @abc.abstractmethod
    async def decode(self, buf: bytes) -> bytes:
        """
        Decodes the given buffer. This will be applied *before* deserialization.

        Args:
            buf: The buffer to decode.

        Returns:
            The decoded buffer.
        """


JournalValueCodecProvider = typing.Callable[[], typing.Awaitable[JournalValueCodec]]
"""A provider that asynchronously builds a :class:`JournalValueCodec`.

It is invoked once, and the resulting codec is reused for the lifetime of the endpoint.
This is useful to perform async setup at startup, e.g. loading an encryption key.
"""
