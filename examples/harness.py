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
import restate

from virtual_object import increment, count, counter

#
# uv run examples/harness.py
#


async def main():
    app = restate.app([counter])
    async with restate.create_test_harness(app) as harness:
        await harness.client.object_call(increment, key="a", arg=5)
        await harness.client.object_call(increment, key="a", arg=5)

        current_count = await harness.client.object_call(count, key="a", arg=None)

        print(f"Current count for 'a': {current_count}")

        send = await harness.client.object_send(increment, key="b", arg=-10, idempotency_key="op1")

        print(f"Sent increment to 'b', invocation ID: {send.invocation_id}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
