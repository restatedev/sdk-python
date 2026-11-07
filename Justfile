# Use UV_PYTHON env variable to select either a python version or 
# the complete python to your python interpreter 

default := "all"

set shell := ["bash", "-c"]

sync:
    uv sync --all-extras --all-packages

format:
    uv run ruff format
    uv run ruff check --fix --fix-only

lint:
    uv run ruff format --check
    uv run ruff check

# The native extension (src/): formatted, and clean of clippy's warnings.
lint-rust:
    cargo fmt --check
    cargo clippy --all-targets -- -D warnings

typecheck-pyright:
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright python/
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright examples/ 
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright examples-agents/
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright tests
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright test-services/

typecheck-mypy:
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional python/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional examples/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional examples-agents/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional tests/

typecheck: typecheck-pyright typecheck-mypy

test:
    uv run -m pytest tests/*

# The tests that need no Restate server (no Docker): in-process, in seconds.
test-fast:
    uv run -m pytest tests/* -m "not server"


# Recipe to run both mypy and pylint
verify: format lint lint-rust typecheck test
    @echo "Type checking and linting completed successfully."

# Recipe to build the project
build:
    @echo "Building the project..."
    #maturin build --release
    uv build --all-packages

clean:
	@echo "Cleaning the project"
	cargo clean

example:
	#!/usr/bin/env bash
	cd examples/
	if [ -z "$PYTHONPATH" ]; then
		export PYTHONPATH="examples/"
	else
		export PYTHONPATH="$PYTHONPATH:examples/"
	fi
	hypercorn --config hypercorn-config.toml example:app
