from asyncio import Event


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
        if ids:
            # make sure that the first id can proceed immediately
            event = self.events[ids[0]]
            event.set()

    async def wait_for(self, id: str) -> None:
        event = self.events[id]
        await event.wait()

    def allow_next_after(self, id: str) -> None:
        next_id = self.turns.get(id)
        if next_id is None:
            return
        next_event = self.events[next_id]
        next_event.set()
