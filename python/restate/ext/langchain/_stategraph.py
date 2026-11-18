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
the guarantee to an arbitrary ``StateGraph``: parallel branches, ``Send`` /
map-reduce, and nested parallel subgraphs.

The problem
-----------
LangGraph runs a superstep's nodes concurrently on asyncio. ``ctx.run_typed``
creates a journal command synchronously at call time, so parallel nodes create
commands in I/O-completion order — non-deterministic, and different on replay
(journaled results resolve instantly). Restate matches the journal by position,
so replay mismatches and the invocation cannot recover.

The approach — fully hidden, no user code change
------------------------------------------------
Wrap the handler body in ``with durable_scope(ctx):``. That is the *only* change:
nodes, agents, ``RestateMiddleware()`` and ``restate_context().run_typed(...)``
are all written exactly as usual. Under the scope:

  * ``ServerInvocationContext.run_typed`` is patched (once, lazily) so that when a
    coordinator is active it routes durable ops through a per-invocation
    coordinator instead of journaling them immediately. The context object stays a
    real ``ServerInvocationContext`` (so ``extension_data`` etc. keep working).
  * ``langgraph.pregel._runner.arun_with_retry`` is patched to tag each task with a
    hierarchical, replay-stable key (parent + ``name|path``; the path carries the
    Send index — no task-id UUIDs, which are not replay-stable).

The coordinator batches durable ops and creates each round's commands in stable,
sorted-key order (deterministic journal) while awaiting the actual I/O
concurrently via :func:`restate.gather`.

Usage::

    from restate.ext.langchain import durable_scope

    @svc.handler()
    async def run(ctx: restate.Context, req: Req) -> Resp:
        with durable_scope(ctx):
            return await graph.ainvoke(state)   # plain nodes, middleware, tools

.. warning::
   **RFC / draft.** Task keying is obtained by monkeypatching a *private* LangGraph
   function (``langgraph.pregel._runner.arun_with_retry``). Brittle across LangGraph
   versions; a merge-ready version wants a supported runner/executor hook.

.. note::
   Limitations: (1) round batching imposes a cross-branch barrier per durable-op
   round, so heterogeneous per-branch latencies run slower than native; (2) assumes
   one durable op in flight per task at a time — a node that fires multiple durable
   ops *concurrently* (intra-node ``gather``) needs op-level sub-keys, not handled;
   (3) a routed ``run_typed`` returns a coroutine rather than a ``RestateDurableFuture``,
   so code that starts an op without awaiting it (to combine via ``restate.gather``)
   must run outside ``durable_scope``.
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
    __slots__ = ("make_future", "event", "value", "error")

    def __init__(self, make_future: Callable[[], Any]) -> None:
        self.make_future = make_future  # () -> RestateDurableFuture, called at flush
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
    round's commands in sorted-key order, then awaiting them concurrently."""

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
                fut = p.make_future()  # sync create -> deterministic order
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

    async def submit(self, full_key: str, make_future: Callable[[], Any]) -> Any:
        p = _Pending(make_future)
        self.pending[full_key] = p
        self._try_flush()
        await p.event.wait()
        if p.error is not None:
            raise p.error
        return p.value


# ---------------------------------------------------------------------------
# Patches + activation. No context wrapper: the real ServerInvocationContext's
# run_typed is patched, so restate_context()/middleware code routes transparently.
# ---------------------------------------------------------------------------

# (coordinator, full_key) for the task currently executing on this async task.
_current: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("_restate_lg_current", default=None)
# The active invocation's coordinator (set by durable_scope in the handler).
_active_coord: contextvars.ContextVar[Optional[_Coordinator]] = contextvars.ContextVar(
    "_restate_lg_active_coord", default=None
)


def coordinator_active() -> bool:
    """True if a StateGraph determinism coordinator is active on the current
    context. RestateMiddleware uses this to skip its tool-call turnstile, which
    is redundant with — and would deadlock against — the coordinator."""
    return _active_coord.get() is not None


_installed = False
_auto_enabled = False
_orig_run_typed: Any = None


