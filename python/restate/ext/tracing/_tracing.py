"""Restate OTEL tracer wrapper that flattens all spans under the Restate trace.

Wraps any tracer so that every span — regardless of framework nesting — becomes a
direct child of the Restate invocation trace.  Works transparently with any
OTEL-integrated agent framework (Google ADK, Pydantic AI, OpenAI Agents, etc.).

Usage:
    tracer = RestateTracer(trace_api.get_tracer("my-tracer"))
    # All spans created by this tracer are flat children of the Restate trace.
"""

from typing import Optional, Iterator, Sequence

from opentelemetry import context as context_api
from opentelemetry.trace import INVALID_SPAN, Span, SpanKind, Tracer, TracerProvider, use_span, Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util import types
from opentelemetry.util._decorator import _agnosticcontextmanager
from restate.server_context import (
    current_context,
    get_extension_data,
    set_extension_data,
    restate_context_is_replaying,
)

_propagator = TraceContextTextMapPropagator()
_EXTENSION_KEY = "otel_span_cleanup"


class _SpanCleanup:
    """Stored as Restate extension data.  ``__close__`` is called automatically
    when the Restate invocation context is torn down, ending any spans that
    were never properly closed (e.g. because the handler raised)."""

    def __init__(self):
        self._spans = []

    def track(self, span):
        self._spans.append(span)

    def __close__(self):
        for span in self._spans:
            if span.is_recording():
                span.end()
        self._spans.clear()


class RestateTracer(Tracer):
    """Wraps a ``Tracer`` to always parent spans under the Restate root context.

    During Restate replay, returns no-op spans to avoid duplicates."""

    def __init__(self, tracer):
        self._tracer = tracer

    @staticmethod
    def _get_root_context():
        """Extract the Restate trace parent from the current handler, or None."""
        ctx = current_context()
        if ctx is None:
            raise Exception("You are not in a Restate handler")
        return _propagator.extract(ctx.request().attempt_headers)

    def start_span(
        self,
        name: str,
        context: Optional[context_api.Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: types.Attributes = None,
        links: Optional[Sequence[Link]] = None,
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        if restate_context_is_replaying.get(False):
            return INVALID_SPAN
        root = self._get_root_context()
        if root is not None:
            context = root
        span = self._tracer.start_span(
            name,
            context=context,
            kind=kind,
            attributes=attributes,
            links=links,
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        )
        self._track_span(span)
        return span

    @_agnosticcontextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Optional[context_api.Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: types.Attributes = None,
        links: Optional[Sequence[Link]] = None,
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        if restate_context_is_replaying.get(False):
            with use_span(INVALID_SPAN, end_on_exit=False) as span:
                yield span
        else:
            root = self._get_root_context()
            if root is not None:
                context = root
            with self._tracer.start_as_current_span(
                name,
                context=context,
                kind=kind,
                attributes=attributes,
                links=links,
                start_time=start_time,
                record_exception=record_exception,
                set_status_on_exception=set_status_on_exception,
                end_on_exit=end_on_exit,
            ) as span:
                yield span

    @staticmethod
    def _track_span(span):
        """Register a span for cleanup when the Restate invocation ends."""
        ctx = current_context()
        if ctx is None:
            return
        cleanup = get_extension_data(ctx, _EXTENSION_KEY)
        if cleanup is None:
            cleanup = _SpanCleanup()
            set_extension_data(ctx, _EXTENSION_KEY, cleanup)
        cleanup.track(span)

    def __getattr__(self, name):
        return getattr(self._tracer, name)


class RestateTracerProvider(TracerProvider):
    """Wraps a ``TracerProvider`` to return ``RestateTracer`` instances.

    Pass this to instrumentors (e.g. ``GoogleADKInstrumentor``) so that every
    span they create is automatically parented under the Restate invocation."""

    def __init__(self, provider):
        self._provider = provider

    def get_tracer(self, *args, **kwargs):
        return RestateTracer(self._provider.get_tracer(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._provider, name)
