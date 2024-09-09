#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""testservices.py"""
# pylint: disable=C0116
# pylint: disable=W0613

import os
import restate
import services

def test_services():
    names = os.environ.get('SERVICES')
    return services.services_named(names.split(',')) if names else services.all_services()

identity_keys = None
e2e_signing_key_env = os.environ.get('E2E_REQUEST_SIGNING_ENV')
if os.environ.get('E2E_REQUEST_SIGNING_ENV'):
    identity_keys = [os.environ.get('E2E_REQUEST_SIGNING_ENV')]

app = restate.app(services=test_services(), identity_keys=identity_keys)
