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

from .counter import counter
from .proxy import proxy
from .awakable_holder import awakeable_holder
from. block_and_wait_workflow import workflow
from .cancel_test import runner, blocking_service

def list_services(bindings):
    """List all services in this module"""
    return {obj.name : obj for _, obj in bindings.items() if isinstance(obj, (Service, VirtualObject, Workflow))}

def services_named(service_names):
    return [ _all_services[name] for name in service_names ]

def all_services():
    return _all_services.values()

_all_services = list_services(locals())
