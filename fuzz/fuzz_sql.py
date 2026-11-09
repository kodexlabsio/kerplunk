"""Fuzz the SQL analysis: for any text in any dialect, ``analyse`` returns lineage or an
``unparsed`` statement, and never raises.

Run with ``uv run --with atheris python fuzz/fuzz_sql.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports(include=['konstruct', 'sqlglot']):
    from konstruct.core.sql import analyse, strip_templating

DIALECTS = [None, 'bigquery', 'databricks', 'duckdb', 'postgres', 'redshift', 'snowflake', 'spark', 'trino']


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    dialect = provider.PickValueInList(DIALECTS)
    sql = provider.ConsumeUnicodeNoSurrogates(len(data))
    analyse(strip_templating(sql), dialect=dialect, target='db.schema.model')


if __name__ == '__main__':
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
