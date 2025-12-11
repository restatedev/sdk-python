#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
ADK session service implementation using Restate Virtual Objects as the backing store.
"""

import restate

from typing import Optional, Any, cast

from google.adk.sessions import Session
from google.adk.sessions.state import State
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    ListSessionsResponse,
    GetSessionConfig,
)
from restate import TerminalError

from restate.extensions import current_context


class RestateSessionService(BaseSessionService):
    def ctx(self) -> restate.ObjectContext:
        return cast(restate.ObjectContext, current_context())

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        if session_id is None:
            session_id = str(self.ctx().uuid())

        session = await self.ctx().get(f"session_store::{session_id}", type_hint=Session)
        if session is not None:
            raise TerminalError("Session with the given ID already exists.")

        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=state or {},
        )

        await self.flush_session_state(session)
        return session

    async def has_session(self, *, session_id: str) -> bool:
        return await self.ctx().get(f"session_store::{session_id}", type_hint=Session) is not None

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        # TODO : Handle config options
        return await self.ctx().get(f"session_store::{session_id}", type_hint=Session) or Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
        )

    async def list_sessions(self, *, app_name: str, user_id: Optional[str] = None) -> ListSessionsResponse:
        state_keys = await self.ctx().state_keys()
        sessions = []
        for key in state_keys:
            if key.startswith("session_store::"):
                session = await self.ctx().get(key, type_hint=Session)
                if session is not None:
                    sessions.append(session)
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        self.ctx().clear(f"session_store::{session_id}")

    async def append_event(self, session: Session, event: Event) -> Event:
        """Appends an event to a session object."""
        if event.partial:
            return event
        # For now, we also store temp state
        event = self._trim_temp_delta_state(event)
        self._update_session_state(session, event)
        session.events.append(event)
        return event

    async def flush_session_state(self, session: Session):
        session_to_store = session.model_copy()

        # Remove temporary state keys before storing
        for key in list(session_to_store.state.keys()):
            if key.startswith(State.TEMP_PREFIX):
                session_to_store.state.pop(key)

        # Remove restate-specific context that got added by the plugin before storing
        session_to_store.state.pop("restate_context", None)

        deterministic_session = await self.ctx().run_typed(
            "store session", lambda: session_to_store, restate.RunOptions(type_hint=Session)
        )
        self.ctx().set(f"session_store::{session.id}", deterministic_session)
