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
steered messages, holds it for that turn too (see ``_signals.steer``).

The turn learns of it from its next ``publish``, which it makes at each tool
call's start and end anyway. At its next step, once the model's reply has a
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
from restate.agents._conversation import Conversation, Entry
from restate.agents._turn import steering_of, stop_signal_of
from restate.sessions import SessionContext, Transaction


async def take_steers(ctx: SessionContext, conversation: Conversation) -> list[Entry]:
    """Take the messages steered to the turn being answered, at a step: store each once, and return the new ones.

    Call it right before a model request that follows the model's reply and
    its tools' results: the integration then gives the model the returned
    entries' items (messages of the user), after what the request already
    holds. It returns nothing, at no cost, unless the turn runs in a ``chat``
    handler of an agent that takes steered messages, a message is waiting,
    the turn is not stopping, and each of its tool calls has a result.

    The messages are steps of the turn from then on: a retried attempt finds
    them stored, in ``history``, and never takes them again. They share the
    turn's fate: stopped, refused or failed with it, they are not answered
    again.
    """
    steering = steering_of(ctx)
    if steering is None or not steering.messages:
        return []
    stop = stop_signal_of(ctx)
    if stop is not None and stop.stopped:
        return []
    turn = conversation.running()
    if turn is None or turn.open_calls():
        return []
    taken = {entry.steer for entry in turn.steered()}
    fresh = [message for message in steering.messages if message["id"] not in taken]
    if not fresh:
        return []
    companion = _signals.signals_of(ctx)

    def sent(tx: Transaction, ids: list[str]) -> None:
        # With the transaction that stores them: so they are marked taken once stored, and once.
        tx.send(_signals.taken, ids, key=companion, idempotency_key=f"taken:{ids[0]}")

    def going() -> bool:
        # A turn stopped while it waited to store them takes none: each is then answered as a turn of its own.
        return stop is None or not stop.stopped

    return await turn.steer(fresh, sent, going)
