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
"""
What libraries built on session objects (``restate.agents``) and tests need,
beside the API handlers code against: as sdk-sessions'
``@restatedev/sessions/internal``.

- ``Schema``: tables a library owns, applied where it uses them
  (``schema.ensure(ctx.db)``); ``database``, another database of the same
  key; ``reset``, the key's database emptied in a transaction.
- ``cancelled`` and ``cancel_calls``: explicit cancellation, for a handler
  with ``explicit_cancellation=True``.
- ``ScopeKey`` and ``definition_of``: what a library keeps for an attempt, or
  for an object.
- ``needing`` and ``served``: the services a definition needs served beside
  it, which ``restate.sessions.app`` serves.
- ``invoke``: a handler's attempt over a host of a test's own, without
  Restate (see ``SqlHost``).
"""

from restate.sessions._object import cancel_calls, cancelled, definition_of, invoke
from restate.sessions._schema import Schema
from restate.sessions._scope import ScopeKey
from restate.sessions._serve import needing, served
from restate.sessions._session import ObjectConfig, SqlHost, database, reset

__all__ = [
    "ObjectConfig",
    "Schema",
    "ScopeKey",
    "SqlHost",
    "cancel_calls",
    "cancelled",
    "database",
    "definition_of",
    "invoke",
    "needing",
    "reset",
    "served",
]
