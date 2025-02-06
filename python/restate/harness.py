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
import pathlib
import threading
import random
import typing
from urllib.error import URLError

from hypercorn.config import Config
from hypercorn.asyncio import serve
from testcontainers.core.network import Network # type: ignore
from testcontainers.core.container import DockerContainer # type: ignore
from testcontainers.core.waiting_utils import wait_container_is_ready # type: ignore
import httpx


def run_in_background(coro) -> threading.Thread:
    """run a coroutine in the background"""
    def runner():
        asyncio.run(coro)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread

class UdsAsgiServer:
    """A simple ASGI server that listens on a unix domain socket"""

    thread: typing.Optional[threading.Thread] = None

    def __init__(self, asgi_app, host_uds):
        self.asgi_app = asgi_app
        self.host_uds = host_uds
        self.stop_event = asyncio.Event()
        self.exit_event = asyncio.Event()

    def stop(self):
        """stop the server"""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            self.thread = None
        self.exit_event.set()

    def start(self):
        """start the server"""

        async def run_asgi():
            """run the asgi app on the given port"""
            config = Config()
            config.bind = [f"unix:{self.host_uds}"]
            config.h2_max_concurrent_streams = 2147483647
            config.keep_alive_max_requests = 2147483647
            config.keep_alive_timeout = 2147483647

            try:
                print(f"Starting ASGI server on port {self.host_uds}", flush=True)
                await serve(self.asgi_app,
                            config=config,
                            mode='asgi',
                            shutdown_trigger= self.stop_event.wait)
            except asyncio.CancelledError:
                print("ASGI server was cancelled", flush=True)
            except Exception as e: # pylint: disable=broad-except
                print(f"Failed to start the ASGI server: {e}", flush=True)
                raise e
            finally:
                self.exit_event.set()

        self.thread = run_in_background(run_asgi())
        return self

class HostProxyContainer(DockerContainer):
    """create a proxy container that proxies a unix domain socket from the host"""

    def __init__(self, name, port, host_uds,  network):
        super().__init__("alpine/socat")
        self.with_name(name)
        self.with_exposed_ports(port)
        self.with_network(network)
        self.with_volume_mapping(host_uds, "/tmp/unix_socket", mode="z")
        self.with_command(["TCP-LISTEN:9081,reuseaddr,fork", "UNIX-CLIENT:/tmp/unix_socket"])
        self.name = name
        self.port = port

    def connection_string(self):
        """return the connection string for the proxy"""
        return f"http://{self.name}:{self.port}"

class RestateContainer(DockerContainer):
    """Create a Restate container"""

    def __init__(self, image, network):
        super().__init__(image)
        self.with_exposed_ports(8080, 9070)
        self.with_network(network)

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
        self.get_ingress_client().get("/restate/health").raise_for_status()
        self.get_admin_client().get("/health").raise_for_status()


    def start(self):
        """start the container and wait for health checks to pass"""
        super().start()
        self._wait_healthy()
        return self



class RestateTestHarness:
    """A test harness for running Restate SDKs"""
    uds_path: typing.Optional[str] = None
    network: typing.Optional[Network] = None
    server: typing.Optional[UdsAsgiServer] = None
    restate: typing.Optional[RestateContainer] = None
    proxy: typing.Optional[HostProxyContainer] = None


    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def start(self):
        """start the restate server and the sdk"""
        self.uds_path = f"/tmp/restate-test-{random.randint(1, 1 << 63)}.sock"
        self.server = UdsAsgiServer(self.asgi_app, self.uds_path).start()

        self.network = Network().create()

        self.restate = RestateContainer(image="restatedev/restate:1.1",
                                        network=self.network).start()

        self.proxy = HostProxyContainer(name="proxy",
                                        port=9081,
                                        host_uds=self.uds_path,
                                        network=self.network).start()
        try:
            self._register_sdk()
        except Exception as e:
            self.stop()
            raise AssertionError("Failed to register the SDK with the Restate server") from e

    def _register_sdk(self):
        """register the sdk with the restate server"""
        assert self.proxy is not None
        assert self.restate is not None

        uri = self.proxy.connection_string()
        client = self.restate.get_admin_client()
        res = client.post("/deployments",
                          headers={"content-type" : "application/json"},
                          json={"uri": uri})
        if not res.is_success:
            msg = f"unable to register the services at {uri} - {res.status_code} {res.text}"
            raise AssertionError(msg)

    def stop(self):
        """terminate the restate server and the sdk"""
        if self.server is not None:
            self.server.stop()
            self.server = None

        if self.restate is not None:
            self.restate.stop()
            self.restate = None

        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None

        if self.uds_path is not None:
            pathlib.Path(self.uds_path).unlink(missing_ok=True)
            self.uds_path = None


    def ingress_client(self):
        """return an httpx client to access the restate server's ingress"""
        if self.restate is None:
            raise AssertionError("The Restate server has not been started. Use .start()")
        return self.restate.get_ingress_client()


    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False


def test_harness(asgi_app) -> RestateTestHarness:
    """create a test harness for running Restate SDKs"""
    return RestateTestHarness(asgi_app)
