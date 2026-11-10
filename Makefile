# Konstruct's commands, as AGENTS.md lists them: Python through uv, the Rust core through cargo and
# the viewer through pnpm, all from the repository root. Targets that run several tools run each
# one, so a single run reports every failure, and exit with failure if any of them failed.

.PHONY: help install i build format format-check lint lint-fix test docs paper bench check

.DEFAULT_GOAL := help

help: ## Show this help menu
	@awk 'BEGIN {FS = ":.*##"; printf "\nKonstruct. Usage: make \033[36m<target>\033[0m\n\n"} /^[a-zA-Z0-9_ -]+:.*?##/ {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2} END {printf "\n"}' $(MAKEFILE_LIST)

install i: ## Install the dependencies, building the extension, and the git hooks (i)
	uv sync
	@if [ "$$CI" = "true" ]; then \
		pnpm install --frozen-lockfile; \
	else \
		pnpm install; \
	fi
	uv run pre-commit install

build: ## Build the Rust extension into the environment, then the viewer
	uv sync
	pnpm build

format: ## Format code
	@rc=0; \
	uv run ruff format || rc=1; \
	cargo fmt || rc=1; \
	pnpm format || rc=1; \
	exit $$rc

format-check: ## Check formatting without writing changes
	@rc=0; \
	uv run ruff format --check || rc=1; \
	cargo fmt --check || rc=1; \
	pnpm format:check || rc=1; \
	exit $$rc

lint: ## Lint and type check code
	@rc=0; \
	uv run ruff check || rc=1; \
	uv run mypy || rc=1; \
	cargo clippy --all-targets --all-features -- -D warnings || rc=1; \
	pnpm check || rc=1; \
	exit $$rc

lint-fix: ## Lint and auto-fix
	@rc=0; \
	uv run ruff check --fix || rc=1; \
	cargo clippy --fix --allow-dirty --allow-staged --all-targets --all-features || rc=1; \
	exit $$rc

test: ## Run the Python, Rust and viewer tests
	@rc=0; \
	uv run pytest || rc=1; \
	cargo test || rc=1; \
	pnpm test || rc=1; \
	exit $$rc

docs: ## Build the documentation, with warnings as errors
	uv run --group docs sphinx-build -W -b html docs docs/_build/html

paper: ## Run the paper's experiments and build its PDF
	uv run python paper/generate.py
	cd paper && latexmk -pdf konstruct.tex

bench: ## Time the build on 500 generated models and the Rust core
	@rc=0; \
	uv run .kodex/scripts/benchmark.py 500 || rc=1; \
	cargo bench --bench core || rc=1; \
	exit $$rc

check: ## Check formatting, lint and run the tests
	@rc=0; \
	$(MAKE) --no-print-directory format-check || rc=1; \
	$(MAKE) --no-print-directory lint || rc=1; \
	$(MAKE) --no-print-directory test || rc=1; \
	exit $$rc
