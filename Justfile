# Justfile for unladen development

# Default: list available recipes
default:
    @just --list

# Install the project in development mode
install:
    uv sync --dev

# Run all tests
test *ARGS:
    uv run pytest {{ARGS}}

# Run tests with coverage
cov *ARGS:
    uv run pytest --cov=unladen --cov-report=term-missing {{ARGS}}

# Run tests with coverage and generate HTML report
cov-html:
    uv run pytest --cov=unladen --cov-report=html
    @echo "Open htmlcov/index.html to view the report"

# Run the linter and format check
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/

# Auto-fix lint issues and format code
fmt:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/

# Run type checking
typecheck:
    uv run ty check

# Run unladen CLI
run *ARGS:
    uv run unladen {{ARGS}}

# Run all checks (lint, typecheck, test)
check: lint typecheck test

# Start the next dev cycle after a release (e.g. 0.1.0 -> 0.2.0.dev1)
bump-dev SEGMENT="minor":
    uv version --bump {{SEGMENT}} --bump dev

# Drop the dev marker to cut a release (e.g. 0.2.0.dev1 -> 0.2.0)
release-version:
    uv version --bump stable

# Clean build artifacts and caches
clean:
    rm -rf build/ dist/ htmlcov/ .coverage .pytest_cache .ruff_cache
    find . -type d -name __pycache__ -exec rm -rf {} +
    find . -type d -name "*.egg-info" -exec rm -rf {} +
