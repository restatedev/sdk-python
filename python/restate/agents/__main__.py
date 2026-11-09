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
"""Talk with an agent from a terminal: ``python -m restate.agents chat assistant/alice``.

It shows the conversation so far, then follows it as it changes: the agent's
messages, its tool calls with their outputs, and what its tools wait for.
What you type goes to the agent, as a message it answers after the one it is
answering, if any. A line that starts with ``/`` is a command::

    /steer <message>         send <message> to the turn being answered, which takes it at its next step
    /signal <name> [value]   resolve the oldest wait for the signal <name> (value: JSON, or text; default "approved")
    /pending                 what the agent's tools wait for
    /status                  what the agent is doing
    /stop                    stop the message it is answering
    /clear                   forget the conversation

Ctrl-D leaves; the agent carries on without you, and running it again shows
the conversation so far, and follows it from there. It needs Restate's
client: ``pip install "restate-sdk[client]"``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import uuid
from typing import Any

from restate.agents._api import Part

USAGE = """\
commands: /steer <message>, /signal <name> [value], /pending, /status, /stop, /clear, /help; Ctrl-D leaves
"""


def _clipped(text: str, limit: int = 160) -> str:
    line = text.replace("\n", " ⏎ ")
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


class Terminal:
    """What the terminal showed of the conversation: each part of a message once, and a tool's output once.

    The user's messages are shown too, where they are in the conversation (a
    message steered to a turn, say, among its steps), but for those typed here
    as messages to ``chat``: the terminal shows them already.
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.shown: set[str] = set()
        self.sent: list[str] = []
        """The messages typed here and sent to ``chat`` that the conversation has not shown yet."""
        self.typed: set[int] = set()
        """The ids of the user's messages that were typed here, and so not shown."""
        self.history = True
        """The first change is the conversation so far: all of it is shown, the messages typed before too."""

    def sending(self, text: str) -> None:
        """Note a message typed here and sent to ``chat``: when the conversation has it, it isn't shown again."""
        self.sent.append(text)

    def unsent(self, text: str) -> None:
        """Forget a message ``sending`` noted, whose send failed."""
        with contextlib.suppress(ValueError):
            self.sent.remove(text)

    def show(self, change: Any) -> None:
        for message in change.messages:
            if message["role"] == "user" and not self.history and self._typed(message):
                continue
            for i, part in enumerate(message["parts"]):
                self._part(f"{message['id']}.{i}", message["role"], part)
        for wait in change.waiting:
            hint = f"/signal {wait['name']} <value>"
            self._once(wait["id"], f"⏸  {wait['name']}: {json.dumps(wait.get('input'))}  ({hint})")
        self.history = False

    def _typed(self, message: Any) -> bool:
        """Whether the user's ``message`` was typed here: the first sent message of its text, and that one after."""
        if message["id"] in self.typed:
            return True
        text = "".join(part["text"] for part in message["parts"] if part["type"] == "text")
        if text not in self.sent:
            return False
        self.sent.remove(text)
        self.typed.add(message["id"])
        return True

    def _part(self, key: str, role: str, part: Part) -> None:
        if part["type"] == "text":
            self._once(key, f"{'you' if role == 'user' else self.agent}: {part['text']}")
            return
        self._once(key, f"  → {part['name']}({_clipped(json.dumps(part['input']))})")
        output = part.get("output")
        if output is not None:
            self._once(f"{key}.output", f"  ← {_clipped(output)}")

    def _once(self, key: str, text: str) -> None:
        if key not in self.shown:
            self.shown.add(key)
            print(text, flush=True)


