# Developer shortcuts. Building and installing is done with Meson, see README.md.

VENV   ?= .venv
PYTHON ?= /usr/bin/python3
BIN    := $(VENV)/bin

.PHONY: venv lint format typecheck test check clean

venv: $(BIN)/pytest

$(BIN)/pytest: requirements-dev.txt
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(BIN)/pip install --quiet -r requirements-dev.txt
	@touch $@

lint: venv
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: venv
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: venv
	$(BIN)/mypy

test: venv
	$(BIN)/pytest

check: lint typecheck test

clean:
	rm -rf build stage .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
