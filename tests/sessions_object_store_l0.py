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
"""``RESTATE_SQL_L0_BYTES``: how many bytes of packs a database keeps in Restate
(L0) before a checkpoint moves them to the object store, here a directory.

``0`` puts every checkpoint's pack in the object store, however small the
database. The variable is read at the process's first open, so each scenario
runs in a process of its own, with the variable set before any; the page cache
is off, so every session reads cold. The scenarios are strings rather than
files because ``just test`` collects every ``.py`` under ``tests/``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HEADER = """
import asyncio, hashlib, json, os
from pathlib import Path

from restate.sessions._database import open
from sessions_database import Backend, listing


def files(path):
    if not path.exists():
        return []
    return [str(f.relative_to(path)) for f in path.rglob("*") if f.is_file()]


def store(backend):
    request = backend.request()
    request.keys = listing(backend)
    return request


# The keys that hold no pack: the head, the tail's entries and the manifests.
def no_pack(key):
    return key == "v22/head" or key.startswith(("v22/tail/", "v22/manifest/"))
"""

SMALL = (
    HEADER
    + """
async def main():
    # Its directory: the namespace, escaped to one segment.
    namespace = "Ledger/small"
    packs = Path(os.environ["PACKS"]) / "_4cedger_2fsmall"
    backend = Backend()

    written = hashlib.sha256()
    db = await open(namespace, store(backend))
    await db.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    await db.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
    await db.execute("INSERT INTO counter VALUES (0)")
    # 2 MiB that does not compress, 64 KiB a commit: a checkpoint at each MiB
    # of tail, far under the 4 MiB L0 would hold by default.
    for i in range(32):
        data = os.urandom(64 << 10)
        written.update(data)
        await db.execute("INSERT INTO blobs VALUES (?, ?)", [i, data])
    # Then small commits: a checkpoint at each 128 of them, the second of
    # which drops the tail that held the blobs.
    for _ in range(300):
        await db.execute("UPDATE counter SET n = n + 1")
    await db.save()

    promoted = files(packs)
    assert promoted, "a pack in the object store"
    assert all(f.startswith("l1/") for f in promoted), f"only L1's packs: {promoted}"
    in_l1 = sum((packs / f).stat().st_size for f in promoted)
    assert in_l1 >= 2 << 20, f"{in_l1} bytes in the object store"

    # Restate holds no pack, only the head, two manifests and the tail since
    # the checkpoint before the head's: here some 170 counter updates.
    assert not backend.unconfirmed, "every write synced"
    kept = sorted(key for key in backend.stored if not no_pack(key))
    assert kept == [], f"no pack in Restate: {kept}"
    in_restate = sum(len(v) for v in backend.stored.values())
    assert in_restate < 64 << 10, f"{in_restate} bytes in Restate"

    # A cold session reads every row from the object store, and the tail.
    again = await open(namespace, store(backend))
    read = hashlib.sha256()
    for (data,) in await again.values("SELECT data FROM blobs ORDER BY id"):
        read.update(data)
    assert read.hexdigest() == written.hexdigest()
    assert await again.values("SELECT n FROM counter") == [[300]]
    # So does a reader beside it.
    reader = await open(namespace, backend.request(), reader=True)
    assert await reader.values("SELECT count(*), sum(length(data)) FROM blobs") == [[32, 32 * (64 << 10)]]
    await reader.close()
    await again.close()


asyncio.run(main())
"""
)

# One process's session of a database whose Restate state is a file: it opens
# the database, checks every row it reads, adds `ADD` rows (1 KiB each that
# does not compress, a commit each), stores the state back, and prints as JSON
# the rows it read, or the open's error and the one after it.
SESSION = (
    HEADER
    + """
state = Path(os.environ["STATE"])


def row(id):
    # Row `id`'s value, the same in every process.
    return b"".join(hashlib.sha256(f"{id}/{n}".encode()).digest() for n in range(32))


