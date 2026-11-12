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
"""What ``restate.app`` serves: anything ``Servable``.

A service, a virtual object and a workflow are servable: each serves itself.
So is anything else that says what to serve for it, such as an agent, which
serves itself and its own companion, the object its tools wait on::

    app = restate.app([greeter, assistant])  # greeter, assistant, and assistant.agentprebuild.restate
"""

from __future__ import annotations

import typing
from collections.abc import Iterable

from restate.object import VirtualObject
from restate.service import Service
from restate.workflow import Workflow

Definition = typing.Union[Service, VirtualObject, Workflow]
"""A service, a virtual object or a workflow: what Restate serves in the end."""


@typing.runtime_checkable
class Servable(typing.Protocol):
    """Something ``restate.app`` can serve, because it says what to serve for it.

    ``__restate_services__`` returns the things to serve. A service, a virtual
    object or a workflow lists itself, and is served as it is; anything else it
    lists is served the same way, in turn. So a library's object can bring what
    it needs along (an agent lists itself and its companion), and a group of
    services can be one servable that lists them::

        class Team:
            def __init__(self, *members: restate.Servable) -> None:
                self.members = members

            def __restate_services__(self) -> Iterable[restate.Servable]:
                return self.members

    Each definition is served once, however many servables list it.
    """

    def __restate_services__(self) -> Iterable[Servable]:
        """Return the things to serve for this one: itself, if it is a service, and what it needs."""
        ...


def definitions_of(servables: Iterable[Servable]) -> list[Definition]:
    """Return the services, virtual objects and workflows to serve for ``servables``, each once, in order.

    Each servable is followed by what it lists, depth first.

    Raises:
        TypeError: If something listed is not servable, or lists itself without being a service, a
            virtual object or a workflow.
        ValueError: If two different definitions have the same name.
    """
    every: dict[str, Definition] = {}
    expanded: set[int] = set()  # by identity: the servables are alive for the whole call

    def add(servable: object) -> None:
        if id(servable) in expanded:
            return
        expanded.add(id(servable))
        if not isinstance(servable, Servable):
            raise TypeError(
                f"cannot serve {servable!r}: expected a service, a virtual object, a workflow, "
                "or another restate.Servable"
            )
        for item in servable.__restate_services__():
            if item is not servable:
                add(item)
                continue
            if not isinstance(item, (Service, VirtualObject, Workflow)):
                raise TypeError(f"{item!r} lists itself, but only a service, a virtual object or a workflow can")
            existing = every.get(item.name)
            if existing is not None and existing is not item:
                raise ValueError(f"Service {item.name} already exists")
            every[item.name] = item

    for servable in servables:
        add(servable)
    return list(every.values())
