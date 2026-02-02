.PHONY: setup install dev clean test lint help

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help:
	@echo "MTG Collection Builder"
	@echo ""
	@echo "Usage:"
	@echo "  make setup    - Create venv and install dependencies"
	@echo "  make install  - Install package in development mode"
	@echo "  make dev      - Install with dev dependencies"
	@echo "  make test     - Run tests"
	@echo "  make lint     - Run linter"
	@echo "  make clean    - Remove venv and build artifacts"
	@echo ""
	@echo "After setup, activate the venv:"
	@echo "  source .venv/bin/activate"
	@echo ""
	@echo "Then run the CLI:"
	@echo "  mtg --help"

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

setup: $(VENV)/bin/activate
	$(PIP) install -e .
	@echo ""
	@echo "Setup complete! Activate the virtual environment:"
	@echo "  source .venv/bin/activate"
	@echo ""
	@echo "Then initialize the database:"
	@echo "  mtg db init"

install: $(VENV)/bin/activate
	$(PIP) install -e .

dev: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"

test: $(VENV)/bin/activate
	$(PYTHON) -m pytest

lint: $(VENV)/bin/activate
	$(PYTHON) -m ruff check mtg_collector/
	$(PYTHON) -m black --check mtg_collector/

clean:
	rm -rf $(VENV)
	rm -rf *.egg-info
	rm -rf build/
	rm -rf dist/
	rm -rf __pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
