#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""client.py"""
# pylint: disable=C0116
# pylint: disable=W0613

import restate

from greeter import greet

async def main():
    client = restate.create_client("http://localhost:8080")
    res = await client.service_call(greet, arg="World")
    print(res)
    
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())