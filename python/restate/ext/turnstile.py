from asyncio import Event
from restate.exceptions import SdkInternalException


class Turnstile:
    """A turnstile to manage ordered access based on IDs."""

    def __init__(self, ids: list[str]):
        # ordered mapping of id to next id in the sequence
        # for example:
        #   {'id1': 'id2', 'id2': 'id3'}     <-- id3 is the last.
        #   {}                               <-- no ids, no turns.
        #   {}                               <-- single id, no next turn.
        #
        self.turns = dict(zip(ids, ids[1:]))
        # mapping of id to event that signals when that id's turn is allowed
        self.events = {id: Event() for id in ids}
        self.canceled = False
        if ids:
            # make sure that the first id can proceed immediately
            event = self.events[ids[0]]
            event.set()

    async def wait_for(self, id: str) -> None:
        event = self.events[id]
        await event.wait()
        if self.canceled:
            raise SdkInternalException() from None

    def cancel_all_after(self, id: str) -> None:
        self.canceled = True
        next_id = self.turns.get(id)
        while next_id is not None:
            next_event = self.events[next_id]
            next_event.set()
            next_id = self.turns.get(next_id)

    def allow_next_after(self, id: str) -> None:
        next_id = self.turns.get(id)
        if next_id is None:
            return
        next_event = self.events[next_id]
        next_event.set()

    def cancel_all(self) -> None:
        self.canceled = True
        for event in self.events.values():
            event.set()
