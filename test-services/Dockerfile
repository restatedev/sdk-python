# syntax=docker.io/docker/dockerfile:1.7-labs

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim as build-sdk

ENV UV_PYTHON "3.12"

RUN apt-get update -y && apt-get install -y build-essential

WORKDIR /usr/src/app

COPY src ./src/
COPY python ./python/
COPY Cargo.lock .
COPY Cargo.toml .
COPY rust-toolchain.toml .
COPY pyproject.toml .
COPY LICENSE .
COPY README.md .
COPY uv.lock .


# Only the wheel is needed, built once from the source tree: no editable install of
# the extras, and no sdist to build it again from (each compiles the Rust extension,
# which under QEMU, for linux/arm64, takes the better part of an hour).
RUN uv build --wheel

FROM python:3.12-slim AS test-services

WORKDIR /usr/src/app

COPY --from=build-sdk /usr/src/app/dist/* /usr/src/app/deps/

RUN pip install deps/*whl 
RUN pip install hypercorn

COPY test-services/ .

EXPOSE 9080

ENV RESTATE_CORE_LOG=debug
ENV RUST_BACKTRACE=1
ENV PORT 9080

ENTRYPOINT ["./entrypoint.sh"]
