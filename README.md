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

## Versions

The Python SDK is currently in active development, and might break across releases.

The compatibility with Restate is described in the following table:

| Restate Server\sdk-python | 0.0 - 0.2 | 0.3 - 0.5 | 0.6 - 0.7 | 0.8 - 0.9        |
|---------------------------|-----------|-----------|-----------|------------------|
| 1.0                       | ✅         | ❌         | ❌         | ❌                |
| 1.1 - 1.2                 | ✅         | ✅         | ❌         | ❌                |
| 1.3                       | ✅         | ✅         | ✅         | ✅ <sup>(1)</sup> |
| 1.4                       | ✅         | ✅         | ✅         | ✅                |

<sup>(1)</sup> **Note** The new Service/Object/Workflow constructor fields and the decorator fields `inactivity_timeout`, `abort_timeout`, `journal_retention`, `idempotency_retention`, `ingress_private`, `workflow_retention` work only from Restate 1.4 onward. Check the in-code documentation for more details.

## Contributing

We’re excited if you join the Restate community and start contributing!
Whether it is feature requests, bug reports, ideas & feedback or PRs, we appreciate any and all contributions.
We know that your time is precious and, therefore, deeply value any effort to contribute!

### Local development

* Python 3
* PyEnv or VirtualEnv
* [just](https://github.com/casey/just)
* [Rust toolchain](https://rustup.rs/)

Setup your virtual environment using the tool of your choice, e.g. VirtualEnv:

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
