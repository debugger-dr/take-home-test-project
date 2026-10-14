.DEFAULT_GOAL := all
.PHONY: all setup test demo

UV := $(shell command -v uv 2>/dev/null)
ARGS ?=

ifdef UV
PYTHON := uv run python
else
PYTHON := .venv/bin/python
endif

all: test demo

setup:
ifdef UV
	@uv sync --quiet
else
	@python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "Python 3.12+ is required, or install uv: https://docs.astral.sh/uv/"; exit 1; }
	@test -x .venv/bin/pytest || { python3 -m venv .venv && .venv/bin/pip install --quiet pytest; }
endif

test: setup
	$(PYTHON) -m pytest

demo: setup
	$(PYTHON) -m llmgw $(ARGS)
