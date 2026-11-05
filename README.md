[![Documentation](https://img.shields.io/badge/doc-reference-blue)](https://docs.restate.dev)
[![Examples](https://img.shields.io/badge/view-examples-blue)](https://github.com/restatedev/examples)
[![Discord](https://img.shields.io/discord/1128210118216007792?logo=discord)](https://discord.gg/skW3AZ6uGd)
[![Twitter](https://img.shields.io/twitter/follow/restatedev.svg?style=social&label=Follow)](https://twitter.com/intent/follow?screen_name=restatedev)

# Restate Python SDK

[Restate](https://restate.dev/) is a system for easily building resilient applications using *distributed durable async/await*. This repository contains the Restate SDK for writing services in **Python**.

## Community

* 🤗️ [Join our online community](https://discord.gg/skW3AZ6uGd) for help, sharing feedback and talking to the community.
* 📖 [Check out our documentation](https://docs.restate.dev) to get quickly started!
* 📣 [Follow us on Twitter](https://twitter.com/restatedev) for staying up to date.
* 🙋 [Create a GitHub issue](https://github.com/restatedev/sdk-typescript/issues) for requesting a new feature or reporting a problem.
* 🏠 [Visit our GitHub org](https://github.com/restatedev) for exploring other repositories.

## Using the SDK

**Prerequisites**:
- Python >= v3.11

To use this SDK, add the dependency to your project:

```shell
pip install restate_sdk
```

## Serving using an ASGI server

Given an `app.py`:

```python
import restate

greeter = restate.Service("greeter")

@greeter.handler()
async def greet(ctx: restate.Context, name: str) -> str:
    return f"Hello {name}!"

app = restate.app(services=[greeter])
```

Serve it with `restate.serve`, which needs the `serve` extra (`pip install "restate-sdk[serve]"`).
It listens on `$PORT`, or 9080, with the HTTP/2 that Restate connects with, and
stops on SIGINT or SIGTERM:

```python
if __name__ == "__main__":
    import asyncio

    asyncio.run(restate.serve(app))
```

Or from the command line, with no `__main__` block: `python -m restate serve app:app` (`--port`
to choose the port).

`restate.app(...)` returns an ASGI application, so any ASGI server can serve it instead, for example
[Hypercorn](https://github.com/pgjones/hypercorn):

```shell
pip install hypercorn
python -m hypercorn app:app --bind "0.0.0.0:9080"
```

Once served, register the deployment with `restate deployments register http://localhost:9080`.

## Connecting to Restate Cloud

An application can connect outbound to Restate Cloud instead of exposing a port. Copy a connect key
from the [Cloud UI -> Development → Configure SDK](https://cloud.restate.dev/to/developers/integration#configure-sdk), pick a name for the deployment, and run:

```shell
RESTATE_CONNECT_KEY=rstcloudv1_... python -m restate connect app:app --name greeter-v1
```

Then register the deployment as `connect://greeter-v1`.

The same from code:

```python
if __name__ == "__main__":
    import asyncio
    
    asyncio.run(restate.connect(app, name="greeter-v1"))
```

Or using the context manager, for finer control:

```python
async with restate.with_connection(app, name="greeter-v1") as connection:
    print(connection.info.deployment_url)
    await connection.wait_closed()
```

### Clients

`restate.create_client()` without a URL connects to Restate Cloud using the ingress key in `RESTATE_INGRESS_KEY`, copied from the
[Cloud UI -> Development → Configure SDK](https://cloud.restate.dev/to/developers/integration#configure-sdk).

## Versions

The compatibility with Restate is described in the following table. sdk-python versions < 0.10 are deprecated and no longer supported.

| Restate Server\sdk-python | 0.10 - 0.18      | 1.0                 |
|---------------------------|------------------|---------------------|
| < 1.3                     | ❌                | ❌                   |
| 1.3 - 1.4                 | ✅ <sup>(1)</sup> | ✅ <sup>(1)(2)</sup> |
| 1.5 - 1.6                 | ✅                | ✅ <sup>(2)</sup>    |
| 1.7                       | ✅                | ✅                   |
| 1.8                       | ✅                | ✅ <sup>(3)</sup>    |

<sup>(1)</sup> **Note** The new Service/Object/Workflow constructor field and the decorator field `invocation_retry_policy` works only from Restate 1.4 onward. Check the in-code documentation for more details.

<sup>(2)</sup> **Note** Scopes and limit keys work only from Restate 1.7 onward. Check the in-code documentation for more details.

<sup>(3)</sup> **Note** The list form of `state_preload` (eager state keys whitelist) works only from Restate 1.8 onward. Check the in-code documentation for more details.

## Contributing

We’re excited if you join the Restate community and start contributing!
Whether it is feature requests, bug reports, ideas & feedback or PRs, we appreciate any and all contributions.
We know that your time is precious and, therefore, deeply value any effort to contribute!

### Local development

* Python 3
* PyEnv or VirtualEnv
* [just](https://github.com/casey/just)
* [Rust toolchain](https://rustup.rs/)

Set up your virtual environment using the tool of your choice, e.g. VirtualEnv:

```shell
python3 -m venv .venv
source .venv/bin/activate
```

Install the build tools:

```shell
pip install -r requirements.txt
```

Now build the Rust module and include opt-in additional dev dependencies:

```shell
maturin dev -E test,lint
```

You usually need to build the Rust module only once, but you might need to rebuild it on pulls.

For linting and testing:

```shell
just verify
```

## Releasing the package

Pull latest main:

```shell
git checkout main && git pull
```

**Update module version in `Cargo.toml` and run a local build to update the `Cargo.lock` too**, commit it. Then push tag, e.g.:

```
git tag -m "Release v0.1.0" v0.1.0
git push origin v0.1.0
```
