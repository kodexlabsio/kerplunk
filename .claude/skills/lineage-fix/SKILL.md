---
name: lineage-fix
description: Reproduce and fix wrong or missing column lineage, a misread DAG, or a wrong formula in Konstruct. Use when a trace, impact, explain or diff result is wrong for some SQL, dbt model, Airflow or Dagster file.
---

# Fixing a lineage bug

1. **Reproduce in isolation.** Reduce the input to the smallest SQL that shows the problem and check
   it directly:
   ```bash
   uv run python -c "
   from konstruct.core.sql import analyse
   for s in analyse('''<sql>''', dialect='duckdb', schema={'db.s.t': {'a': 'INT'}}):
       print(s.target, s.columns, s.references, s.expressions, s.errors)
   "
   ```
   For a whole project: `uv run konstruct build <path>` then `konstruct trace`, `explain` or
   `describe` on the column.
2. **Write the failing test** in `tests/test_sql.py` (lineage), `tests/test_explain.py` (formulas),
   `tests/test_airflow.py` or `tests/test_dagster.py` (orchestration). Assert exact columns,
   transforms and references.
3. **Find the cause.** Column lineage: `_column_lineage`, `_leaves` and `_references` in
   `konstruct/core/sql.py`. Canonical expressions: `_Canonical`. Formulas: `Explainer.compose` in
   `konstruct/analysis/explain.py`. dbt rendering without dbt: `konstruct/inputs/jinja.py`.
4. **Check against the data.** Lineage claims must survive `konstruct verify`
   (`tests/test_verify.py`, including the differential property test). A fix that changes what is
   claimed should run:
   ```bash
   uv run pytest tests/test_sql.py tests/test_verify.py tests/test_properties.py tests/test_explain.py
   KONSTRUCT_REALWORLD=1 uv run pytest tests/test_realworld.py   # dbt Labs' jaffle-shop
   ```
5. **Mind the cost.** Changes to `sql.py` or `explain.py` should not slow builds:
   `uv run .kodex/scripts/benchmark.py 500` before and after.
6. If the limitation cannot be fixed, document it in `docs/reference/limitations.md`.
