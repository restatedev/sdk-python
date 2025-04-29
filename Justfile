# Justfile 

python := "python3"

default:
    @echo "Available recipes:"
    @echo "  mypy   - Run mypy for type checking"
    @echo "  pylint - Run pylint for linting"
    @echo "  test   - Run pytest for testing"
    @echo "  verify - Run mypy, pylint, test"

# Recipe to run mypy for type checking
mypy:
    @echo "Running mypy..."
    {{python}} -m mypy --check-untyped-defs --ignore-missing-imports python/restate/
    {{python}} -m mypy --check-untyped-defs --ignore-missing-imports examples/

# Recipe to run pylint for linting
pylint:
    @echo "Running pylint..."
    {{python}} -m pylint python/restate --ignore-paths='^.*.?venv.*$'
    {{python}} -m pylint examples/ --ignore-paths='^.*\.?venv.*$'

test:
    @echo "Running Python tests..."
    {{python}} -m pytest tests/*

# Recipe to run both mypy and pylint
verify: mypy pylint test
    @echo "Type checking and linting completed successfully."

# Recipe to build the project
build:
    @echo "Building the project..."
    maturin build --release

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
