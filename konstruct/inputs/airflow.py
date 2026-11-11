"""Static parsing of Airflow DAG files.

DAG files are read as syntax trees and never imported, so Airflow does not need to be installed
and no DAG code runs. The reader understands the common ways of writing DAGs and tasks; anything
it cannot resolve statically (computed task ids, dynamic task mapping, SQL built at runtime) is
skipped and reported. It is part of the native core (``konstruct._core``, the Rust crate in
``src/airflow/``); this module is where the rest of Konstruct imports it from.
"""

from __future__ import annotations

from konstruct._core import (
    COSMOS_CLASSES,
    DAG_CLASSES,
    DBT_COMMANDS,
    GROUP,
    GROUP_CLASSES,
    READ_KWARGS,
    SQL_KWARGS,
    UNRESOLVED,
    WRITE_KWARGS,
    DagInfo,
    DbtInvocation,
    TaskInfo,
    looks_like_dag_file,
    parse_dag_file,
    parse_dag_source,
    parse_dbt_commands,
)

parse_file = parse_dag_file
parse_source = parse_dag_source

__all__ = [
    'COSMOS_CLASSES',
    'DAG_CLASSES',
    'DBT_COMMANDS',
    'GROUP',
    'GROUP_CLASSES',
    'READ_KWARGS',
    'SQL_KWARGS',
    'UNRESOLVED',
    'WRITE_KWARGS',
    'DagInfo',
    'DbtInvocation',
    'TaskInfo',
    'looks_like_dag_file',
    'parse_dbt_commands',
    'parse_file',
    'parse_source',
]
