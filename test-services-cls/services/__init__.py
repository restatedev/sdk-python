from restate.cls import _SERVICE_ATTR
from restate.service import Service as _OrigService
from restate.object import VirtualObject as _OrigObject
from restate.workflow import Workflow as _OrigWorkflow

from .counter import Counter as s1
from .proxy import Proxy as s2
from .awakeable_holder import AwakeableHolder as s3
from .block_and_wait_workflow import BlockAndWaitWorkflow as s4
from .cancel_test import CancelTestRunner, CancelTestBlockingService as s5
from .failing import Failing as s6
from .kill_test import KillTestRunner, KillTestSingleton as s7
from .list_object import ListObject as s8
from .map_object import MapObject as s9
from .non_determinism import NonDeterministic as s10
from .test_utils import TestUtilsService as s11
from .virtual_object_command_interpreter import VirtualObjectCommandInterpreter as s16

from .interpreter import layer_0 as s12
from .interpreter import layer_1 as s13
from .interpreter import layer_2 as s14
from .interpreter import helper as s15


def list_services(bindings):
    """List all services from local bindings — supports both class-based and decorator-based."""
    result = {}
    for _, obj in bindings.items():
        svc = getattr(obj, _SERVICE_ATTR, obj)
        if isinstance(svc, (_OrigService, _OrigObject, _OrigWorkflow)):
            result[svc.name] = obj
    return result


def services_named(service_names):
    return [_all_services[name] for name in service_names]


def all_services():
    return _all_services.values()


_all_services = list_services(locals())
