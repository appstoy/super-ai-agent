.PHONY: test lint typecheck check

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run mypy src

check: test lint typecheck

