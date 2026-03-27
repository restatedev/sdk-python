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
Client for the Restate Admin API.

Provides typed access to service and handler metadata
via the Restate admin API (default port 9070).
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class HandlerInfo(BaseModel):
    """Metadata about a handler returned by the Restate admin API."""

    model_config = ConfigDict(extra="allow")

    name: str
    ty: Literal["Exclusive", "Shared", "Workflow"] | None = None
    documentation: str | None = None
    metadata: dict[str, str] | None = None
    input_description: str | None = None
    output_description: str | None = None
    input_json_schema: dict[str, Any] | None = None
    output_json_schema: dict[str, Any] | None = None


class ServiceInfo(BaseModel):
    """Metadata about a service returned by the Restate admin API."""

    model_config = ConfigDict(extra="allow")

    name: str
    ty: Literal["Service", "VirtualObject", "Workflow"]
    handlers: list[HandlerInfo] = Field(default_factory=list)
    deployment_id: str | None = None
    revision: int | None = None
    public: bool | None = None
    documentation: str | None = None
    metadata: dict[str, str] | None = None

    def get_handler(self, name: str) -> HandlerInfo | None:
        """Get a handler by name, or None if not found."""
        for h in self.handlers:
            if h.name == name:
                return h
        return None


class ListServicesResponse(BaseModel):
    """Response from GET /services."""

    services: list[ServiceInfo]


class AdminClient:
    """Client for the Restate Admin API.

    Example::

        async with AdminClient("http://localhost:9070") as client:
            services = await client.list_services()
            for svc in services:
                print(f"{svc.name} ({svc.ty}): {len(svc.handlers)} handlers")
                for h in svc.handlers:
                    print(f"  - {h.name} metadata={h.metadata}")
    """

    def __init__(self, admin_url: str, headers: dict[str, str] | None = None):
        self._admin_url = admin_url.rstrip("/")
        self._headers = headers
        self._client: httpx.AsyncClient | None = None
        self._owns_client = False

    @classmethod
    def from_client(cls, admin_url: str, client: httpx.AsyncClient) -> AdminClient:
        """Create an AdminClient using an existing httpx client."""
        instance = cls(admin_url)
        instance._client = client
        instance._owns_client = False
        return instance

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._admin_url, headers=self._headers)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AdminClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def list_services(self) -> list[ServiceInfo]:
        """List all registered services with their handlers and metadata.

        Returns:
            A list of ServiceInfo objects, each containing the service's
            handlers, metadata, documentation, and configuration.
        """
        client = await self._get_client()
        response = await client.get("/services")
        response.raise_for_status()
        parsed = ListServicesResponse.model_validate(response.json())
        return parsed.services

    async def get_service(self, name: str) -> ServiceInfo:
        """Get detailed information about a specific service.

        Args:
            name: The service name.

        Returns:
            A ServiceInfo object with full handler and metadata details.

        Raises:
            httpx.HTTPStatusError: If the service is not found (404) or other errors.
        """
        client = await self._get_client()
        response = await client.get(f"/services/{name}")
        response.raise_for_status()
        return ServiceInfo.model_validate(response.json())