async def main():
    backend = Backend()
    if state.exists():
        backend.stored = {k: bytes.fromhex(v) for k, v in json.loads(state.read_text()).items()}

    try:
        db = await open(os.environ["NAMESPACE"], store(backend))
    except Exception as error:  # noqa: BLE001
        # And what the open after it does.
        try:
            await open(os.environ["NAMESPACE"], store(backend))
            again = "opened"
        except Exception as second:  # noqa: BLE001
            again = str(second)
        print(json.dumps({"error": str(error), "again": again}))
        return

    await db.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    rows = await db.values("SELECT id, data FROM rows ORDER BY id")
    for n, (id, data) in enumerate(rows):
        assert id == n and data == row(n), f"row {n} does not read back as written"
    for id in range(len(rows), len(rows) + int(os.environ["ADD"])):
        await db.execute("INSERT INTO rows VALUES (?, ?)", [id, row(id)])
    await db.save()

    assert not backend.unconfirmed, "every write synced"
    state.write_text(json.dumps({k: v.hex() for k, v in backend.stored.items()}))
    print(json.dumps({"rows": len(rows), "keys": sorted(backend.stored), "l1": sorted(files(Path(os.environ["PACKS"]) / os.environ["DIR"]))}))


asyncio.run(main())
"""
)


def run(scenario: str, **env: str):
    """`scenario` in a process of its own, with `env` over this one's."""
    tests = Path(__file__).parent
    ran = subprocess.run(
        [sys.executable, "-c", scenario],
        env={
            **os.environ,
            **env,
            "RESTATE_SQL_CACHE": "off",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(tests), os.environ.get("PYTHONPATH")])),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return ran.stdout


def test_with_l0_bytes_zero_a_database_far_under_4_mib_keeps_every_pack_in_the_object_store(tmp_path: Path):
    packs = tmp_path / "packs"
    run(
        SMALL,
        RESTATE_SQL_OBJECT_STORE=f"file://{packs}",
        RESTATE_SQL_L0_BYTES="0",
        PACKS=str(packs),
    )


def test_each_process_may_open_a_database_with_an_l0_bytes_of_its_own(tmp_path: Path):
    packs = tmp_path / "packs"
    state = tmp_path / "ledger-b.json"

    def session(add: int, l0: str):
        out = json.loads(
            run(
                SESSION,
                RESTATE_SQL_OBJECT_STORE=f"file://{packs}",
                RESTATE_SQL_L0_BYTES=l0,
                PACKS=str(packs),
                DIR="_4cedger_2fb",
                NAMESPACE="Ledger/b",
                STATE=str(state),
                ADD=str(add),
            )
        )
        in_l0 = [k for k in out["keys"] if k.startswith("v22/pack/l0/")]
        return out["rows"], in_l0, out["l1"]

    # The default, 4 MiB: 300 KiB of packs stays in Restate. An empty value
    # is an unset one.
    rows, in_l0, in_l1 = session(300, "")
    assert rows == 0
    assert in_l0, "packs in L0"
    assert in_l1 == []

    # 0: the next checkpoint moves all L0 holds to the object store, with its
    # own pack, and the one after it deletes L0's packs.
    rows, in_l0, in_l1 = session(300, "0")
    assert rows == 300
    assert in_l0 == [], f"no pack in L0: {in_l0}"
    assert in_l1, "packs in L1"

    # Raised: checkpoints go to Restate again, and what they did not rewrite
    # stays in the object store, where it is read.
    rows, in_l0, in_l1 = session(300, "64MiB")
    assert rows == 600
    assert in_l0, "packs in L0 again"
    assert in_l1, "packs in L1 still"

    # And back to the default, which reads both tiers.
    rows, _, _ = session(0, "")
    assert rows == 900


def test_an_l0_bytes_that_is_not_a_size_fails_every_open_with_its_message(tmp_path: Path):
    packs = tmp_path / "packs"
    # With an object store, or without one, where it would change nothing.
    for value, store in [("4MB", f"file://{packs}"), ("-1", f"file://{packs}"), ("1.5MiB", "")]:
        out = json.loads(
            run(
                SESSION,
                RESTATE_SQL_OBJECT_STORE=store,
                RESTATE_SQL_L0_BYTES=value,
                PACKS=str(packs),
                DIR="_4cedger_2fc",
                NAMESPACE="Ledger/c",
                STATE=str(tmp_path / "unopened.json"),
                ADD="1",
            )
        )
        message = f"RESTATE_SQL_L0_BYTES={value}: not a size (bytes, or with KiB, MiB or GiB)"
        assert out == {"error": message, "again": message}
