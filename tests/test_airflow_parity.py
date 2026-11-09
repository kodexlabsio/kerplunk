"""The static parser against Airflow itself: the same DAGs, tasks and dependencies.

Needs Apache Airflow, which konstruct does not depend on. Run with:

    uv run --with "apache-airflow>=3" --with apache-airflow-providers-standard \
        --with apache-airflow-providers-common-sql pytest tests/test_airflow_parity.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from konstruct.inputs.airflow import parse_file

pytestmark = pytest.mark.airflow_parity

FIXTURES = Path(__file__).parent / 'fixtures' / 'airflow'
EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'jaffle_shop' / 'dags' / 'jaffle_shop_daily.py'
FILES = [*sorted(FIXTURES.glob('*.py')), EXAMPLE]


def airflow_view(path: Path, home: Path) -> dict[str, tuple[set[str], set[tuple[str, str]]]]:
    os.environ.setdefault('AIRFLOW_HOME', str(home))
    os.environ.setdefault('AIRFLOW__CORE__LOAD_EXAMPLES', 'False')
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:
        pytest.skip('Apache Airflow 3 is not installed')
    bag = DagBag(dag_folder=str(path))
    assert bag.import_errors == {}, bag.import_errors
    view = {}
    for dag_id, dag in bag.dags.items():
        tasks = set(dag.task_dict)
        edges = {
            (task_id, down) for task_id, task in dag.task_dict.items() for down in task.downstream_task_ids
        }
        view[dag_id] = (tasks, edges)
    return view


@pytest.mark.parametrize('path', FILES, ids=[p.stem for p in FILES])
def test_static_parse_matches_airflow(path: Path, tmp_path: Path) -> None:
    expected = airflow_view(path, tmp_path)
    parsed = {dag.dag_id: (set(dag.tasks), set(dag.dependencies)) for dag in parse_file(path)}
    assert set(parsed) == set(expected)
    for dag_id, (tasks, edges) in expected.items():
        assert parsed[dag_id][0] == tasks, dag_id
        assert parsed[dag_id][1] == edges, dag_id
