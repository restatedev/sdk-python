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
from datetime import timedelta

import restate
from restate.discovery import compute_discovery
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
