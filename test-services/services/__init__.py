#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE

from typing import Dict, Union
from restate import Service, VirtualObject, Workflow

from .counter import counter_object as s1
from .proxy import proxy as s2
from .awakeable_holder import awakeable_holder as s3
from. block_and_wait_workflow import workflow as s4
from .cancel_test import runner, blocking_service as s5
from .failing import failing as s6
from .kill_test import kill_runner, kill_singleton as s7
from .list_object import list_object as s8
from .map_object import map_object as s9
from .non_determinism import non_deterministic as s10
from .test_utils import test_utils as s11
from .virtual_object_command_interpreter import virtual_object_command_interpreter as s16

from .interpreter import layer_0 as s12
from .interpreter import layer_1 as s13
from .interpreter import layer_2 as s14
from .interpreter import helper as s15

def list_services(bindings):
    """List all services in this module"""
    return {obj.name : obj for _, obj in bindings.items() if isinstance(obj, (Service, VirtualObject, Workflow))}

def services_named(service_names):
    return [ _all_services[name] for name in service_names ]

def all_services():
    return _all_services.values()

_all_services = list_services(locals())
