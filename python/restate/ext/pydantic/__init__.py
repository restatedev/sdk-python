import typing

from restate import ObjectContext, Context
from restate.server_context import current_context

from ._agent import RestateAgent
from ._model import RestateModelWrapper
from ._serde import PydanticTypeAdapter
from ._toolset import RestateContextRunToolSet

def restate_object_context() -> ObjectContext:
    """Get the current Restate ObjectContext."""
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return typing.cast(ObjectContext, ctx)


def restate_context() -> Context:
    """Get the current Restate Context."""
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return ctx


__all__ = [
    "RestateModelWrapper",
    "RestateAgent",
    "PydanticTypeAdapter",
    "RestateContextRunToolSet",
    "restate_object_context",
    "restate_context",
]
