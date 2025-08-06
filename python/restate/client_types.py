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
Type definitions for the Restate client.
"""
import abc
from datetime import timedelta
import typing

from .context import HandlerType

I = typing.TypeVar('I')
O = typing.TypeVar('O')

class RestateClientSendHandle:
    """
    A handle for a send operation.
    This is used to track the status of a send operation.
    """
    def __init__(self, invocation_id: str, status_code: int):
        self.invocation_id = invocation_id
        self.status_code = status_code
 
class RestateClient(abc.ABC):
    """
    An abstract base class for a Restate client.
    This class defines the interface for a Restate client.
    """

    @abc.abstractmethod
    async def service_call(self,
                tpe: HandlerType[I, O],
                arg: I,
                idempotency_key: str | None = None,
                headers: typing.Dict[str, str] | None = None) -> O:
        """Make an RPC call to the given handler"""
        pass
    
    @abc.abstractmethod
    async def object_call(self,
                    tpe: HandlerType[I, O],
                    key: str,
                    arg: I,
                    idempotency_key: str | None = None,
                    headers: typing.Dict[str, str] | None = None) -> O:
        """Make an RPC call to the given object handler"""
        pass

    @abc.abstractmethod
    async def workflow_call(self,
                        tpe: HandlerType[I, O],
                        key: str,
                        arg: I,
                        idempotency_key: str | None = None,
                        headers: typing.Dict[str, str] | None = None) -> O:
        """Make an RPC call to the given workflow handler"""
        pass
    
    @abc.abstractmethod
    async def generic_call(self, service: str, handler: str, arg: bytes,
                            key: str | None = None,
                            idempotency_key: str | None = None,
                            headers: typing.Dict[str, str] | None = None) -> bytes:
          """Make a generic RPC call to the given service and handler"""
          pass
      
    @abc.abstractmethod
    async def generic_send(self, service: str, handler: str, arg: bytes,
                            key: str | None = None,
                            send_delay: timedelta | None = None,
                            idempotency_key: str | None = None,
                            headers: typing.Dict[str, str] | None = None) -> RestateClientSendHandle:
        """Make a generic send operation to the given service and handler"""
        pass