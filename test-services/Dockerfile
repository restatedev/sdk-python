# syntax=docker.io/docker/dockerfile:1.7-labs

FROM ghcr.io/pyo3/maturin AS build-sdk

WORKDIR /usr/src/app

COPY src ./src/
COPY python ./python/
COPY Cargo.lock .
COPY Cargo.toml .
COPY rust-toolchain.toml .
COPY requirements.txt .
COPY pyproject.toml .
COPY LICENSE .
COPY README.md .

RUN maturin build --out dist --interpreter python3.12

FROM python:3.12-slim AS test-services

WORKDIR /usr/src/app

COPY --from=build-sdk /usr/src/app/dist/* /usr/src/app/deps/

RUN pip install deps/* && pip install hypercorn
COPY test-services/ .

EXPOSE 9080

ENV RESTATE_CORE_LOG=debug
ENV RUST_BACKTRACE=1
ENV PORT 9080

ENTRYPOINT ["./entrypoint.sh"]
