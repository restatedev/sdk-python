from contextlib import contextmanager
from contextvars import ContextVar

from restate.ext.turnstile import Turnstile


class State:
    __slots__ = ("turnstile",)

    def __init__(self):
        self.turnstile = Turnstile([])


restate_state_var = ContextVar("restate_state_var", default=State())


def current_state() -> State:
    return restate_state_var.get()


def set_current_state(state: State):
    restate_state_var.set(state)


@contextmanager
def state_context():
    token = restate_state_var.set(State())
    try:
        yield
    finally:
        restate_state_var.reset(token)
