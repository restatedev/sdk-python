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
"""restate.Servable: what restate.app serves for each thing it is given."""

# pylint: disable=missing-function-docstring,missing-class-docstring

from collections.abc import Iterable

import pytest

import restate
from restate import sessions
from restate.agents import AgentObject
from restate.endpoint import Endpoint
from restate.servable import definitions_of


class Needing(restate.VirtualObject):
    """A library's object that brings services of its own along."""

    def __init__(self, name: str, *needs: restate.Servable) -> None:
        super().__init__(name)
        self.needs = needs

    def __restate_services__(self) -> tuple[restate.Servable, ...]:
        return (self, *self.needs)


class Team:
    """A group of servables that is not a service itself."""

    def __init__(self, *members: restate.Servable) -> None:
        self.members = members

    def __restate_services__(self) -> Iterable[restate.Servable]:
        return self.members


def names(servables: Iterable[restate.Servable]) -> list[str]:
    return [definition.name for definition in definitions_of(servables)]


def test_services_objects_and_workflows_serve_themselves():
    service, obj, workflow = restate.Service("s"), restate.VirtualObject("o"), restate.Workflow("w")
    for definition in (service, obj, workflow, sessions.SessionObject("session")):
        assert isinstance(definition, restate.Servable)
        assert tuple(definition.__restate_services__()) == (definition,)
    assert names([service, obj, workflow]) == ["s", "o", "w"]


def test_what_a_servable_brings_along_is_served_once_depth_first():
    helper = restate.Service("helper")
    companion = Needing("companion", helper)
    first = Needing("first", companion, helper)
    second = Needing("second", companion)
    assert names([first, second]) == ["first", "companion", "helper", "second"]


def test_a_group_serves_its_members():
    shared = restate.VirtualObject("shared")
    assert names([Team(Needing("a", shared), Team(Needing("b", shared))), shared]) == ["a", "shared", "b"]


def test_an_agent_brings_its_own_companion_along():
    first, second = AgentObject("first_agent"), AgentObject("second_agent")
    served = ["first_agent", "first_agent.prebuilt.restate", "second_agent", "second_agent.prebuilt.restate"]
    assert names([first, second]) == served
    endpoint = Endpoint().bind(first).bind(second)  # bound one at a time, each with its companion
    assert list(endpoint.services) == served
    assert names([first, first]) == served[:2]


def test_what_cannot_be_served():
    with pytest.raises(TypeError, match="cannot serve"):
        definitions_of([object()])  # type: ignore[list-item]

    class Itself:
        def __restate_services__(self) -> Iterable[restate.Servable]:
            return (self,)

    with pytest.raises(TypeError, match="lists itself"):
        definitions_of([Itself()])
    with pytest.raises(ValueError, match="Service twin already exists"):
        definitions_of([restate.Service("twin"), restate.VirtualObject("twin")])
    with pytest.raises(ValueError, match="Service twin already exists"):
        Endpoint().bind(restate.Service("twin")).bind(restate.Service("twin"))
