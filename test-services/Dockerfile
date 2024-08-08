# syntax=docker.io/docker/dockerfile:1.7-labs

FROM ghcr.io/pyo3/maturin AS build-sdk

WORKDIR /usr/src/app

COPY . .

RUN maturin build --out dist --interpreter python3.12

FROM python:3.12-slim AS test-services

WORKDIR /usr/src/app

COPY --from=build-sdk /usr/src/app/dist/* /usr/src/app/deps/

RUN pip install deps/* && pip install hypercorn
COPY test-services/ .

EXPOSE 9080

CMD ["hypercorn", "testservices:app", "--config", "hypercorn-config.toml"]
