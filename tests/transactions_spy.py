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
"""A database session that records the transactions of what runs over it: the tables each wrote, in order."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import contextlib
import re
import typing
from collections.abc import AsyncIterator, Callable
from typing import Any

from restate.sessions import Database, DatabaseWriter

WRITE = re.compile(r'(?:INSERT OR REPLACE INTO|INSERT OR IGNORE INTO|INSERT INTO|UPDATE|DELETE FROM)\s+"?(\w+)', re.I)


class _Recorder:
    """A transaction that records the statements it runs, as the table and the parameters of each."""

    def __init__(self, tx: DatabaseWriter, statements: list[tuple[str, list[Any]]]) -> None:
        self._tx = tx
        self._statements = statements

    async def execute(self, sql: str, params: Any = None) -> Any:
        found = WRITE.match(sql)
        if found:
            self._statements.append((found.group(1), list(params or [])))
        return await self._tx.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tx, name)


class Spy:
    """Passes everything to ``db``, and records each committed transaction as its statements.

    ``fail``, if set, is asked for the tables of each transaction when its body ends: if it says so, the transaction
    fails, as a commit that the database refused, and rolls back.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.commits: list[list[tuple[str, list[Any]]]] = []
        self.fail: Callable[[list[str]], bool] | None = None

    @property
    def tables(self) -> list[list[str]]:
        """The tables each committed transaction wrote, in the order they were first written."""
        return [list(dict.fromkeys(table for table, _ in commit)) for commit in self.commits]

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        statements: list[tuple[str, list[Any]]] = []
        async with self.db.transaction() as tx:
            yield _Recorder(tx, statements)
            if self.fail is not None and self.fail(list(dict.fromkeys(table for table, _ in statements))):
                raise RuntimeError("the commit failed")
        self.commits.append(statements)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db, name)

    @property
    def database(self) -> Database:
        """This spy, as the database that a saver takes."""
        return typing.cast(Database, self)
