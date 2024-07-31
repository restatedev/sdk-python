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
"""example.py"""
# pylint: disable=C0116
# pylint: disable=W0613

import restate
import os
from receiver import receiver
from coordinator import coordinator

# List of known services
known_services = [receiver, coordinator]

# Resolve the SERVICES env variable, if none just use *
services = "*"
if os.environ.get('SERVICES') is not None:
    services = os.environ['SERVICES']

# Resolve services to mount
known_services_map = dict([
    (svc.name, svc) for svc in known_services
])
mounted_services = []
if services == "*":
    mounted_services = list(known_services_map.values())
else:
    for svc in services.split(sep=","):
        mounted_services.append(known_services_map[svc])

app = restate.app(services=mounted_services)
