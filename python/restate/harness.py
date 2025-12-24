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

import abc
import asyncio
from dataclasses import dataclass
import threading
import typing
import socket
from contextlib import contextmanager, asynccontextmanager

from hypercorn.config import Config
from hypercorn.asyncio import serve
from restate.client import create_client
from restate.server_types import RestateAppT
from restate.types import HarnessEnvironment
from testcontainers.core.container import DockerContainer  # type: ignore
from testcontainers.core.wait_strategies import CompositeWaitStrategy, HttpWaitStrategy

import httpx


def find_free_port():
    """find the next free port to bind to on the host machine"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def run_in_background(coro) -> threading.Thread:
    """run a coroutine in the background"""

    def runner():
        asyncio.run(coro)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


class BindAddress(abc.ABC):
    """A bind address for the ASGI server"""

    @abc.abstractmethod
    def get_local_bind_address(self) -> str:
        """return the local bind address for hypercorn to bind to"""

    @abc.abstractmethod
    def get_endpoint_connection_string(self) -> str:
        """return the SDK connection string to be used by restate"""

    @abc.abstractmethod
    def cleanup(self):
        """cleanup any resources used by the bind address"""


class TcpSocketBindAddress(BindAddress):
    """Bind a TCP address that listens on a random TCP port"""

    def __init__(self):
        self.port = find_free_port()

    def get_local_bind_address(self) -> str:
        return f"0.0.0.0:{self.port}"

    def get_endpoint_connection_string(self) -> str:
        return f"http://host.docker.internal:{self.port}"

    def cleanup(self):
        pass


class AsgiServer:
    """A simple ASGI server that listens on a unix domain socket"""

    thread: typing.Optional[threading.Thread] = None

    def __init__(self, asgi_app, bind_address: BindAddress):
        self.asgi_app = asgi_app
        self.bind_address = bind_address
        self.stop_event = asyncio.Event()
        self.exit_event = asyncio.Event()

    def stop(self):
        """stop the server"""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
            self.thread = None
        self.exit_event.set()

    def start(self):
        """start the server"""

        def shutdown_trigger():
            """trigger the shutdown event"""
            return self.stop_event.wait()

        async def run_asgi():
            """run the asgi app on the given port"""
            config = Config()
            config.h2_max_concurrent_streams = 2147483647
            config.keep_alive_max_requests = 2147483647
            config.keep_alive_timeout = 2147483647

            bind = self.bind_address.get_local_bind_address()
            config.bind = [bind]
            try:
                print(f"Starting ASGI server on {bind}", flush=True)
                await serve(self.asgi_app, config=config, mode="asgi", shutdown_trigger=shutdown_trigger)
            except asyncio.CancelledError:
                print("ASGI server was cancelled", flush=True)
            except Exception as e:  # pylint: disable=broad-except
                print(f"Failed to start the ASGI server: {e}", flush=True)
                raise e
            finally:
                self.exit_event.set()

        self.thread = run_in_background(run_asgi())
        return self


class RestateContainer(DockerContainer):
    """Create a Restate container"""

    log_thread: typing.Optional[threading.Thread] = None

    def __init__(self, image, always_replay, disable_retries):
        super().__init__(image)
        self.with_exposed_ports(8080, 9070)
        self.with_env("RESTATE_LOG_FILTER", "restate=info")
        self.with_env("RESTATE_BOOTSTRAP_NUM_PARTITIONS", "1")
        self.with_env("RESTATE_DEFAULT_NUM_PARTITIONS", "1")
        self.with_env("RESTATE_SHUTDOWN_TIMEOUT", "10s")
        self.with_env("RESTATE_ROCKSDB_TOTAL_MEMORY_SIZE", "32 MB")
        self.with_env("RESTATE_WORKER__INVOKER__IN_MEMORY_QUEUE_LENGTH_LIMIT", "64")
        if always_replay:
            self.with_env("RESTATE_WORKER__INVOKER__INACTIVITY_TIMEOUT", "0s")
        else:
            self.with_env("RESTATE_WORKER__INVOKER__INACTIVITY_TIMEOUT", "10m")
        self.with_env("RESTATE_WORKER__INVOKER__ABORT_TIMEOUT", "10m")
        if disable_retries:
            self.with_env("RESTATE_WORKER__INVOKER__RETRY_POLICY__TYPE", "none")

        self.with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
        self.waiting_for(
            CompositeWaitStrategy(
                HttpWaitStrategy(8080, "/restate/health").for_status_code(200),
                HttpWaitStrategy(9070, "/health").for_status_code(200),
            )
        )

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

    def start(self, stream_logs=False):
        """start the container and wait for health checks to pass"""
        super().start()

        def stream_log():
            for line in self.get_wrapped_container().logs(stream=True):
                print(line.decode("utf-8"), end="", flush=True)

        if stream_logs:
            thread = threading.Thread(target=stream_log, daemon=True)
            thread.start()
            self.log_thread = thread

        return self


@dataclass
class TestConfiguration:
    """A configuration for running tests"""

    restate_image: str = "restatedev/restate:latest"
    stream_logs: bool = False
    always_replay: bool = False
    disable_retries: bool = False


class RestateTestHarness:
    """A test harness for running Restate SDKs"""

    bind_address: typing.Optional[BindAddress] = None
    server: typing.Optional[AsgiServer] = None
    restate: typing.Optional[RestateContainer] = None

    def __init__(self, asgi_app, config: typing.Optional[TestConfiguration]):
        self.asgi_app = asgi_app
        if config:
            self.config = config
        else:
            self.config = TestConfiguration()

    def start(self):
        """start the restate server and the sdk"""
        self.bind_address = TcpSocketBindAddress()
        self.server = AsgiServer(self.asgi_app, self.bind_address).start()
        self.restate = RestateContainer(
            image=self.config.restate_image,
            always_replay=self.config.always_replay,
            disable_retries=self.config.disable_retries,
        ).start(self.config.stream_logs)
        try:
            self._register_sdk()
        except Exception as e:
            self.stop()
            raise AssertionError("Failed to register the SDK with the Restate server") from e

    def _register_sdk(self):
        """register the sdk with the restate server"""
        assert self.bind_address is not None
        assert self.restate is not None

        uri = self.bind_address.get_endpoint_connection_string()
        client = self.restate.get_admin_client()
        res = client.post("/deployments", headers={"content-type": "application/json"}, json={"uri": uri})
        if not res.is_success:
            msg = f"unable to register the services at {uri} - {res.status_code} {res.text}"
            raise AssertionError(msg)

    def stop(self):
        """terminate the restate server and the sdk"""
        if self.server is not None:
            self.server.stop()
        if self.restate is not None:
            self.restate.stop()
        if self.bind_address is not None:
            self.bind_address.cleanup()

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


@contextmanager
def create_app_server(
    app: RestateAppT,
) -> typing.Generator[str, None, None]:
    """
    Creates and starts an ASGI server for the given application.

    :param app: The ASGI application to be served.
    """
    bind_address = TcpSocketBindAddress()
    server = AsgiServer(app, bind_address).start()
    try:
        yield bind_address.get_endpoint_connection_string()
    finally:
        server.stop()


@contextmanager
def create_restate_container(
    restate_image: str = "restatedev/restate:latest",
    always_replay: bool = False,
    disable_retries: bool = False,
    stream_logs: bool = False,
) -> typing.Generator[RestateContainer, None, None]:
    """
    Creates and starts a Restate container.

    :param restate_image: The image name for the restate-server container
                          (default is "restatedev/restate:latest").
    :param always_replay: When True, this forces restate-server to always replay
                          on a suspension point. This is useful to hunt non-deterministic bugs
                          that might prevent your code to replay correctly (default is False).
    :param disable_retries: When True, retries are disabled (default is False).
    """
    restate = RestateContainer(
        image=restate_image,
        always_replay=always_replay,
        disable_retries=disable_retries,
    ).start(stream_logs)
    try:
        yield restate
    finally:
        restate.stop()


def test_harness(
    app: RestateAppT,
    follow_logs: bool = False,
    restate_image: str = "docker.io/restatedev/restate:latest",
    always_replay: bool = False,
    disable_retries: bool = False,
) -> RestateTestHarness:
    """
    DEPRECATED: Use ctx.create_test_harness instead.
    """
    config = TestConfiguration(
        restate_image=restate_image,
        stream_logs=follow_logs,
        always_replay=always_replay,
        disable_retries=disable_retries,
    )
    return RestateTestHarness(app, config)


@asynccontextmanager
async def create_test_harness(
    app: RestateAppT,
    follow_logs: bool = False,
    restate_image: str = "docker.io/restatedev/restate:latest",
    always_replay: bool = False,
    disable_retries: bool = False,
) -> typing.AsyncGenerator[HarnessEnvironment, None]:
    """
    Creates a test harness for running Restate services together with restate-server.

    example:
    ```
    from restate import create_test_harness
    from my_app import my_restate_app

    with create_test_harness(my_restate_app) as env:
        client = env.create_ingress_client()
        # run tests against the client
    ```

    :param app: The application to be tested using the RestateTestHarness.
    :param follow_logs: Whether to stream logs for the test process (default is False).
    :param restate_image: The image name for the restate-server container
                          (default is "restatedev/restate:latest").
    :param always_replay: When True, this forces restate-server to always replay
                          on a suspension point. This is useful to hunt non-deterministic bugs
                          that might prevent your code to replay correctly (default is False).
    :param disable_retries: When True, retries are disabled (default is False).
    """
    with (
        create_restate_container(
            restate_image=restate_image,
            always_replay=always_replay,
            disable_retries=disable_retries,
            stream_logs=follow_logs,
        ) as runtime,
        create_app_server(app) as bind_address,
    ):
        res = runtime.get_admin_client().post(
            "/deployments", headers={"content-type": "application/json"}, json={"uri": bind_address}
        )
        if not res.is_success:
            msg = f"unable to register the services at {bind_address} - {res.status_code} {res.text}"
            raise AssertionError(msg)

        async with create_client(runtime.ingress_url()) as client:
            yield HarnessEnvironment(
                ingress_url=runtime.ingress_url(), admin_api_url=runtime.admin_url(), client=client
            )