def _value(words: list[str]) -> object:
    text = " ".join(words)
    if not text:
        return "approved"
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _say(agent: Any, terminal: Terminal, line: str) -> None:
    """Run a command, or send a message to the agent."""
    command, *words = line.split()
    if command == "/steer" and words:
        steered = await agent.steer(line.split(None, 1)[1], idempotency_key=str(uuid.uuid4()))
        turn = steered["turn"]
        print(f"steered to {turn}" if turn else "queued: no turn takes it, it is answered next", flush=True)
    elif command == "/signal" and words:
        resolved = await agent.signal(words[0], _value(words[1:]))
        print(f"resolved {resolved}" if resolved else f"nothing waits for {words[0]}", flush=True)
    elif command == "/pending":
        print(json.dumps(await agent.pending(), indent=2), flush=True)
    elif command == "/status":
        print(json.dumps(await agent.status(), indent=2), flush=True)
    elif command == "/stop":
        stopped = await agent.stop()
        print(f"stopped {stopped}" if stopped else "nothing to stop", flush=True)
    elif command == "/clear":
        await agent.clear()
        print("cleared", flush=True)
    elif command.startswith("/"):
        print(USAGE, end="", flush=True)
    else:
        terminal.sending(line)
        try:
            await agent.send(line)
        except BaseException:
            terminal.unsent(line)
            raise


async def chat(target: str, ingress: str | None) -> None:
    """Talk with the agent ``target`` (``<agent>/<key>``) through Restate's ingress at ``ingress``."""
    from restate.agents.client import AgentClient  # pylint: disable=import-outside-toplevel
    from restate.client import create_client  # pylint: disable=import-outside-toplevel

    name, _, key = target.partition("/")
    async with create_client(ingress) as restate:
        agent = AgentClient(restate, name, key)
        terminal = Terminal(name)

        async def following() -> None:
            try:
                async for change in agent.follow():
                    terminal.show(change)
            except Exception as error:  # said, rather than following silently no more
                print(f"Following the conversation failed: {error}", flush=True)

        shown = asyncio.ensure_future(following())
        print(USAGE, end="", flush=True)
        typed = _lines()
        try:
            while line := await typed.get():
                if not line.strip():
                    continue
                try:
                    await _say(agent, terminal, line.strip())
                except Exception as error:  # Restate's answer, shown rather than ending the chat
                    print(f"Restate answered {error}", flush=True)
        finally:
            shown.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shown


def _lines() -> asyncio.Queue[str]:
    """Return the lines typed, then ``""`` at the end (Ctrl-D).

    They are read on a daemon thread: Ctrl-C leaves at once, while ``asyncio.to_thread``'s thread would be waited for,
    still reading a line.
    """
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str] = asyncio.Queue()

    def read() -> None:
        line = None
        while line != "":
            line = sys.stdin.readline()
            try:
                loop.call_soon_threadsafe(lines.put_nowait, line)
            except RuntimeError:  # the chat has ended, and its loop is closed
                return

    threading.Thread(target=read, name="restate-agents-chat-input", daemon=True).start()
    return lines


def _target(text: str) -> str:
    name, slash, key = text.partition("/")
    if not (name and slash and key):
        raise argparse.ArgumentTypeError(f"{text!r} is not <agent>/<key>, such as assistant/alice")
    return text


def main(argv: list[str] | None = None) -> None:
    """Run the command line: ``python -m restate.agents chat <agent>/<key> [--ingress URL]``."""
    parser = argparse.ArgumentParser(prog="python -m restate.agents", description="Talk with Restate agents.")
    commands = parser.add_subparsers(dest="command", required=True)
    talk = commands.add_parser("chat", help="talk with an agent, and follow its conversation")
    talk.add_argument("target", type=_target, help="the agent and the conversation's key, such as assistant/alice")
    talk.add_argument(
        "--ingress",
        default=None,
        help="Restate's ingress URL (default: RESTATE_INGRESS_KEY's, else http://localhost:8080)",
    )
    arguments = parser.parse_args(argv)
    ingress = arguments.ingress or (None if os.environ.get("RESTATE_INGRESS_KEY") else "http://localhost:8080")
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl-C leaves, as Ctrl-D does
        asyncio.run(chat(arguments.target, ingress))


if __name__ == "__main__":
    main()
