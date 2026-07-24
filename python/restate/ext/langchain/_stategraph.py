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
"""Deterministic durability for LangGraph ``StateGraph`` parallelism on Restate.

``RestateMiddleware`` (see ``_middleware.py``) makes a ``create_agent`` agent
durable and linearizes its parallel *tool-call batch*. This module generalizes
the same idea to an arbitrary ``StateGraph``: parallel branches, ``Send`` /
map-reduce, and nested parallel subgraphs.

The problem
-----------
LangGraph runs the nodes of a superstep concurrently on asyncio. ``ctx.run_typed``
synchronously creates a journal command at call time, so parallel nodes create
commands in I/O-completion order — non-deterministic, and different on replay
(journaled results resolve instantly). Restate matches the journal by position, so
replay mismatches raise ``TerminalError`` / journal-mismatch and the invocation
cannot recover.

The approach
------------
``durable_scope(ctx)`` yields a context wrapper; nodes call ``dctx.run_typed(...)``
exactly as usual (no node wrappers, no ``config`` threading). Under the hood a
per-invocation coordinator batches durable ops and creates each round's commands
in a stable, sorted order (deterministic journal) while awaiting the actual I/O
concurrently via :func:`restate.gather`. Task identity comes from LangGraph's
replay-stable ``name``/``path`` metadata, composed hierarchically for nesting.

Usage::

    from restate.ext.langchain import durable_scope

    @svc.handler()
    async def run(ctx: restate.Context, req: Req) -> Resp:
        with durable_scope(ctx) as dctx:
            graph = build_graph(dctx)          # nodes call dctx.run_typed(...)
            return await graph.ainvoke(state)  # no config, no node wrappers

.. warning::
   **RFC / draft.** Membership and per-task keying are obtained by monkeypatching
   a *private* LangGraph function (``langgraph.pregel._runner.arun_with_retry``),
   applied lazily the first time :class:`durable_scope` is entered. This is
   brittle across LangGraph versions; a merge-ready version wants a *supported*
   LangGraph runner/executor hook. See the PR discussion.

.. note::
   Current limitations: (1) round batching imposes a cross-branch barrier per
   durable-op round, so heterogeneous per-branch latencies run slower than native;
   (2) assumes one durable op in flight per task at a time — a node that fires
   multiple durable ops *concurrently* (intra-node ``gather``) needs op-level
   sub-keys, not handled here.
"""

import asyncio
import contextvars
import functools
from typing import Any, Callable, Optional

import restate

# ---------------------------------------------------------------------------
# Coordinator: deterministic command-creation order, concurrent I/O.
# ---------------------------------------------------------------------------


class _Pending:
    __slots__ = ("name", "fn", "ctx", "event", "value", "error")

    def __init__(self, name: str, fn: Callable[[], Any], ctx: Any) -> None:
        self.name, self.fn, self.ctx = name, fn, ctx
        self.event = asyncio.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


def _await_all(futs: list) -> Any:
    """:func:`restate.gather` for real SDK futures; :func:`asyncio.gather` for
    plain awaitables (keeps the coordinator unit-testable without a server)."""
    try:
        from restate.server_context import ServerDurableFuture

        if futs and isinstance(futs[0], ServerDurableFuture):
            return restate.gather(*futs)
    except Exception:  # pylint: disable=broad-except
        pass
    return asyncio.gather(*futs, return_exceptions=True)


