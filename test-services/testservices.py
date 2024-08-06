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

app = restate.app(services=test_services())
