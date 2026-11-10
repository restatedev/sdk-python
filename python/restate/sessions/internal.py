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
"""Building blocks for libraries built on session objects, and for tests.

Application code should not need this module; ``restate.agents`` is built on
it. It provides:

- ``Schema``: tables that a library owns and creates where it uses them
  (``await schema.ensure(ctx.db)``), and ``ensure_schemas``, which applies
  several in one commit.
- ``database``: another named database under the same key.
- ``reset``: empty the key's database inside a transaction.
- ``cancelled`` and ``cancel_calls``: explicit cancellation, for handlers
  declared with ``explicit_cancellation=True``.
- ``start`` and ``attach_invocation``: send, keeping the invocation id,
  and wait for that invocation's result; a library runs a workflow this way
  (a send to it again, after a crash, starts nothing: the same run).
- ``ScopeKey`` and ``definition_of``: state a library keeps per attempt, and
  the object an attempt belongs to.
- ``invoke``, ``SqlHost`` and ``ObjectConfig``: run one attempt of a handler
  against a host of your own, without Restate, for tests.
"""

from restate.sessions._object import attach_invocation, cancel_calls, cancelled, definition_of, invoke, start
from restate.sessions._schema import Schema, ensure_schemas
from restate.sessions._scope import ScopeKey
from restate.sessions._session import ObjectConfig, SqlHost, database, reset

__all__ = [
    "ObjectConfig",
    "Schema",
    "ScopeKey",
    "SqlHost",
    "attach_invocation",
    "cancel_calls",
    "cancelled",
    "database",
    "definition_of",
    "ensure_schemas",
    "invoke",
    "reset",
    "start",
]