class _Coordinator:
    """Nesting-aware coordinator. Flushes a round when every active *leaf* task
    (an active task with no active descendant) has a pending op, creating that
    round's commands in sorted-key order, then awaiting them concurrently.

    With eager registration via the runner hook the full sibling set is known up
    front, so ``settle_turns == 0`` (flush as soon as every active leaf is
    pending). A positive ``settle_turns`` tolerates lazy registration.
    """

    def __init__(self, settle_turns: int = 0) -> None:
        self.settle_turns = settle_turns
        self.active: set[str] = set()
        self.pending: dict[str, _Pending] = {}
        self._flushing = False
        self._flush_scheduled = False

    def register(self, full_key: str) -> None:
        self.active.add(full_key)

    def checkout(self, full_key: str) -> None:
        self.active.discard(full_key)
        self.pending.pop(full_key, None)
        self._try_flush()

    def _leaves(self) -> set[str]:
        act = self.active
        return {k for k in act if not any(o != k and o.startswith(k + "/") for o in act)}

    def _ready_set(self) -> Optional[set[str]]:
        leaves = self._leaves()
        if leaves and all(k in self.pending for k in leaves):
            return leaves
        return None

    def _try_flush(self) -> None:
        if self._flushing or self._flush_scheduled:
            return
        if self._ready_set() is None:
            return
        self._flush_scheduled = True
        asyncio.create_task(self._flush_when_stable())

    async def _flush_when_stable(self) -> None:
        try:
            # settle_turns == 0 (hook installed): flush immediately. Otherwise
            # wait until the ready leaf-set is stable across a few turns so
            # lazily-registering siblings can appear first.
            stable = 0
            prev: Optional[frozenset] = None
            while stable < self.settle_turns:
                if self._flushing:
                    return
                ready = self._ready_set()
                if ready is None:
                    prev, stable = None, 0
                else:
                    snap = frozenset(ready)
                    stable = stable + 1 if snap == prev else 1
                    prev = snap
                await asyncio.sleep(0)
            ready = self._ready_set()
            if self._flushing or ready is None:
                return
            self._flushing = True
            entries = []
            for k in sorted(ready):
                p = self.pending.pop(k)
                fut = p.ctx.run_typed(p.name, p.fn)  # sync create -> deterministic order
                entries.append((p, fut))
            asyncio.create_task(self._drive(entries))
        finally:
            self._flush_scheduled = False

    async def _drive(self, entries: list) -> None:
        try:
            gathered = await _await_all([fut for _, fut in entries])
            is_results = isinstance(gathered, list) and gathered and not hasattr(gathered[0], "__await__")
            for i, (p, fut) in enumerate(entries):
                try:
                    p.value = gathered[i] if is_results else await fut
                    if isinstance(p.value, BaseException):
                        p.error, p.value = p.value, None
                except BaseException as e:  # noqa: BLE001  pylint: disable=broad-except
                    p.error = e
                p.event.set()
        finally:
            self._flushing = False
            self._try_flush()

    async def submit(self, full_key: str, ctx: Any, name: str, fn: Callable[[], Any]) -> Any:
        p = _Pending(name, fn, ctx)
        self.pending[full_key] = p
        self._try_flush()
        await p.event.wait()
        if p.error is not None:
            raise p.error
        return p.value


# ---------------------------------------------------------------------------
# Runner hook + context wrapper (the integration surface).
# ---------------------------------------------------------------------------

# (coordinator, full_key) for the task currently executing on this async task.
_current: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("_restate_lg_current", default=None)
# The active invocation's coordinator (set by durable_scope in the handler).
_active_coord: contextvars.ContextVar[Optional[_Coordinator]] = contextvars.ContextVar(
    "_restate_lg_active_coord", default=None
)

_installed = False


def install() -> None:
    """Idempotently patch LangGraph's task runner. Called lazily by
    :class:`durable_scope`, so the monkeypatch is only applied when the feature
    is actually used (not merely on import)."""
    global _installed  # pylint: disable=global-statement
    if _installed:
        return
    _installed = True

    import langgraph.pregel._runner as _runner_mod  # pylint: disable=import-outside-toplevel

    orig_arun = _runner_mod.arun_with_retry

    @functools.wraps(orig_arun)
    async def patched_arun(task, *args, **kwargs):  # type: ignore[no-untyped-def]
        coord = _active_coord.get()
        if coord is None or task is None:
            return await orig_arun(task, *args, **kwargs)
        parent = _current.get()
        parent_key = parent[1] if parent else ""
        # Hierarchical, replay-stable key: parent + this task's name + path
        # (path carries the Send index). No task-id UUIDs (those aren't stable).
        full = f"{parent_key}/{task.name}|{task.path}"
        coord.register(full)  # eager: at task start, before any deep await
        token = _current.set((coord, full))
        try:
            return await orig_arun(task, *args, **kwargs)
        finally:
            _current.reset(token)
            coord.checkout(full)

    _runner_mod.arun_with_retry = patched_arun


class DurableContext:
    """Wraps a Restate ``Context`` so ``run_typed`` routes through the active
    coordinator. Everything else delegates to the real context — node code calls
    ``ctx.run_typed`` exactly as normal and the determinism is invisible."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def run_typed(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        cur = _current.get()
        if cur is None:
            return self._ctx.run_typed(name, fn, *args, **kwargs)
        coord, full = cur
        bound = fn if not (args or kwargs) else functools.partial(fn, *args, **kwargs)
        return coord.submit(full, self._ctx, name, bound)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


class durable_scope:  # pylint: disable=invalid-name
    """Handler helper. ``with durable_scope(ctx) as dctx:`` installs the runner
    hook (lazily, idempotent) and a fresh per-invocation coordinator, and yields
    a durable-wrapped context. Build the graph with ``dctx`` and invoke normally
    — no node wrapping, no ``config``."""

    def __init__(self, ctx: Any) -> None:
        install()
        self.ctx = ctx
        self.coord = _Coordinator(settle_turns=0)
        self._token: Any = None

    def __enter__(self) -> DurableContext:
        self._token = _active_coord.set(self.coord)
        return DurableContext(self.ctx)

    def __exit__(self, *exc: Any) -> None:
        _active_coord.reset(self._token)


__all__ = ["durable_scope", "DurableContext", "install"]
