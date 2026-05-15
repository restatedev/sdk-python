---
name: run-sdk-tests
description: Run the Restate SDK conformance test suite locally against this SDK's Docker image. Use when the user wants to run sdk tests, run conformance tests, or verify an implementation.
user-invocable: true
---

# Running SDK Conformance Tests Locally

## Quick start

```bash
# Build image + run all default suite tests
./.tools/run-sdk-tests.sh

# Skip rebuild if service code hasn't changed
./.tools/run-sdk-tests.sh --skip-build

# Run a single test class
./.tools/run-sdk-tests.sh --skip-build --test-suite=default --test-name=Combinators
```

## Prerequisites

- Java 21+
- Podman or Docker

## What the script does

1. Reads the suite version from `.github/workflows/integration.yaml` (single source of truth)
2. Builds the service image: `podman/docker build -f test-services/Dockerfile .`
3. Downloads and caches `sdk-tests.jar` in `tmp/` (version-pinned)
4. Pulls the Restate runtime image explicitly
5. Runs `java -jar sdk-tests.jar run ...` directly on the host

## Key flags (passed through to the runner)

| Flag | Purpose |
|------|---------|
| `--test-suite=default` | Which suite to run (`default`, `alwaysSuspending`, `threeNodes`, etc.) |
| `--test-name=ClassName` | Run only one test class (requires `--test-suite`) |
| `--exclusions-file=path` | YAML file listing tests to skip |

## Reading results

Logs are written to `tmp/test-report/<timestamp>/<suite>/<TestClass>/`:
- `testRunner.log` — client-side request/response logs
- `runtime_0.log` — Restate server log
- `default-service_0.log` — SDK service container log
