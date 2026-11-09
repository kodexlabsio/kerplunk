# Contributing to Konstruct

Thank you for helping. Bug reports with a reproducible case are the most valuable contribution: a SQL statement whose lineage is wrong, a DAG or Dagster file that is misread, or a project where a command fails.

## Reporting a Problem

Open an issue with the [bug report form](https://github.com/kodexlabsio/konstruct/issues/new?template=bug.yml). The most useful reports include:

- the smallest SQL, DAG or dbt model that shows the problem (rename anything confidential);
- what Konstruct said (`konstruct trace`, `explain` or `diff` output, or the warning in `REPORT.md`) and what it should have said;
- `konstruct --version`, the SQL dialect and, for dbt, the adapter.

For a security problem, follow [SECURITY.md](https://github.com/kodexlabsio/konstruct/blob/main/SECURITY.md) instead.

## Proposing a Change

For anything larger than a fix, open an issue first, so the approach can be agreed before you write it. New inputs (an orchestrator, a warehouse) and new commands especially benefit from this.

## Contributing an Ontology Pack

An ontology pack describes the entities, keys and measures of a schema that many teams load, such as a SaaS tool's tables as Fivetran or Airbyte land them, so every project that has the schema gets them without writing them. A pack is one file in `konstruct/semantics/ontologies/`: a `definitions.yml` with a `name`, a `description`, `schemas` patterns for the schema names the source lands in, and a `match` block naming the tables and the columns only that source has (see [The Ontology](https://kodexlabsio.github.io/konstruct/guide/ontology.html)). Add a test to `tests/test_registry.py` with a warehouse that has the schema, and take column names from the tool's published schema, not from one project's copy of it.

## Making a Change

The [Development section of the README](https://kodexlabsio.github.io/konstruct/development.html) covers setting up, the test suite and the commands. You need [uv](https://docs.astral.sh/uv/) and a stable [Rust toolchain](https://rustup.rs/) (1.88 or newer): `uv sync` builds Konstruct's Rust core with maturin. In short:

```bash
uv sync
uv run pre-commit install
eval "$(.kodex/scripts/services.sh up)"
uv run pytest --cov
```

A pull request is ready when:

1. **It has tests.** A lineage fix comes with a test that fails without it, preferably as a small SQL case in `tests/test_sql.py`. A change to what Konstruct claims about lineage should also hold under `konstruct verify` (`tests/test_verify.py`). Coverage must stay at or above 90%.
2. **The checks pass.** `uv run ruff check`, `uv run ruff format --check` and `uv run mypy` (strict) are clean, and the pre-push hook has run the tests. For a change to the Rust core (`src/`), `cargo fmt --check`, `cargo clippy --all-targets --all-features -- -D warnings` and `cargo test` pass there too.
3. **Output changes are reviewed.** If the CLI's text or `REPORT.md` changes, regenerate the golden files with `KONSTRUCT_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and check the diff is what you meant.
4. **Performance is no worse.** For changes to the build, SQL analysis or formulas, run `uv run .kodex/scripts/benchmark.py 500` before and after.
5. **The viewer is rebuilt.** Changes under `viewer/` pass `pnpm check` and `pnpm test`, and the rebuilt `konstruct/outputs/viewer.html` is committed with them (the pre-commit hook does the rebuild).
6. **The README says what changed**, if a user would notice it: a new command, option, input or limitation.

## Commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with the scopes listed in the README. A hook checks each message. Releases are cut automatically from them: `fix` gives a patch release, `feat` a minor one. Write the subject in sentence case and the body in plain British English, explaining why the change is needed.

## Code Style

- Typed Python 3.11+, checked by mypy in strict mode; the Rust core formatted by rustfmt and clean under Clippy; the viewer in strict TypeScript and Svelte 5, formatted by Prettier.
- Code lives in one language: what moves to Rust is removed from Python, with no second implementation to fall back on.
- Prefer small, plain functions to frameworks. Konstruct has few dependencies, and new ones need a good reason.
- Never import or execute user code (DAGs, Dagster definitions, Python models). Parse it.
- Comments explain why, not what.

## Licence

By contributing you agree that your contribution is licensed under the [MIT Licence](https://github.com/kodexlabsio/konstruct/blob/main/LICENSE).
