#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""Test containers based wrapper for the restate server"""

import asyncio
import socket
import typing
from urllib.error import URLError

from hypercorn.config import Config
from hypercorn.asyncio import serve
from testcontainers.core.container import DockerContainer # type: ignore
from testcontainers.core.waiting_utils import wait_container_is_ready # type: ignore
import httpx

class RestateContainer(DockerContainer):
    """Create a Restate container"""

    def __init__(self, image):
        super().__init__(image)
        self.with_exposed_ports(8080, 9070)

    def ingress_url(self):
        """return the URL to access the Restate ingress"""
        return f"http://{self.get_container_host_ip()}:{self.get_exposed_port(8080)}"

    def admin_url(self):
        """return the URL to access the Restate admin"""
        return f"http://{self.get_container_host_ip()}:{self.get_exposed_port(9070)}"

    def get_admin_client(self):
        """return an httpx client to access the admin interface"""
        return httpx.Client(base_url=self.admin_url())

    def get_ingress_client(self):
        """return an httpx client to access the ingress interface"""
        return httpx.Client(base_url=self.ingress_url())

    @wait_container_is_ready(httpx.HTTPError, URLError)
    def _wait_healthy(self):
        """wait for restate's health checks to pass"""
        return self.get_ingress_client().get("/restate/health").is_success() and \
            self.get_admin_client().get("/health").is_success()

    def start(self):
        """start the container and wait for health checks to pass"""
        super().start()
        self._wait_healthy()
        return self


class RestateTestHarness:
    """A test harness for running Restate SDKs"""

    sdk_task: typing.Optional[asyncio.Task] = None
    restate_container: typing.Optional[RestateContainer] = None


    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def start(self):
        """start the restate server and the sdk"""
        # find a free port
        sock = socket.socket()
        sock.bind(('', 0))
        port = sock.getsockname()[1]

        # create the hypercorn server from the asgi app
        config = Config()
        config.bind = [f"0.0.0.0:{port}"]
        config.h2_max_concurrent_streams = 2147483647
        config.keep_alive_max_requests = 2147483647
        config.keep_alive_timeout = 2147483647

        coro = serve(self.asgi_app, config=config, mode='asgi')
        loop = asyncio.get_event_loop()

        self.sdk_task = loop.create_task(coro)

        # create a restate server
        restate = RestateContainer(image="restatedev/restate:1.1").start()
        self.restate_container = restate

        # register the sdk with the server
        uri = f"http://host.testcontainers.internal:${port}"
        try:
            client = restate.get_admin_client()
            client.post("/deployments", json={"uri": uri}).raise_for_status()
        except Exception as e:
            self.stop()
            raise AssertionError("Failed to register the SDK with the Restate server") from e



    def stop(self):
        """terminate the restate server and the sdk"""
        if self.restate_container is not None:
            self.restate_container.stop()
            self.restate_container = None
        if self.sdk_task is not None:
            self.sdk_task.cancel()
            self.sdk_task = None


    def ingress_client(self):
        """return an httpx client to access the restate server's ingress"""
        if self.restate_container is None:
            raise AssertionError("The Restate server has not been started. Use .start()")
        return self.restate_container.get_ingress_client()


    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False