def install() -> None:
    """Idempotently install the routing patches (LangGraph task runner + Restate
    ``run_typed``). Both are no-ops unless a coordinator is active on the current
    context — so this alone changes nothing until either :class:`durable_scope`
    (scoped) or :func:`enable` (global) activates one."""
    global _installed, _orig_run_typed  # pylint: disable=global-statement
    if _installed:
        return
    _installed = True

    import langgraph.pregel._runner as _runner_mod  # pylint: disable=import-outside-toplevel
    from restate.server_context import ServerInvocationContext  # pylint: disable=import-outside-toplevel

    # --- 1. tag each LangGraph task with a hierarchical, replay-stable key ---
    orig_arun = _runner_mod.arun_with_retry

    @functools.wraps(orig_arun)
    async def patched_arun(task, *args, **kwargs):  # type: ignore[no-untyped-def]
        coord = _active_coord.get()
        if coord is None or task is None:
            return await orig_arun(task, *args, **kwargs)
        parent = _current.get()
        parent_key = parent[1] if parent else ""
        full = f"{parent_key}/{task.name}|{task.path}"
        coord.register(full)  # eager: at task start, before any deep await
        token = _current.set((coord, full))
        try:
            return await orig_arun(task, *args, **kwargs)
        finally:
            _current.reset(token)
            coord.checkout(full)

    _runner_mod.arun_with_retry = patched_arun

    # --- 2. route ctx.run_typed through the coordinator when one is active ---
    _orig_run_typed = ServerInvocationContext.run_typed

    def patched_run_typed(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        coord = _active_coord.get()
        cur = _current.get()
        if coord is None or cur is None:
            return _orig_run_typed(self, *args, **kwargs)
        _, full = cur
        # Defer command creation to the coordinator's sorted flush; the flush
        # calls the ORIGINAL run_typed (no recursion) to create the command.
        return coord.submit(full, lambda: _orig_run_typed(self, *args, **kwargs))

    ServerInvocationContext.run_typed = patched_run_typed  # type: ignore[method-assign, assignment]


def enable() -> None:
    """Turn on StateGraph determinism **globally** — call once at app startup and
    every handler activates a fresh per-invocation coordinator automatically. NO
    per-handler code, no ``durable_scope``: existing handlers, ``RestateMiddleware()``
    and ``restate_context().run_typed(...)`` become deterministic as-is.

    Implemented by wrapping the SDK's per-invocation ``invoke_handler`` entry point.
    The coordinator is a no-op for any handler that doesn't run parallel LangGraph
    nodes (the run_typed patch only routes when inside a LangGraph task)."""
    global _auto_enabled  # pylint: disable=global-statement
    install()
    if _auto_enabled:
        return
    _auto_enabled = True

    import restate.server_context as _sc  # pylint: disable=import-outside-toplevel

    # getattr/setattr: invoke_handler is imported into server_context but not
    # re-exported, so plain attribute access trips pyright's private-import check.
    orig_invoke_handler = getattr(_sc, "invoke_handler")

    async def patched_invoke_handler(handler, ctx, in_buffer):  # type: ignore[no-untyped-def]
        token = _active_coord.set(_Coordinator(settle_turns=0))
        try:
            return await orig_invoke_handler(handler=handler, ctx=ctx, in_buffer=in_buffer)
        finally:
            _active_coord.reset(token)

    setattr(_sc, "invoke_handler", patched_invoke_handler)  # server_context.enter() resolves this name


class durable_scope:  # pylint: disable=invalid-name
    """Scoped opt-in alternative to :func:`enable`: ``with durable_scope(ctx):``
    activates a coordinator for just this handler body. Useful if you don't want
    the global :func:`enable`. Redundant (harmless) once :func:`enable` is set."""

    def __init__(self, ctx: Any = None) -> None:
        install()
        self._ctx = ctx  # accepted for a natural call site; not otherwise needed
        self._token: Any = None

    def __enter__(self) -> "durable_scope":
        self._token = _active_coord.set(_Coordinator(settle_turns=0))
        return self

    def __exit__(self, *exc: Any) -> None:
        _active_coord.reset(self._token)


__all__ = ["enable", "durable_scope", "install"]
