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
"""non_determinism.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from datetime import timedelta
from typing import Dict
from restate.cls import VirtualObject, handler, Restate

from . import counter

invoke_counts: Dict[str, int] = {}


def do_left_action() -> bool:
    count_key = Restate.key()
    invoke_counts[count_key] = invoke_counts.get(count_key, 0) + 1
    return invoke_counts[count_key] % 2 == 1


def increment_counter():
    add_fn = counter.Counter._restate_handlers["add"].fn
    Restate.object_send(add_fn, key=Restate.key(), arg=1)


class NonDeterministic(VirtualObject, name="NonDeterministic"):

    @handler(name="setDifferentKey")
    async def set_different_key(self):
        if do_left_action():
            Restate.set("a", "my-state")
        else:
            Restate.set("b", "my-state")
        await Restate.sleep(timedelta(milliseconds=100))
        increment_counter()

    @handler(name="backgroundInvokeWithDifferentTargets")
    async def background_invoke_with_different_targets(self):
        get_fn = counter.Counter._restate_handlers["get"].fn
        reset_fn = counter.Counter._restate_handlers["reset"].fn
        if do_left_action():
            Restate.object_send(get_fn, key="abc", arg=None)
        else:
            Restate.object_send(reset_fn, key="abc", arg=None)
        await Restate.sleep(timedelta(milliseconds=100))
        increment_counter()

    @handler(name="callDifferentMethod")
    async def call_different_method(self):
        get_fn = counter.Counter._restate_handlers["get"].fn
        reset_fn = counter.Counter._restate_handlers["reset"].fn
        if do_left_action():
            await Restate.object_call(get_fn, key="abc", arg=None)
        else:
            await Restate.object_call(reset_fn, key="abc", arg=None)
        await Restate.sleep(timedelta(milliseconds=100))
        increment_counter()

    @handler(name="eitherSleepOrCall")
    async def either_sleep_or_call(self):
        get_fn = counter.Counter._restate_handlers["get"].fn
        if do_left_action():
            await Restate.sleep(timedelta(milliseconds=100))
        else:
            await Restate.object_call(get_fn, key="abc", arg=None)
        await Restate.sleep(timedelta(milliseconds=100))
        increment_counter()
