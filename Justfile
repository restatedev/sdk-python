# Justfile

python := "python3"

# Recipe to run mypy for type checking
mypy:
    @echo "Running mypy..."
    {{python}} -m mypy --check-untyped-defs src/

# Recipe to run pylint for linting
pylint:
    @echo "Running pylint..."
    {{python}} -m pylint src/

test:
    @echo "Running tests..."
    {{python}} -m unittest discover tests/

# Recipe to run both mypy and pylint
verify: mypy pylint test
    @echo "Type checking and linting completed successfully."

# Recipe to build the project
build:
    @echo "Building the project..."
    {{python}} setup.py sdist bdist_wheel

clean:
	@echo "Cleaning the project"
	rm -rf dist/

# Default recipe to show help message
default:
    @echo "Available recipes:"
    @echo "  mypy   - Run mypy for type checking"
    @echo "  pylint - Run pylint for linting"
    @echo "  verify  - Run both mypy and pylint"
