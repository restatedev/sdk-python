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
"""A database past L0 (4 MiB compressed): its checkpoints promote to the object
store ``RESTATE_SQL_OBJECT_STORE`` names, here a directory, and read back from it.

The variable is read at the process's first open, so the scenario runs in a
process of its own, with the variable set before any; the page cache is off,
so every session reads cold.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIO = """
import asyncio, hashlib, os
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


async def main():
    # Its directory: the namespace, escaped to one segment.
    namespace = "Ledger/a%20b"
    packs = Path(os.environ["PACKS"]) / "_4cedger_2fa_2520b"
    backend = Backend()

    written = hashlib.sha256()
    db = await open(namespace, store(backend))
    await db.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    # 12 MiB that does not compress, 256 KiB a commit.
    for i in range(48):
        data = os.urandom(256 << 10)
        written.update(data)
        await db.execute("INSERT INTO blobs VALUES (?, ?)", [i, data])
    await db.save()

    promoted = files(packs)
    assert promoted, "a pack in the object store"
    assert all(f.startswith("l1/") for f in promoted), f"only L1's packs: {promoted}"
    # Restate keeps no more than L0's few MiB, and the head, tail and manifests.
    in_restate = sum(len(v) for v in backend.stored.values())
    assert in_restate < 8 << 20, f"{in_restate} bytes in Restate"

    # A cold session reads every row, from both tiers.
    again = await open(namespace, store(backend))
    read = hashlib.sha256()
    for (data,) in await again.values("SELECT data FROM blobs ORDER BY id"):
        read.update(data)
    assert read.hexdigest() == written.hexdigest()
    # So does a reader beside it.
    reader = await open(namespace, backend.request(), reader=True)
    assert await reader.values("SELECT count(*) FROM blobs") == [[48]]
    await reader.close()

    # Rewriting it all promotes again; the packs no checkpoint needs go.
    for i in range(48):
        await again.execute("UPDATE blobs SET data = ? WHERE id = ?", [os.urandom(256 << 10), i])
    await again.save()
    stored = sum((packs / f).stat().st_size for f in files(packs))
    assert stored < 40 << 20, f"{stored} bytes in the object store for a 12 MiB database"


asyncio.run(main())
"""


def test_a_database_past_l0_promotes_its_packs_to_the_object_store_and_reads_them_back(tmp_path: Path):
    packs = tmp_path / "packs"
    tests = Path(__file__).parent
    env = {
        **os.environ,
        "RESTATE_SQL_OBJECT_STORE": f"file://{packs}",
        "RESTATE_SQL_CACHE": "off",
        "PACKS": str(packs),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(tests), os.environ.get("PYTHONPATH")])),
    }
    ran = subprocess.run([sys.executable, "-c", SCENARIO], env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_an_object_store_the_build_cannot_serve_fails_the_open_with_its_message():
    env = {
        **os.environ,
        "RESTATE_SQL_OBJECT_STORE": "memory:///",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).parent), os.environ.get("PYTHONPATH")])),
    }
    scenario = """
import asyncio
from restate.sessions._database import open
from sessions_database import Backend

async def main():
    try:
        await open("Ledger/a", Backend().request())
    except Exception as error:
        assert "RESTATE_SQL_OBJECT_STORE=memory:///" in str(error), error
    else:
        raise AssertionError("the open succeeded")

asyncio.run(main())
"""
    ran = subprocess.run([sys.executable, "-c", scenario], env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr


SMALL = """
import asyncio, os, re
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


async def main():
    packs = Path(os.environ["PACKS"]) / "_4cedger_2fsmall"
    backend = Backend()
    db = await open("Ledger/small", store(backend))
    await db.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    await db.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
    await db.execute("INSERT INTO counter VALUES (0)")
    # 2 MiB that does not compress, 64 KiB a commit, then small commits: a checkpoint at each 128 of them.
    for i in range(32):
        await db.execute("INSERT INTO blobs VALUES (?, ?)", [i, os.urandom(64 << 10)])
    for _ in range(300):
        await db.execute("UPDATE counter SET n = n + 1")
    await db.save()

    packs_in_restate = [k for k in backend.stored if not re.match(r"^v22/(head$|tail/|manifest/)", k)]
    print("L1", len([f for f in files(packs) if f.startswith("l1/")]))
    print("OTHER", len([f for f in files(packs) if not f.startswith("l1/")]))
    print("RESTATE", len(packs_in_restate))

    again = await open("Ledger/small", store(backend))
    assert await again.values("SELECT n FROM counter") == [[300]]
    assert await again.values("SELECT count(*) FROM blobs") == [[32]]

asyncio.run(main())
"""


def small_database(tmp_path: Path, **variables: str) -> dict[str, int]:
    """Run ``SMALL`` in a process of its own with ``variables`` (an empty value unsets one): the packs it left in the
    object store's ``l1/``, elsewhere in it, and in Restate."""
    env = {
        **os.environ,
        "RESTATE_SQL_OBJECT_STORE": f"file://{tmp_path / 'packs'}",
        "RESTATE_SQL_CACHE": "off",
        "RESTATE_SQL_L0_BYTES": "",
        "PACKS": str(tmp_path / "packs"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).parent), os.environ.get("PYTHONPATH")])),
        **variables,
    }
    ran = subprocess.run([sys.executable, "-c", SMALL], env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return {name.lower(): int(count) for name, count in (line.split() for line in ran.stdout.splitlines())}


def test_l0_bytes_of_zero_keeps_every_pack_of_a_small_database_in_the_object_store(tmp_path: Path):
    kept = small_database(tmp_path, RESTATE_SQL_L0_BYTES="0")
    assert kept["l1"] > 0, kept
    # Restate holds no pack, only the head, the tail and the manifests.
    assert kept["other"] == 0 and kept["restate"] == 0, kept


def test_without_l0_bytes_a_small_database_keeps_its_packs_in_restate(tmp_path: Path):
    kept = small_database(tmp_path)
    assert kept["l1"] == 0 and kept["restate"] > 0, kept


@pytest.mark.parametrize("value,store", [("4MB", True), ("-1", True), ("1.5MiB", False)])
def test_an_l0_bytes_that_is_not_a_size_fails_every_open_with_its_message(tmp_path: Path, value: str, store: bool):
    # With an object store, or without one, where it would change nothing.
    env = {
        **os.environ,
        "RESTATE_SQL_L0_BYTES": value,
        "RESTATE_SQL_OBJECT_STORE": f"file://{tmp_path}" if store else "",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).parent), os.environ.get("PYTHONPATH")])),
    }
    scenario = """
import asyncio, os
from restate.sessions._database import open
from sessions_database import Backend

async def main():
    expected = f"RESTATE_SQL_L0_BYTES={os.environ['RESTATE_SQL_L0_BYTES']}: not a size (bytes, or with KiB, MiB or GiB)"
    for _ in range(2):
        try:
            await open("Ledger/c", Backend().request())
        except Exception as error:
            assert expected in str(error), error
        else:
            raise AssertionError("the open succeeded")

asyncio.run(main())
"""
    ran = subprocess.run([sys.executable, "-c", scenario], env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
