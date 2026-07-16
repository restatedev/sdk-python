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

typecheck-pyright:
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright python/
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright examples/ 
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright tests
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright test-services/
    PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright ai-tests/

typecheck-mypy:
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional python/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional examples/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional tests/
    uv run -m mypy --check-untyped-defs --ignore-missing-imports --implicit-optional ai-tests/

typecheck: typecheck-pyright typecheck-mypy

test:
    uv run -m pytest tests/*

# AI integration tests: scripted (no key) + live (needs OPENAI_API_KEY). Runs both.
test-ai:
    uv run -m pytest ai-tests/ -v


# Recipe to run both mypy and pylint
verify: format lint typecheck test
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
