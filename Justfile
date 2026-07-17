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

# Each integration runs in its own dependency environment.
test-ai-openai:
    uv run --isolated --locked --extra test --extra harness --extra serde --extra openai -m pytest ai-tests/openai_test.py -v

test-ai-pydantic:
    uv run --isolated --locked --extra test --extra harness --extra serde --extra pydantic_ai -m pytest ai-tests/pydantic_test.py -v

test-ai-google-adk:
    uv run --isolated --locked --extra test --extra harness --extra serde --extra adk -m pytest ai-tests/google_adk_test.py -v

test-ai-langchain:
    uv run --isolated --locked --extra test --extra harness --extra serde --extra langchain --extra langchain_test -m pytest ai-tests/langchain_test.py -v

test-ai: test-ai-openai test-ai-pydantic test-ai-google-adk test-ai-langchain

test-ai-scripted:
    uv run --isolated --locked --extra test --extra harness --extra serde --extra openai -m pytest ai-tests/openai_test.py -m "not live_model" -v
    uv run --isolated --locked --extra test --extra harness --extra serde --extra pydantic_ai -m pytest ai-tests/pydantic_test.py -m "not live_model" -v
    uv run --isolated --locked --extra test --extra harness --extra serde --extra adk -m pytest ai-tests/google_adk_test.py -m "not live_model" -v
    uv run --isolated --locked --extra test --extra harness --extra serde --extra langchain --extra langchain_test -m pytest ai-tests/langchain_test.py -m "not live_model" -v


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
