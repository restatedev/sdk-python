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

import logging
import restate

from greeter import greeter
from random_greeter import random_greeter
from virtual_object import counter
from workflow import payment
from pydantic_greeter import pydantic_greeter
from concurrent_greeter import concurrent_greeter

logging.basicConfig(level=logging.INFO)

app = restate.app(services=[greeter,
                            random_greeter,
                            counter,
                            payment,
                            pydantic_greeter,
                            concurrent_greeter])

if __name__ == "__main__":
    import hypercorn
    import hypercorn.asyncio
    import asyncio
    conf = hypercorn.Config()
    conf.bind = ["0.0.0.0:9080"]
    asyncio.run(hypercorn.asyncio.serve(app, conf))
