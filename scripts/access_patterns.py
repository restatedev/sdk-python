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
"""Measure what a conversation costs in Restate state, turn after turn.

Drives a conversation of an agent whose framework is the tests' made-up one
(``tests/agents_conversation.py``'s ``Items``) through ``answer_once``, over
the tests' fake context (``tests/sessions_object.py``'s ``Host``), and meters
every request the key's database makes to the object's state: the reads
(``get``, in round trips), the writes (``set``, ``clear``) and the syncs,
classified by the storage's key kinds (head, tail, manifest, pack). It meters
the SQL too: statements, and the rows and bytes they read. With ``--follow``,
after each turn a client following the conversation reads what changed (the
``messages`` view from its cursor), metered the same way.

It measures any checkout of this repository with the same API, so that two
versions can be compared. Each turn runs warm, in one process, as a key
answered by the same container turn after turn. ``--cold-at`` measures the
turn after each of the given turns in a new process (an empty page cache,
the conversation as stored), as a key's first invocation in a new
container; with ``--follow``, a client reading the given turn, the latest
stored, from the cursor before it, in a process of its own; and
``--probe`` costs a statement alone, the same way::

    git worktree add ../before <commit>       # and copy the built python/restate/_internal*.so there
    python scripts/access_patterns.py --tree ../before --turns 1000 --follow --cold-at 100,500,1000 > before.jsonl
    python scripts/access_patterns.py --tree . --turns 1000 --follow --cold-at 100,500,1000 > after.jsonl
    python scripts/access_patterns.py --tree . --turns 500 --cold-at 500 --probe "SELECT COUNT(*) FROM restate_agent_turns"
    python scripts/access_patterns.py --report before.jsonl after.jsonl

The text of prompts, replies and tool results is random words, about as
compressible as prose. Round trips are counted as the batches of reads
issued together: a read issued while none is outstanding starts a new one.
The fake store answers at once, so they are what a real store would wait
for, not a timing. ``RESTATE_SQL_CACHE=off`` measures without any cache,
which also re-reads within a session, unlike a new process.
"""

import argparse
import asyncio
import itertools
import json
import pickle
import random
import subprocess
import sys
import tempfile
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("head", "tail", "manifest", "pack", "other")


def kind_of(key: str) -> str:
    for kind in KINDS[1:-1]:
        if f"/{kind}/" in key:
            return kind
    return "head" if key.endswith("/head") else "other"


