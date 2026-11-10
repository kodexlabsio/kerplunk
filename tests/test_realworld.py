"""dbt Labs' current jaffle-shop project, end to end: sources over seeds, packages, macros.

Needs network access (git clone and `dbt deps`), so it runs only with KONSTRUCT_REALWORLD=1; CI runs
it in its own job.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from konstruct import Project

from .conftest import dbt_executable

pytestmark = pytest.mark.realworld

REPOSITORY = 'https://github.com/dbt-labs/jaffle-shop.git'
COMMIT = '5beb145b00f5465ec759cfcdd9745e858818cf95'
PROFILE = 'default:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: jaffle.duckdb\n'


@pytest.fixture(scope='module')
def jaffle_shop(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not os.environ.get('KONSTRUCT_REALWORLD'):
        pytest.skip('set KONSTRUCT_REALWORLD=1 to run (needs network access)')
    dbt = dbt_executable()
    assert dbt is not None
    root = tmp_path_factory.mktemp('realworld') / 'jaffle-shop'
    subprocess.run(['git', 'clone', '--quiet', REPOSITORY, str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'checkout', '--quiet', COMMIT], check=True)
    (root / 'profiles.yml').write_text(PROFILE)
    env = {**os.environ, 'DBT_PROFILES_DIR': str(root), 'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false'}
    variables = '{"load_source_data": true}'
    for args in (['deps'], ['seed', '--vars', variables], ['build', '--vars', variables]):
        subprocess.run([dbt, *args, '--quiet', '--no-version-check'], cwd=root, env=env, check=True)
    return root


@pytest.fixture(scope='module')
def project(jaffle_shop: Path) -> Project:
    project = Project(jaffle_shop)
    project.build(run_dbt=False)
    return project


def test_builds_without_warnings(project: Project) -> None:
    graph = project.graph
    assert graph.warnings == []
    kinds = {n.kind for n in graph.nodes.values()}
    assert {'model', 'seed', 'source', 'test', 'column'} <= kinds
    from konstruct.outputs.report import analyse

    findings = analyse(graph)
    assert findings.lineage_covered / findings.lineage_total > 0.98


def test_lineage_runs_from_seed_through_source(project: Project) -> None:
    walk = project.trace('orders.order_total')
    assert [(c.label, c.relation_kind, c.transform) for c in walk.columns] == [
        ('stg_orders.order_total', 'model', 'direct'),
        ('ecom.raw_orders.order_total', 'source', 'transformed'),
        ('raw_orders.order_total', 'seed', 'direct'),
    ]


def test_macros_and_renames_resolve_to_real_columns(project: Project) -> None:
    supply = project.trace('stg_supplies.supply_cost')
    assert [(c.label, c.transform) for c in supply.columns][:1] == [('ecom.raw_supplies.cost', 'transformed')]
    cents = project.trace('stg_orders.order_total_cents')
    assert [(c.label, c.transform) for c in cents.columns][:1] == [('ecom.raw_orders.order_total', 'direct')]


def test_names_resolve_to_the_source(project: Project) -> None:
    assert project.describe('raw_orders').node.kind == 'source'
    assert project.describe('ecom.raw_orders').node.kind == 'source'
    assert project.describe('seed.jaffle_shop.raw_orders').node.kind == 'seed'
    impact = project.impact('raw_orders.order_total')
    assert 'orders.order_total' in {c.label for c in impact.columns}


def test_rendering_without_dbt_matches_compiled_lineage(
    jaffle_shop: Path, project: Project, tmp_path: Path
) -> None:
    """konstruct's own Jinja rendering (used when there is no compiled SQL, and by diff) must give
    the same lineage as dbt's compiled SQL."""
    import json
    import shutil

    copy = tmp_path / 'uncompiled'
    shutil.copytree(jaffle_shop, copy, ignore=shutil.ignore_patterns('konstruct-out'))
    manifest_path = copy / 'target' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for node in manifest['nodes'].values():
        node.pop('compiled_code', None)
    manifest_path.write_text(json.dumps(manifest))
    rendered = Project(copy)
    rendered.build(run_dbt=False)

    def edges(graph: object) -> tuple[set[tuple[str, str, str]], set[tuple[str, str]]]:
        all_edges = graph.edges  # type: ignore[attr-defined]
        columns = {
            (e.source, e.target, e.attrs.get('transform')) for e in all_edges if e.kind == 'derived_from'
        }
        references = {(e.source, e.target) for e in all_edges if e.attrs.get('role') == 'reference'}
        return columns, references

    assert edges(rendered.graph) == edges(project.graph)
    assert rendered.graph.warnings == []


def test_every_claimed_edge_is_confirmed_on_the_data(project: Project, jaffle_shop: Path) -> None:
    # The README and the paper quote this result: every column edge the build claims is confirmed
    # by running the models, and nothing changes that the graph does not explain.
    totals = project.verify(warehouse=jaffle_shop / 'jaffle.duckdb', seed=1729).totals()
    assert totals['confirmed'] == totals['claimed'] and totals['unexplained'] == 0
