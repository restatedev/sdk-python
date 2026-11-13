#!/usr/bin/env python3
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
"""Measure what an agent's turn costs in Restate traffic, steered or not, against a real Restate server.

Serves an agent whose framework is the tests' made-up one: each message is
answered by one tool call (which a test gate holds while a message is
steered to the turn), a step, and a reply. For each turn it reads, from
Restate's admin SQL API, the ``chat`` invocation's journal (its commands and
notifications) and the calls it made to the agent's companion, by handler;
and for a steered message, the invocations it costs on its own: the
``steer`` view, the companion's ``steer`` and the agent's ``steered``.

It measures any checkout of this repository, so that a turn nobody steers
can be compared with one of a version before steering::

    python scripts/steering_traffic.py --tree . --turns 5
    python scripts/steering_traffic.py --tree ../before --turns 5    # no steering: its plain turns only

It runs Restate's test harness (Docker, unless the harness is made to run a
local server).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import typing
import uuid
from pathlib import Path
from typing import Any


def _companion(service: str) -> bool:
    """Whether ``service`` is an agent's companion, ``<agent>.prebuilt.restate``."""
    return service.endswith(".prebuilt.restate")


async def query(client: Any, admin: str, sql: str) -> list[dict[str, Any]]:
    response = await client.post(f"{admin}/query", json={"query": sql}, headers={"accept": "application/json"})
    response.raise_for_status()
    return typing.cast(list[dict[str, Any]], response.json()["rows"])


async def cost(client: Any, admin: str, invocation: str) -> dict[str, Any]:
    """Return the journal of ``invocation`` (commands, notifications) and the companion calls it made, by handler."""
    journal = await query(client, admin, f"SELECT entry_type FROM sys_journal WHERE id = '{invocation}'")
    kinds = collections.Counter(row["entry_type"].split(":")[0] for row in journal)
    called = await query(
        client,
        admin,
        f"SELECT target_service_name, target_handler_name FROM sys_invocation WHERE invoked_by_id = '{invocation}'",
    )
    companion = collections.Counter(
        row["target_handler_name"] for row in called if _companion(row["target_service_name"])
    )
    return {
        "journal": len(journal),
        "commands": kinds.get("Command", 0),
        "notifications": kinds.get("Notification", 0),
        "companion": dict(sorted(companion.items())),
        "companion_calls": sum(companion.values()),
    }


async def measure(args: argparse.Namespace) -> None:
    tree = Path(args.tree).resolve()
    sys.path[:0] = [str(tree / "python"), str(tree / "tests")]
    # pylint: disable=import-outside-toplevel,import-error
    import httpx2
    from agents_conversation import ITEMS
    from support import RESTATE_IMAGE, Gate

    import restate
    from restate import sessions
    from restate.agents._conversation import Conversation, answer_once
    from restate.agents._object import AgentObject
    from restate.agents._turn import stop_signal_of, tool_call_ended, tool_call_started

    try:
        from restate.agents._steering import take_steers
    except ImportError:
        take_steers = None
    steering = take_steers is not None
    gate = Gate()

    async def answer(ctx: sessions.SessionContext, text: str) -> str:
        async def work(conversation: Conversation) -> str:
            turn = conversation.turn
            if not turn.items:
                call = {"id": "call-1", "name": "lookup", "input": {}}
                await turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
                await tool_call_started(ctx)
                stop = stop_signal_of(ctx)
                assert stop is not None
                await stop.guard(gate.wait())
                await turn.append({"result": "call-1", "output": "found"})
                await tool_call_ended(ctx)
            if take_steers is not None:
                await take_steers(ctx, conversation)
            reply = f"answered: {text}"
            await turn.append_reply({"role": "assistant", "text": reply})
            return reply

        return await answer_once(ctx, text, ITEMS, work)

    agent = AgentObject("measured")
    if steering:
        agent.chat(answer, items=ITEMS, steers=True)
    else:
        agent.chat(answer, items=ITEMS)

    text_headers = {"content-type": "text/plain"}
    rows: list[dict[str, Any]] = []
    async with restate.create_test_harness(restate.app([agent]), restate_image=RESTATE_IMAGE) as harness:
        ingress, admin = harness.ingress_url, harness.admin_api_url
        async with httpx2.AsyncClient(timeout=60) as client:

            async def answering(key: str, invocation: str) -> None:
                for _ in range(400):
                    status = (await client.post(f"{ingress}/measured/{key}/status")).json()
                    if status["answering"] is not None and status["answering"]["invocationId"] == invocation:
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("the turn never started")

            async def done(key: str, handler: str) -> list[dict[str, Any]]:
                rows = await query(
                    client,
                    admin,
                    "SELECT id, status FROM sys_invocation WHERE target_service_name = 'measured' "
                    f"AND target_service_key = '{key}' AND target_handler_name = '{handler}'",
                )
                return rows

            for n in range(args.turns):
                key = f"plain-{n}-{uuid.uuid4().hex[:6]}"
                gate.open()
                sent = await client.post(
                    f"{ingress}/measured/{key}/chat/send", content=f"look {n}", headers=text_headers
                )
                invocation = sent.json()["invocationId"]
                await client.get(f"{ingress}/restate/invocation/{invocation}/attach")
                rows.append({"turn": "plain", "n": n, **await cost(client, admin, invocation)})

            for n in range(args.turns if steering else 0):
                key = f"steered-{n}-{uuid.uuid4().hex[:6]}"
                gate.close()
                sent = await client.post(
                    f"{ingress}/measured/{key}/chat/send", content=f"look {n}", headers=text_headers
                )
                invocation = sent.json()["invocationId"]
                await answering(key, invocation)
                steered = await client.post(
                    f"{ingress}/measured/{key}/steer",
                    content=f"and note {n}",
                    headers={**text_headers, "idempotency-key": uuid.uuid4().hex},
                )
                assert steered.json()["turn"] == invocation, steered.text
                gate.open()
                answer_text = (await client.get(f"{ingress}/restate/invocation/{invocation}/attach")).json()
                # The placeholder runs once the turn ended: wait for it, then cost the message's own invocations.
                for _ in range(400):
                    placeholders = await done(key, "steered")
                    if placeholders and all(row["status"] == "completed" for row in placeholders):
                        break
                    await asyncio.sleep(0.05)
                [view] = await done(key, "steer")
                [placeholder] = placeholders
                companion_steer = await query(
                    client, admin, f"SELECT id FROM sys_invocation WHERE invoked_by_id = '{view['id']}'"
                )
                rows.append(
                    {
                        "turn": "steered",
                        "n": n,
                        "answer": answer_text,
                        **await cost(client, admin, invocation),
                        "steer_view": (await cost(client, admin, view["id"]))["journal"],
                        "companion_steer": [
                            (await cost(client, admin, row["id"]))["journal"] for row in companion_steer
                        ],
                        "placeholder": (await cost(client, admin, placeholder["id"]))["journal"],
                    }
                )

    for row in rows:
        print(json.dumps(row))
    for kind in ("plain", "steered"):
        mine = [row for row in rows if row["turn"] == kind]
        if not mine:
            continue
        journals = sorted({row["journal"] for row in mine})
        calls = sorted({json.dumps(row["companion"]) for row in mine})
        print(
            f"# {kind:8} turns: {len(mine)}, chat journal entries {journals}, companion calls {calls}",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", default=".", help="the checkout to measure")
    parser.add_argument("--turns", type=int, default=5, help="turns of each kind")
    asyncio.run(measure(parser.parse_args()))


if __name__ == "__main__":
    main()
