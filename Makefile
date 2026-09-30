.DEFAULT_GOAL := all

CI_PYTHON := 3.13
PYTHON ?= python
VENV_BIN := .venv/bin
ifeq ($(OS),Windows_NT)
VENV_BIN := .venv/Scripts
endif
DEV_PYTHON := $(VENV_BIN)/python
export PATH := $(abspath $(VENV_BIN)):$(PATH)
export UV_SYSTEM_PYTHON := 0
UV_VERSION := 0.12.13
CI_BUILD_REQUIREMENTS = $(DEV_PYTHON) -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"]))'
CI_COMPILE = uv pip compile pyproject.toml - --universal --python-version $(CI_PYTHON) --no-python-downloads --no-sources --no-build --generate-hashes --no-annotate --custom-compile-command 'make lock-update'

PYTHON_INPUTS := src tests plugins _build.py conftest.py
MARKDOWN_FILES := README.md docs/README.md docs/adding-a-host.md docs/compatibility.md docs/concepts.md docs/development.md $(wildcard plugins/*/README.md) docs/platforms.md docs/reference/cli.md docs/reference/host-protocols.md docs/security.md docs/troubleshooting.md

.PHONY: all check dead-code-check env-check format format-check install lock-check lock-update quality reinstall setup test type-check uv-check venv-check whitespace-check

uv-check:
	@test "$$(uv --version | cut -d ' ' -f 2)" = "$(UV_VERSION)" || \
		{ echo 'Use uv $(UV_VERSION), matching CI.' >&2; exit 1; }

setup: uv-check
	@test -d .venv || uv venv --python "$(PYTHON)" --no-python-downloads .venv
	rm -f .venv/.requirements-ci.txt .venv/.pyproject.toml
	uv pip sync --python "$(DEV_PYTHON)" --no-python-downloads --require-hashes --build-constraints requirements-ci.txt requirements-ci.txt
	cp requirements-ci.txt .venv/.requirements-ci.txt
	cp pyproject.toml .venv/.pyproject.toml

venv-check:
	@test -x "$(DEV_PYTHON)" || { echo 'Run make setup before project commands.' >&2; exit 1; }

env-check: venv-check
	@cmp -s requirements-ci.txt .venv/.requirements-ci.txt && \
		cmp -s pyproject.toml .venv/.pyproject.toml || \
		{ echo 'Project setup is incomplete or outdated; run make setup.' >&2; exit 1; }

install:
	uv tool install .

reinstall:
	uv tool install --reinstall .

format: env-check
	$(DEV_PYTHON) -m ruff format $(PYTHON_INPUTS)
	$(DEV_PYTHON) -m ruff check --fix $(PYTHON_INPUTS)
	biome format --write .
	$(DEV_PYTHON) -m mdformat $(MARKDOWN_FILES)
	$(VENV_BIN)/pyproject-fmt pyproject.toml

format-check: env-check
	$(DEV_PYTHON) -m ruff format --check $(PYTHON_INPUTS)
	$(DEV_PYTHON) -m ruff check $(PYTHON_INPUTS)
	biome format .
	$(DEV_PYTHON) -m mdformat --check $(MARKDOWN_FILES)
	$(VENV_BIN)/pyproject-fmt --check pyproject.toml

lock-check: env-check uv-check
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	cp requirements-ci.txt "$$tmp/requirements-ci.txt" && \
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group dev --quiet --output-file "$$tmp/requirements-ci.txt" && \
	diff -u requirements-ci.txt "$$tmp/requirements-ci.txt" && \
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group test --constraint "$$tmp/requirements-ci.txt" --quiet --output-file "$$tmp/requirements-test.txt" && \
	diff -u requirements-test.txt "$$tmp/requirements-test.txt"

lock-update: venv-check uv-check
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group dev --output-file requirements-ci.txt $(LOCK_FLAGS)
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group test --constraint requirements-ci.txt --output-file requirements-test.txt

type-check: env-check
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonpath "$(DEV_PYTHON)" --pythonplatform Linux
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonpath "$(DEV_PYTHON)" --pythonplatform Darwin
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonpath "$(DEV_PYTHON)" --pythonplatform Windows

test: env-check
	$(DEV_PYTHON) -m pytest -q $(TEST_FLAGS)

dead-code-check: env-check
	$(DEV_PYTHON) -m vulture $(PYTHON_INPUTS)

whitespace-check:
	git diff --check HEAD

quality: format-check lock-check type-check dead-code-check whitespace-check

check: quality test

all: check
