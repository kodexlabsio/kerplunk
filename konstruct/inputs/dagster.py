"""Static parsing of Dagster code: assets, their dependencies, dbt assets, jobs and schedules.

Like the Airflow reader, this one reads files as syntax trees and never imports them, so Dagster
does not need to be installed and no user code runs. It understands the common ways of declaring
assets (``@asset`` and ``@multi_asset`` with their keys, groups and dependencies, ``@dbt_assets``,
``define_asset_job``, schedules and sensors) and op-based jobs (``@op`` composed in ``@graph`` and
``@job`` bodies); SQL passed as a literal inside an asset or op is kept for lineage. It is part of
the native core (``konstruct._core``, the Rust crate in ``src/dagster/``); this module is where the
rest of Konstruct imports it from.
"""

from __future__ import annotations

from konstruct._core import (
    ASSET_DECORATORS,
    OP_WRAPPERS,
    SENSOR_DECORATORS,
    SQL_METHODS,
    AssetInfo,
    DagsterFile,
    JobInfo,
    OpCall,
    OpInfo,
    SensorInfo,
    looks_like_dagster_file,
    parse_dagster_file,
    parse_dagster_source,
)

parse_file = parse_dagster_file
parse_source = parse_dagster_source

__all__ = [
    'ASSET_DECORATORS',
    'OP_WRAPPERS',
    'SENSOR_DECORATORS',
    'SQL_METHODS',
    'AssetInfo',
    'DagsterFile',
    'JobInfo',
    'OpCall',
    'OpInfo',
    'SensorInfo',
    'looks_like_dagster_file',
    'parse_file',
    'parse_source',
]
