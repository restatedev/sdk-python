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
from datetime import datetime

# pylint: disable=C0116
# pylint: disable=W0613

from restate import Service, Context

random_greeter = Service("random_greeter")

@random_greeter.handler()
async def greet(ctx: Context, name: str) -> str:

    # ctx.random() returns a Python Random instance seeded deterministically.
    # By using ctx.random() you don't write entries in the journal,
    # but you still get the same generated values on retries.

    # To generate random numbers
    random_number = ctx.random().randint(0, 100)

    # To generate random bytes
    random_bytes = ctx.random().randbytes(10)

    # Use ctx.uuid() to generate a UUID v4 seeded deterministically
    # As with ctx.random(), this won't write entries in the journal
    random_uuid = ctx.uuid()

    # To get a timestamp, use ctx.time()
    # This will record the timestamp in the Restate journal
    now = await ctx.time()

    # You can convert it to date/datetime using Python's standard library functions, e.g.
    now_datetime = datetime.fromtimestamp(now)

    # Or to perform a difference:
    # start = await ctx.time()
    # # Some code
    # end = await ctx.time()
    # delta = datetime.timedelta(seconds=(end-start))

    return (f"Hello {name} with "
            f"random number {random_number}, "
            f"random bytes {random_bytes!r} "
            f"random uuid {random_uuid},"
            f"now datetime {now_datetime}!")
