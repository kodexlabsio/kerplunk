"""Fuzz the readers of Airflow, Dagster and PySpark files, which parse Python with ``ast`` and
never run it: for any text, each reads it or reports that it cannot (a SyntaxError from the
Airflow and Dagster readers, a problem from the PySpark one), and nothing else escapes.

Run with ``uv run --with atheris python tests/fuzz_targets/fuzz_python.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import atheris

with atheris.instrument_imports(include=['konstruct']):
    from konstruct.inputs import airflow, dagster, spark

PATH = Path('dags/fuzzed.py')
# Each reader looks further into a file that imports its framework.
PRELUDES = [
    '',
    'from airflow import DAG\n',
    'from dagster import asset\n',
    'from pyspark.sql import SparkSession\n',
]


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    source = provider.PickValueInList(PRELUDES) + provider.ConsumeUnicodeNoSurrogates(len(data))
    for read in (airflow.parse_source, dagster.parse_source):
        with contextlib.suppress(SyntaxError):
            read(PATH, source)
    spark.parse(source, str(PATH))


if __name__ == '__main__':
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
