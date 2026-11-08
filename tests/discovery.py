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
import json
from datetime import timedelta

import pytest

import restate
from restate.discovery import compute_discovery, compute_discovery_json
from restate.endpoint import Endpoint


def test_zero_durations_are_preserved_in_manifest():
    service = restate.Service(
        "ZeroDurationService",
        inactivity_timeout=timedelta(0),
        abort_timeout=timedelta(0),
        journal_retention=timedelta(0),
        idempotency_retention=timedelta(0),
    )

    @service.handler(
        inactivity_timeout=timedelta(0),
        abort_timeout=timedelta(0),
        journal_retention=timedelta(0),
        idempotency_retention=timedelta(0),
    )
    async def handle(ctx: restate.Context):
        pass

    workflow = restate.Workflow("ZeroRetentionWorkflow")

    @workflow.main(workflow_retention=timedelta(0))
    async def run(ctx: restate.WorkflowContext):
        pass

    manifest = compute_discovery(Endpoint().bind(service, workflow), "bidi")

    service_manifest = manifest.services[0]
    assert service_manifest.inactivityTimeout == 0
    assert service_manifest.abortTimeout == 0
    assert service_manifest.journalRetention == 0
    assert service_manifest.idempotencyRetention == 0

    handler_manifest = service_manifest.handlers[0]
    assert handler_manifest.inactivityTimeout == 0
    assert handler_manifest.abortTimeout == 0
    assert handler_manifest.journalRetention == 0
    assert handler_manifest.idempotencyRetention == 0

    workflow_handler_manifest = manifest.services[1].handlers[0]
    assert workflow_handler_manifest.workflowCompletionRetention == 0


def _object_manifest(state_preload=None, enable_lazy_state=None, handler_kwargs=None):
    obj = restate.VirtualObject("Obj", state_preload=state_preload, enable_lazy_state=enable_lazy_state)

    @obj.handler(**(handler_kwargs or {}))
    async def handle(ctx: restate.ObjectContext):
        pass

    return compute_discovery(Endpoint().bind(obj), "bidi").services[0]


def test_state_preload_whitelist_on_object_and_handler():
    keys = ["a", "b"]
    service_manifest = _object_manifest(state_preload=keys, handler_kwargs={"state_preload": ["c"]})
    assert service_manifest.enableLazyState is True
    assert service_manifest.eagerStateKeysWhitelist == ["a", "b"]
    assert service_manifest.handlers[0].enableLazyState is True
    assert service_manifest.handlers[0].eagerStateKeysWhitelist == ["c"]
    # The whitelist must be a copy of the user list
    keys.append("z")
    assert service_manifest.eagerStateKeysWhitelist == ["a", "b"]


def test_state_preload_booleans():
    service_manifest = _object_manifest(state_preload=False, handler_kwargs={"state_preload": True})
    assert service_manifest.enableLazyState is True
    assert service_manifest.eagerStateKeysWhitelist is None
    assert service_manifest.handlers[0].enableLazyState is False
    assert service_manifest.handlers[0].eagerStateKeysWhitelist is None


def test_state_preload_empty_list_is_fully_lazy():
    service_manifest = _object_manifest(state_preload=[])
    assert service_manifest.enableLazyState is True
    assert service_manifest.eagerStateKeysWhitelist is None


def test_state_preload_unset_keeps_legacy_enable_lazy_state():
    service_manifest = _object_manifest(enable_lazy_state=True, handler_kwargs={"enable_lazy_state": False})
    assert service_manifest.enableLazyState is True
    assert service_manifest.eagerStateKeysWhitelist is None
    assert service_manifest.handlers[0].enableLazyState is False
    assert service_manifest.handlers[0].eagerStateKeysWhitelist is None

    service_manifest = _object_manifest()
    assert service_manifest.enableLazyState is None
    assert service_manifest.handlers[0].enableLazyState is None


def test_state_preload_and_enable_lazy_state_are_mutually_exclusive():
    with pytest.raises(ValueError):
        restate.VirtualObject("Obj", state_preload=False, enable_lazy_state=True)
    with pytest.raises(ValueError):
        restate.Workflow("Wf", state_preload=["a"], enable_lazy_state=True)

    obj = restate.VirtualObject("Obj")
    with pytest.raises(ValueError):

        @obj.handler(state_preload=False, enable_lazy_state=True)
        async def handle(ctx: restate.ObjectContext):
            pass

    wf = restate.Workflow("Wf")
    with pytest.raises(ValueError):

        @wf.main(state_preload=["a"], enable_lazy_state=False)
        async def run(ctx: restate.WorkflowContext):
            pass


def test_state_preload_on_workflow():
    wf = restate.Workflow("Wf", state_preload=["a"])

    @wf.main(state_preload=["b"])
    async def run(ctx: restate.WorkflowContext):
        pass

    @wf.handler(state_preload=False)
    async def query(ctx: restate.WorkflowSharedContext):
        pass

    service_manifest = compute_discovery(Endpoint().bind(wf), "bidi").services[0]
    assert service_manifest.enableLazyState is True
    assert service_manifest.eagerStateKeysWhitelist == ["a"]
    handlers = {h.name: h for h in service_manifest.handlers}
    assert handlers["run"].eagerStateKeysWhitelist == ["b"]
    assert handlers["query"].enableLazyState is True
    assert handlers["query"].eagerStateKeysWhitelist is None


def test_stateless_service_never_advertises_state_options():
    service = restate.Service("Svc")

    @service.handler()
    async def handle(ctx: restate.Context):
        pass

    service_manifest = compute_discovery(Endpoint().bind(service), "bidi").services[0]
    assert service_manifest.enableLazyState is None
    assert service_manifest.eagerStateKeysWhitelist is None
    assert service_manifest.handlers[0].enableLazyState is None
    assert service_manifest.handlers[0].eagerStateKeysWhitelist is None


def test_eager_state_keys_whitelist_requires_manifest_v5():
    obj = restate.VirtualObject("Obj", state_preload=["a"])

    @obj.handler()
    async def handle(ctx: restate.ObjectContext):
        pass

    endpoint = Endpoint().bind(obj)
    with pytest.raises(ValueError, match="eagerStateKeysWhitelist"):
        compute_discovery_json(endpoint, 4, "bidi")
    manifest = json.loads(compute_discovery_json(endpoint, 5, "bidi"))
    assert manifest["services"][0]["enableLazyState"] is True
    assert manifest["services"][0]["eagerStateKeysWhitelist"] == ["a"]
    assert "eagerStateKeysWhitelist" not in manifest["services"][0]["handlers"][0]

    # Handler-level whitelist is rejected too
    obj2 = restate.VirtualObject("Obj2")

    @obj2.handler(state_preload=["a"])
    async def handle2(ctx: restate.ObjectContext):
        pass

    with pytest.raises(ValueError, match="eagerStateKeysWhitelist"):
        compute_discovery_json(Endpoint().bind(obj2), 4, "bidi")


def test_state_preload_false_works_on_manifest_v4():
    obj = restate.VirtualObject("Obj", state_preload=False)

    @obj.handler()
    async def handle(ctx: restate.ObjectContext):
        pass

    manifest = json.loads(compute_discovery_json(Endpoint().bind(obj), 4, "bidi"))
    assert manifest["services"][0]["enableLazyState"] is True
    assert "eagerStateKeysWhitelist" not in manifest["services"][0]
