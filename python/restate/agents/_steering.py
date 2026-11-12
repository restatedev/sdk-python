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
"""Steering: a message sent while a turn is answered joins it, at the turn's next step.

A client sends the message to the agent's ``steer`` view. The companion
queues it at once as a turn of its own (the agent's private ``steered``), in
order with the messages sent after it, and, if the turn being answered takes
steered messages, holds it for that turn too (see the companion's ``steer``, in ``_signals.py``).

The turn learns of it from its next ``publish``, which it makes anyway as it
starts and after each write of steps that commits (a reply with tool calls,
the tools' results), and waits for. At its next step, once the model's reply has a
result for each of its tool calls and before the next model request, the
framework integration calls ``take_steers``: the turn stores each message
once, as a message of the user that is a step of the turn, and the
integration puts it in the request that is about to go out. The transaction
that stores them tells the companion they are taken (``taken``), once.

Once the turn ends, the queued ``steered`` for a message it took does
nothing; one it did not take (it came after the turn's last step, say) is
answered as a turn of its own. So each message is answered once.
"""

from __future__ import annotations

from restate.agents import _signals
from restate.agents._conversation import Conversation, Item
from restate.agents._turn import inbox_of, stop_signal_of
from restate.sessions import SessionContext, Transaction


async def take_steers(ctx: SessionContext, conversation: Conversation) -> list[Item]:
    """Take the messages steered to the turn being answered, at a step: store each once, and return their items.

    Call it right before a model request that follows the model's reply and
    its tools' results: the integration then puts the returned items in the
    request, after what it already holds. They are messages of the user, as
    the framework's ``Items`` makes them (``steered``, or else ``prompt``).
    It returns nothing, at no cost, unless the turn runs in a ``chat``
    handler of an agent that takes steered messages, a message it has not
    taken is waiting, the turn is not stopping, and each of its tool calls
    has a result.

    The messages are steps of the turn from then on: a retried attempt finds
    them stored, in ``history``, and never takes them again. They share the
    turn's fate: stopped, refused or failed with it, they are not answered
    again.
    """
    inbox = inbox_of(ctx)
    if inbox is None or not inbox.messages:
        return []
    stop = stop_signal_of(ctx)

    def going() -> bool:
        # A turn stopped while it waited to store them takes none: each is then answered as a turn of its own.
        return stop is None or not stop.stopped

    turn = conversation.running()
    if not going() or turn is None or turn.open_calls():
        return []
    companion = _signals.companion_in(ctx)

    def taken(tx: Transaction, ids: list[str]) -> None:
        # With the transaction that stores them: so they are marked taken once stored, and once.
        tx.send(companion.taken, ids, key=ctx.key, idempotency_key=f"taken:{ids[0]}")

    return await turn.steer(inbox.messages, taken, going)