@dataclass
class Meter:
    """What one invocation asked of the object's state and of its database."""

    gets: int = 0
    get_bytes: int = 0
    rounds: int = 0
    sets: int = 0
    set_bytes: int = 0
    clears: int = 0
    syncs: int = 0
    statements: int = 0
    rows: int = 0
    row_bytes: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)
    by_statement: dict[str, list[int]] = field(default_factory=dict)
    _pending: int = 0
    _statement: str = "open"

    def got(self, key: str, value: bytes | None) -> None:
        self.gets += 1
        self.get_bytes += len(value or b"")
        _add(self.by_statement, self._statement, len(value or b""))
        _add(self.by_kind, f"get:{kind_of(key)}", len(value or b""))

    def put(self, key: str, value: bytes) -> None:
        self.sets += 1
        self.set_bytes += len(value)
        _add(self.by_kind, f"set:{kind_of(key)}", len(value))

    def cleared(self, key: str) -> None:
        self.clears += 1
        _add(self.by_kind, f"clear:{kind_of(key)}", 0)

    def row(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _add(table: dict[str, list[int]], name: str, size: int) -> None:
    """Count one more request, and its bytes, under ``name``."""
    counts = table.setdefault(name, [0, 0])
    counts[0] += 1
    counts[1] += size


METER = Meter()


_LETTERS = "etaoinshrdlcumwfgypbvkjxqz"
_LETTER_WEIGHTS = [12.7, 9.1, 8.2, 7.5, 7.0, 6.7, 6.3, 6.1, 6.0, 4.3, 4.0, 2.8, 2.8, 2.4, 2.4, 2.2, 2.0, 2.0, 1.9]
_LETTER_WEIGHTS += [1.5, 1.0, 0.8, 0.15, 0.15, 0.1, 0.07]
_LENGTHS = [1, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 7, 8, 9, 10]


def _word(i: int) -> str:
    pick = random.Random(f"word {i}")
    return "".join(pick.choices(_LETTERS, _LETTER_WEIGHTS, k=pick.choice(_LENGTHS)))


_WORDS = [_word(i) for i in range(5000)]
# Cumulative, once: given weights, random.choices would add up all 5,000 for every word.
_WORD_WEIGHTS = list(itertools.accumulate(1 / rank for rank in range(1, 5001)))


def text(seed: str, chars: int) -> str:
    """Return about ``chars`` characters of words, the same for the same ``seed``.

    Words of English letter frequencies, picked by Zipf's law: a block of
    1,500 characters compresses (zlib) about 2.0x, as one of these docs'
    prose does 1.9x.
    """
    pick = random.Random(seed)
    words: list[str] = []
    size = 0
    while size < chars:
        words.append(pick.choices(_WORDS, cum_weights=_WORD_WEIGHTS)[0])
        size += len(words[-1]) + 1
    return " ".join(words)


def invocation_ids(seed: int) -> typing.Iterator[str]:
    """Yield invocation ids as random as Restate's (the suffix of a prompt's id, and so its order in an index)."""
    pick = random.Random(seed)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    while True:
        yield "".join(pick.choices(alphabet, k=26))


def size_of(value: Any) -> int:
    if isinstance(value, (bytes, str)):
        return len(value)
    return 8 if value is not None else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", default=".", help="the checkout to measure")
    parser.add_argument("--turns", type=int, default=200)
    parser.add_argument("--calls", type=int, default=2, help="tool calls per turn, made in one reply")
    parser.add_argument("--result", type=int, default=1500, help="characters of each tool result")
    parser.add_argument("--window", default="1", help="the agent's window: a number, or all")
    parser.add_argument("--budget", type=int, default=None, help="the agent's budget, in tokens")
    parser.add_argument("--follow", action="store_true", help="after each turn, read the changes from a cursor")
    parser.add_argument("--probe", action="append", default=[], help="cost this SQL alone, at each --cold-at")
    parser.add_argument("--cold-at", default="", help="turns after which to measure the next turn in a new process")
    parser.add_argument("--resume", help=argparse.SUPPRESS)
    parser.add_argument("--report", nargs="+", help="summarize these JSONL files instead of measuring")
    args = parser.parse_args()
    if args.report:
        report(args.report)
        return
    cold = sorted(int(n) for n in args.cold_at.split(",") if n)
    if args.resume is None:
        if any(at < 1 or at > args.turns for at in cold):
            parser.error(f"--cold-at takes turns from 1 to --turns ({args.turns}), not {args.cold_at}")
        if args.probe and not cold:
            parser.error("--probe costs statements in a new process after the turns of --cold-at: give some")
    with tempfile.TemporaryDirectory() as saved:
        asyncio.run(measure(args, saved, set(cold)))
        for at in cold:
            # A new process, its page cache empty: the next turn as a key's first in a new container.
            command = [sys.executable, __file__, "--tree", args.tree, "--turns", "1", "--resume", f"{saved}/{at}"]
            command += ["--calls", str(args.calls), "--result", str(args.result), "--window", args.window]
            command += [*(["--budget", str(args.budget)] if args.budget is not None else [])]
            subprocess.run(command, check=True)
            if args.follow:
                subprocess.run([*command[:5], "0", *command[6:], "--follow"], check=True)
            for sql in args.probe:
                # Each statement in a process of its own, so that no other read warmed the cache for it.
                subprocess.run([*command[:5], "0", *command[6:], f"--probe={sql}"], check=True)


async def measure(args: argparse.Namespace, saved: str, save_at: set[int]) -> None:
    tree = Path(args.tree).resolve()
    sys.path[:0] = [str(tree / "python"), str(tree / "tests")]
    # pylint: disable=import-outside-toplevel,import-error
    import agents_conversation
    import sessions_object
    from sessions_database import Backend

    from restate import sessions
    from restate.agents import _conversation, _messages
    from restate.sessions import _database
    from restate.sessions.internal import invoke

    for cls in (_database.Connection, _database.ConnectionTransaction):
        original = cls.query_columns

        # Gets are charged to the statement in flight: the turns here run one statement at a time.
        async def counted(self, sql, params=None, _original=original):
            METER._statement = " ".join(sql.split())[:160]  # pylint: disable=protected-access
            try:
                columns, rows = await _original(self, sql, params)
            finally:
                METER._statement = "between statements"  # pylint: disable=protected-access
            METER.statements += 1
            METER.rows += len(rows)
            METER.row_bytes += sum(size_of(value) for row in rows for value in row)
            return columns, rows

        cls.query_columns = counted

    class Metered(sessions_object.Host):
        def get(self, key: str):
            value = self.backend.request().get(key)
            METER.got(key, value)
            if METER._pending == 0:  # pylint: disable=protected-access
                METER.rounds += 1
            METER._pending += 1  # pylint: disable=protected-access
            future = asyncio.get_running_loop().create_future()

            def answer() -> None:
                METER._pending -= 1  # pylint: disable=protected-access
                future.set_result(value)

            asyncio.get_running_loop().call_soon(answer)
            return future

        def set(self, key: str, value: bytes) -> None:
            METER.put(key, value)
            super().set(key, value)

        def clear(self, key: str) -> None:
            METER.cleared(key)
            super().clear(key)

        async def sync(self) -> None:
            METER.syncs += 1
            await super().sync()

        def view(self) -> "Metered":
            return Metered(self.backend, self.service, self.key, shared=True)

    items = agents_conversation.ITEMS
    new = hasattr(_conversation, "Turn")
    window: Any = "all" if args.window == "all" else int(args.window)
    model_input: list[int] = [0, 0]
    asked = [0]

    async def run(conversation: Any) -> str:
        n = asked[0]
        steps = conversation.turn if new else conversation
        seen = conversation.history() if new else [entry.item for entry in conversation.entries]
        model_input[:] = [len(seen), sum(len(json.dumps(item)) for item in seen)]
        calls = [
            {"id": f"c{n}.{i}", "name": "lookup", "input": {"q": text(f"q{n}.{i}", 40)}} for i in range(args.calls)
        ]
        await steps.append_reply({"role": "assistant", "text": text(f"r{n}", 60), "calls": calls})
        for call in calls:
            await steps.append({"result": call["id"], "output": text(f"o{call['id']}", args.result)})
        answer = text(f"a{n}", 600)
        await steps.append_reply({"role": "assistant", "text": answer})
        return answer

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        if new:
            return await _conversation.answer_once(ctx, text, items, run, window, args.budget)
        return await _conversation.answer_once(ctx, text, items, run)

    @obj.view
    async def follow(ctx: sessions.ViewContext, cursor: int | None) -> Any:
        return await _messages.page(ctx.db, items, {"cursor": cursor} if cursor is not None else {"limit": 50})

    @obj.view
    async def probe(ctx: sessions.ViewContext, sql: str) -> Any:
        return await ctx.db.values(sql)

    backend = Backend()
    cursor, first, cold = None, 1, args.resume is not None
    if args.resume is not None:
        with open(args.resume, "rb") as resumed:
            backend.stored, first, cursor = pickle.load(resumed)
    # Invocation ids, and so prompt ids, as random as Restate's, and each process's its own.
    sessions_object._ids = invocation_ids(first)  # pylint: disable=protected-access
    published = agents_conversation.PUBLISHED
    if args.turns == 0 and args.follow:
        # Only a client following the conversation, as a key's first invocation in a new process: it reads the
        # latest turn, from the cursor before it.
        METER.__init__()  # type: ignore[misc]
        await invoke(obj, "follow", Metered(backend, service="asker").view(), sessions_object.request(), cursor)
        print(json.dumps({"handler": "follow", "turn": first - 1, "cold": cold, **METER.row()}), flush=True)
    for turn in range(first, first + args.turns):
        METER.__init__()  # type: ignore[misc]
        asked[0] = turn
        host = Metered(backend, service="asker", answers=published)
        prompt = text(f"p{turn}", 150)
        await invoke(obj, "ask", host, sessions_object.request(), prompt)
        stored = sum(len(v) for v in backend.stored.values())
        row = {"handler": "ask", "turn": turn, "cold": cold, **METER.row(), "model_items": model_input[0]}
        row |= {"model_chars": model_input[1], "stored_keys": len(backend.stored), "stored_bytes": stored}
        print(json.dumps(row), flush=True)
        before = cursor
        if args.follow:
            METER.__init__()  # type: ignore[misc]
            page = await invoke(obj, "follow", host.view(), sessions_object.request(), cursor)
            cursor = typing.cast(dict, page)["cursor"]
            print(json.dumps({"handler": "follow", "turn": turn, "cold": cold, **METER.row()}), flush=True)
        if turn in save_at:
            with open(f"{saved}/{turn}", "wb") as saving:
                # The cursor before this turn's: a client that follows from it reads this turn.
                pickle.dump((backend.stored, turn + 1, before), saving)
    for sql in args.probe if cold else []:
        METER.__init__()  # type: ignore[misc]
        await invoke(obj, "probe", Metered(backend, service="asker").view(), sessions_object.request(), sql)
        opened = METER.by_statement.get("open", [0, 0])
        beyond = [METER.gets - opened[0], METER.get_bytes - opened[1]]
        print(json.dumps({"handler": "probe", "turn": first - 1, "sql": sql, **METER.row(), "beyond_open": beyond}))


def report(paths: list[str]) -> None:
    """Print, per file: the warm turns at a few lengths of the conversation and their sum, the cold ones, and the probes."""
    columns = ("gets", "rounds", "get_bytes", "sets", "set_bytes", "syncs", "statements", "rows", "row_bytes")
    sampled = {1, 2, 11, 51, 101, 201, 501, 1001}
    for path in paths:
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        for handler in ("ask", "follow"):
            for cold in (False, True):
                mine = [row for row in rows if row["handler"] == handler and row.get("cold") == cold]
                if not mine:
                    continue
                print(f"\n{path}: {handler}, {'cold (a new process)' if cold else 'warm'}")
                print("turn".rjust(5) + "".join(c.rjust(11) for c in columns) + "  model_items stored_bytes")
                last = mine[-1]["turn"]
                for row in mine if cold else [row for row in mine if row["turn"] in sampled | {last}]:
                    extra = f"  {row.get('model_items', ''):>11} {row.get('stored_bytes', ''):>12}"
                    print(str(row["turn"]).rjust(5) + "".join(str(row[c]).rjust(11) for c in columns) + extra)
                if not cold:
                    print("  sum" + "".join(str(sum(row[c] for row in mine)).rjust(11) for c in columns))
        probes = [row for row in rows if row["handler"] == "probe"]
        if probes:
            print(f"\n{path}: statements, each alone in a new process (reads beyond the open)")
            for row in probes:
                gets, read = row["beyond_open"]
                print(f"  after turn {row['turn']:>5}: {gets:4} gets {read:9} bytes  {row['sql']}")


if __name__ == "__main__":
    main()
