from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph, Node
from konstruct.inputs import dbt

pytestmark = pytest.mark.dbt


@pytest.fixture
def loaded(jaffle_with_artefacts: Path) -> tuple[Graph, dbt.DbtLoad]:
    graph = Graph()
    graph.meta['root'] = str(jaffle_with_artefacts)
    project = dbt.find_projects(jaffle_with_artefacts)[0]
    dbt.ensure_artefacts(project, run_dbt=False, graph=graph)
    result = dbt.load(project, graph, dialect=None)
    assert result is not None
    return graph, result


def test_find_projects_skips_packages_and_targets(tmp_path: Path) -> None:
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'dbt_project.yml').write_text('name: alpha\nmodel-paths: [src]\n')
    for ignored in ('dbt_packages/pkg', 'a/target/x', '.venv/lib'):
        (tmp_path / ignored).mkdir(parents=True)
        (tmp_path / ignored / 'dbt_project.yml').write_text('name: ignored\n')
    projects = dbt.find_projects(tmp_path)
    assert [p.name for p in projects] == ['alpha']
    assert projects[0].source_paths[0] == tmp_path / 'a' / 'src'


def test_ensure_artefacts_generates_a_compiled_manifest_and_catalog(jaffle: Path, dbt_on_path: None) -> None:
    graph = Graph()
    project = dbt.find_projects(jaffle)[0]
    assert not project.manifest_path.exists()
    dbt.ensure_artefacts(project, run_dbt=True, graph=graph)
    result = dbt.load(project, graph, dialect=None)
    assert result is not None
    assert result.project.manifest_path.is_file()
    assert result.project.catalog_path.is_file()
    assert result.dialect == 'duckdb'
    assert (result.default_catalog, result.default_schema) == ('jaffle_shop', 'main')
    assert all(m.compiled for m in result.models)
    assert graph.warnings == []


def test_models_seeds_and_relations(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    orders = graph.nodes['model.jaffle_shop.orders']
    assert orders.kind == 'model'
    assert orders.relation == 'jaffle_shop.main.orders'
    assert orders.path == 'models/orders.sql'
    assert orders.attrs['materialized'] == 'table'
    assert orders.description and 'orders' in orders.description
    assert graph.nodes['seed.jaffle_shop.raw_payments'].kind == 'seed'
    assert (
        graph.node_for_relation('jaffle_shop.main.stg_orders') is graph.nodes['model.jaffle_shop.stg_orders']
    )
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    assert ('model.jaffle_shop.stg_payments', 'model.jaffle_shop.orders') in feeds
    assert ('seed.jaffle_shop.raw_orders', 'model.jaffle_shop.stg_orders') in feeds


def test_columns_come_from_the_catalog_in_order_with_docs(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    columns = graph.columns_of('model.jaffle_shop.orders')
    assert [c.name for c in columns][:4] == ['order_id', 'customer_id', 'order_date', 'status']
    assert 'amount' in [c.name for c in columns]
    order_id = graph.column('model.jaffle_shop.orders', 'order_id')
    assert order_id is not None
    assert order_id.data_type == 'INTEGER'
    assert order_id.description == 'This is a unique identifier for an order'


def test_tests_attach_to_columns(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    tests = [n for n in graph.nodes.values() if n.kind == 'test']
    assert len(tests) >= 20
    unique = next(n for n in tests if n.name == 'unique_orders_order_id')
    assert unique.attrs['test'] == 'unique'
    assert [e.target for e in graph.out_edges(unique.id, 'tests')] == ['model.jaffle_shop.orders#order_id']
    relationship = next(n for n in tests if n.attrs.get('test') == 'relationships' and 'orders' in n.name)
    targets = {e.target: e.attrs.get('role') for e in graph.out_edges(relationship.id, 'tests')}
    assert targets == {
        'model.jaffle_shop.orders#customer_id': None,
        'model.jaffle_shop.customers#customer_id': 'relationship target',
    }


def test_exposures(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    exposure = graph.nodes['exposure.jaffle_shop.customer_dashboard']
    assert exposure.attrs == {
        'type': 'dashboard',
        'label': 'Customer dashboard',
        'maturity': 'medium',
        'owner': 'Analytics',
    }
    assert {e.source for e in graph.in_edges(exposure.id, 'feeds')} == {
        'model.jaffle_shop.customers',
        'model.jaffle_shop.orders',
    }


@pytest.mark.parametrize(
    ('selectors', 'excludes', 'expected'),
    [
        (['staging'], [], {'stg_customers', 'stg_orders', 'stg_payments'}),
        (['customers', 'orders'], [], {'customers', 'orders'}),
        (['+orders'], [], {'orders', 'stg_orders', 'stg_payments', 'raw_orders', 'raw_payments'}),
        (['1+orders'], [], {'orders', 'stg_orders', 'stg_payments'}),
        (['stg_payments+'], [], {'stg_payments', 'orders', 'customers'}),
        (['path:models/staging'], [], {'stg_customers', 'stg_orders', 'stg_payments'}),
        (['models/staging/stg_orders.sql'], [], {'stg_orders'}),
        (['resource_type:seed'], [], {'raw_customers', 'raw_orders', 'raw_payments'}),
        (['stg_*'], ['stg_orders'], {'stg_customers', 'stg_payments'}),
        (['jaffle_shop.staging.stg_orders'], [], {'stg_orders'}),
        (['staging,stg_orders+'], [], {'stg_orders'}),
        (['config.materialized:view'], [], {'stg_customers', 'stg_orders', 'stg_payments'}),
        (['tag:nightly'], [], set()),
    ],
)
def test_select_nodes(
    loaded: tuple[Graph, dbt.DbtLoad], selectors: list[str], excludes: list[str], expected: set[str]
) -> None:
    graph, _ = loaded
    assert {n.name for n in dbt.select_nodes(graph, selectors, excludes)} == expected


def test_selectors_match_as_every_node_compared_would(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    """The index of what selectors name finds the nodes that comparing each node with the selector
    finds, for parts of names, paths and fqns, any case and wildcards."""
    graph, _ = loaded
    graph.nodes['model.jaffle_shop.orders'].attrs['tags'] = ['Nightly', 'finance']
    graph.changed()
    nodes = [
        n for n in graph.nodes.values() if 'dbt' in n.origins and n.kind in ('model', 'seed', 'snapshot')
    ]
    values = [
        'orders', 'ORDERS', 'stg', 'stg_*', 'staging', 'jaffle_shop', 'jaffle_shop.staging',
        'jaffle_shop.staging.stg_orders', 'staging.stg_orders', 'models', 'models/', 'models/sta',
        'models/staging', 'staging/stg_orders.sql', 'stg_orders.sql', 'seeds/raw_orders.csv', 'mod',
        'stg_orders', 's?g_orders', '[sr]*', 'nightly', 'Nightly', 'night*', 'finance', 'seed', 'model',
        'view', 'table', '', '/', 'models/staging/', 'jaffle_shop.*',
    ]  # fmt: skip
    methods = ['default', 'fqn', 'tag', 'path', 'resource_type', 'package', 'config.materialized', 'other']
    for method in methods:
        for value in values:
            selector = value if method == 'default' else f'{method}:{value}'
            expected = {n.id for n in nodes if dbt._matches(method, value, n)}
            assert {n.id for n in dbt.select_nodes(graph, [selector], [])} == expected, selector


def test_selection_sees_nodes_added_after_it(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    assert dbt.select_nodes(graph, ['late'], []) == []
    graph.add_node(Node(id='model.jaffle_shop.late', kind='model', name='late', origins=['dbt']))
    assert [n.id for n in dbt.select_nodes(graph, ['late'], [])] == ['model.jaffle_shop.late']


def test_selection_sees_a_path_given_in_place_after_it() -> None:
    """A build sets a node's path where it had none (a SQL file that writes it), without adding
    a node: selectors by path match it from then on."""
    graph = Graph()
    late = graph.add_node(Node('model.shop.late', 'model', 'late', origins=['dbt']))
    graph.add_node(Node('model.shop.early', 'model', 'early', origins=['dbt'], path='models/early.sql'))
    assert [n.id for n in dbt.select_nodes(graph, ['path:models'], [])] == ['model.shop.early']
    late.path = 'models/late.sql'
    assert sorted(n.id for n in dbt.select_nodes(graph, ['path:models'], [])) == [
        'model.shop.early',
        'model.shop.late',
    ]
    assert [n.id for n in dbt.select_nodes(graph, ['models/late.sql'], [])] == ['model.shop.late']


def test_select_nothing_means_everything(loaded: tuple[Graph, dbt.DbtLoad]) -> None:
    graph, _ = loaded
    assert len(dbt.select_nodes(graph, [], [])) == 8


def test_stale_manifest_is_regenerated(jaffle: Path, dbt_on_path: None) -> None:
    project = dbt.find_projects(jaffle)[0]
    assert dbt.artefacts_stale(project)
    dbt.ensure_artefacts(project, run_dbt=True, graph=Graph())
    assert not dbt.artefacts_stale(project)
    model = project.root / 'models' / 'staging' / 'stg_orders.sql'
    later = project.manifest_path.stat().st_mtime + 0.5
    os.utime(model, (later, later))
    assert dbt.artefacts_stale(project)
    dbt.ensure_artefacts(project, run_dbt=True, graph=Graph())
    assert not dbt.artefacts_stale(project)


def test_a_deleted_or_moved_model_makes_the_manifest_stale(tmp_path: Path) -> None:
    """Deleting or moving a file leaves no file newer than the manifest, but the manifest names a
    file that is gone; a file dbt never reads (an editor's swap file) changes nothing."""
    (tmp_path / 'dbt_project.yml').write_text('name: shop\n')
    models = tmp_path / 'models' / 'staging'
    models.mkdir(parents=True)
    (models / 'a.sql').write_text('select 1 as x')
    project = dbt.find_projects(tmp_path)[0]
    project.manifest_path.parent.mkdir()
    node = {'package_name': 'shop', 'original_file_path': 'models/staging/a.sql'}
    package = {'package_name': 'dbt_utils', 'original_file_path': 'macros/gone.sql'}
    project.manifest_path.write_text(json.dumps({'nodes': {'model.shop.a': node, 'macro.x': package}}))
    # Compiled after the files it read were last written, and before anything below happens.
    past = project.manifest_path.stat().st_mtime - 60
    for path in (tmp_path / 'dbt_project.yml', models / 'a.sql'):
        os.utime(path, (past - 10, past - 10))
    os.utime(project.manifest_path, (past, past))
    assert not dbt.artefacts_stale(project)
    (models / '.a.sql.swp').write_text('swap')
    (tmp_path / 'models' / '.DS_Store').write_text('')
    assert not dbt.artefacts_stale(project)
    (models / 'a.sql').rename(tmp_path / 'models' / 'a.sql')
    assert dbt.artefacts_stale(project)


def test_missing_manifest_without_dbt_is_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'dbt_project.yml').write_text('name: nothing\n')
    project = dbt.find_projects(tmp_path)[0]
    graph = Graph()
    dbt.ensure_artefacts(project, run_dbt=False, graph=graph)
    assert 'running dbt is disabled' in graph.warnings[0]
    monkeypatch.setenv('PATH', str(tmp_path))
    graph = Graph()
    dbt.ensure_artefacts(project, run_dbt=True, graph=graph)
    assert 'dbt is not on PATH' in graph.warnings[0]
    assert dbt.load(project, graph, dialect=None) is None


# A hand-written manifest covers what jaffle_shop does not: sources, uncompiled SQL and packages.


def _manifest() -> dict[str, Any]:
    def model(name: str, raw: str, deps: list[str], **extra: Any) -> dict[str, Any]:
        return {
            'unique_id': f'model.shop.{name}',
            'resource_type': 'model',
            'name': name,
            'alias': name,
            'package_name': 'shop',
            'database': 'warehouse',
            'schema': 'analytics',
            'relation_name': f'"warehouse"."analytics"."{name}"',
            'original_file_path': f'models/{name}.sql',
            'fqn': ['shop', name],
            'raw_code': raw,
            'language': 'sql',
            'depends_on': {'nodes': deps, 'macros': []},
            'config': {'materialized': 'view'},
            'tags': ['nightly'],
            'columns': {},
            **extra,
        }

    return {
        'metadata': {'adapter_type': 'postgres', 'invocation_id': 'abc'},
        'nodes': {
            'model.shop.stg_events': model(
                'stg_events',
                "select id, {{ dbt_utils.star(x) }} kind from {{ source('app', 'events') }} {% if is_incremental() %}where 1=1{% endif %}",
                ['source.shop.app.events'],
                columns={'id': {'name': 'id', 'description': 'Event id', 'data_type': 'bigint'}},
            ),
            'model.shop.daily': model(
                'daily',
                "select kind, count(*) as n from {{ ref('stg_events') }} group by 1",
                ['model.shop.stg_events'],
            ),
            'model.shop.py_model': {
                **model('py_model', 'def model(dbt, session): ...', []),
                'language': 'python',
            },
        },
        'sources': {
            'source.shop.app.events': {
                'unique_id': 'source.shop.app.events',
                'resource_type': 'source',
                'name': 'events',
                'identifier': 'raw_events',
                'source_name': 'app',
                'package_name': 'shop',
                'database': 'warehouse',
                'schema': 'app',
                'relation_name': '"warehouse"."app"."raw_events"',
                'description': '',
                'source_description': 'Application database',
                'columns': {'id': {'name': 'id', 'description': '', 'data_type': None}},
            }
        },
        'exposures': {},
    }


def test_uncompiled_manifest_renders_refs_and_sources(tmp_path: Path) -> None:
    (tmp_path / 'dbt_project.yml').write_text('name: shop\n')
    (tmp_path / 'target').mkdir()
    (tmp_path / 'target' / 'manifest.json').write_text(json.dumps(_manifest()))
    project = dbt.find_projects(tmp_path)[0]
    graph = Graph()
    result = dbt.load(project, graph, dialect=None)
    assert result is not None
    assert result.dialect == 'postgres'
    by_id = {m.node_id: m for m in result.models}
    assert set(by_id) == {'model.shop.stg_events', 'model.shop.daily'}
    stg = by_id['model.shop.stg_events']
    assert not stg.compiled
    assert '"warehouse"."app"."raw_events"' in stg.sql
    assert 'is_incremental' not in stg.sql
    assert '"warehouse"."analytics"."stg_events"' in by_id['model.shop.daily'].sql
    source = graph.nodes['source.shop.app.events']
    assert source.name == 'raw_events'
    assert source.relation == 'warehouse.app.raw_events'
    assert source.description == 'Application database'
    column = graph.column('model.shop.stg_events', 'id')
    assert column is not None and column.description == 'Event id' and column.data_type == 'bigint'
    assert not result.has_catalog
    assert result.counts == {'model': 3, 'seed': 0, 'snapshot': 0, 'test': 0, 'source': 1, 'exposure': 0}


def test_render_refs_with_packages_and_versions() -> None:
    manifest = _manifest()
    sql = (
        "select * from {{ ref('shop', 'daily') }} join {{ ref('stg_events', v=2) }} join {{ ref('unknown') }}"
    )
    rendered = dbt.render_refs(sql, manifest)
    assert rendered == (
        'select * from "warehouse"."analytics"."daily" join "warehouse"."analytics"."stg_events" join unknown'
    )


def test_parse_relation_name() -> None:
    assert dbt.parse_relation_name('"DB"."Sch"."T"', 'snowflake') == 'db.sch.t'
    assert dbt.parse_relation_name('`proj`.`ds`.`t`', 'bigquery') == 'proj.ds.t'
    assert dbt.parse_relation_name(None, None) is None


MALFORMED = [
    {'tags': None},
    {'tags': [1]},
    {'tags': [None, 'nightly']},
    {'tags': 'nightly'},
    {'fqn': [None]},
    {'fqn': None},
    {'fqn': ['big', None, 'odd']},
    {'fqn': [['big'], 'odd']},
    {'materialized': ['table']},
    {'materialized': {'kind': 'table'}},
    {'package': ['big']},
    {'dbt_name': 7},
]


@pytest.mark.parametrize('attrs', MALFORMED)
def test_a_node_with_an_attribute_that_is_not_a_name_is_skipped_by_selectors(attrs: dict[str, Any]) -> None:
    """A manifest can hold anything (``"tags": null``, a number for a tag): what a selector does
    not compare as a name it does not match, and the build does not fail on it."""
    graph = Graph()
    graph.add_node(
        Node('model.big.odd', 'model', 'odd', origins=['dbt'], path='models/odd.sql', attrs=dict(attrs))
    )
    fine = {'tags': ['nightly'], 'fqn': ['big', 'fine'], 'materialized': 'table', 'package': 'big'}
    graph.add_node(
        Node('model.big.fine', 'model', 'fine', origins=['dbt'], path='models/fine.sql', attrs=fine)
    )
    nodes = list(graph.nodes.values())
    for selector in [
        'odd', 'fine', 'big', 'big.odd', 'big.fine', 'fqn:big.*', 'tag:nightly', 'tag:night*', '*',
        'config.materialized:table', 'package:big', 'path:models', 'models/odd.sql', 'resource_type:model',
    ]:  # fmt: skip
        method, _, value = selector.rpartition(':')
        expected = {n.id for n in nodes if dbt._matches(method or 'default', value, n)}
        assert {n.id for n in dbt.select_nodes(graph, [selector], [])} == expected, selector
        assert 'model.big.fine' in expected or selector in ('odd', 'big.odd', 'models/odd.sql'), selector


def test_a_manifest_with_null_tags_builds_and_its_dag_runs_its_models(
    jaffle_with_artefacts: Path, tmp_path: Path
) -> None:
    import shutil

    from konstruct import Project

    jaffle = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_with_artefacts, jaffle)
    manifest_path = jaffle / 'target' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for node in manifest['nodes'].values():
        node['tags'] = None
    manifest_path.write_text(json.dumps(manifest))
    graph = Project(jaffle).build(run_dbt=False, warehouses=False).graph
    staging = [e.target for e in graph.edges if e.kind == 'runs' and e.source.endswith('dbt_run_staging')]
    assert sorted(staging) == [
        'model.jaffle_shop.stg_customers',
        'model.jaffle_shop.stg_orders',
        'model.jaffle_shop.stg_payments',
    ]


def test_select_nodes_ignores_non_dbt_nodes() -> None:
    graph = Graph()
    graph.add_node(Node('table.x', 'table', 'x'))
    assert dbt.select_nodes(graph, [], []) == []


def test_primary_key_constraints() -> None:
    model = {
        'constraints': [
            {'type': 'not_null', 'columns': ['a']},
            {'type': 'primary_key', 'columns': ['a', 'b']},
        ]
    }
    assert dbt._primary_key(model) == ['a', 'b']
    column = {'columns': {'id': {'name': 'ID', 'constraints': [{'type': 'primary_key'}]}}}
    assert dbt._primary_key(column) == ['ID']
    assert dbt._primary_key({'columns': {'id': {'constraints': [{'type': 'unique'}]}}}) == []
    both = {'columns': {c: {'constraints': [{'type': 'primary_key'}]} for c in ('order_id', 'line')}}
    assert dbt._primary_key(both) == ['order_id', 'line']


def test_dbt_runs_with_its_usage_statistics_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'dbt_project.yml').write_text('name: nothing\n')
    project = dbt.find_projects(tmp_path)[0]
    seen: list[dict[str, str]] = []

    def run(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs['env'])
        raise FileNotFoundError

    monkeypatch.setattr('shutil.which', lambda name: '/bin/dbt')
    monkeypatch.setattr('subprocess.run', run)
    monkeypatch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'true')
    with pytest.raises(FileNotFoundError):
        dbt.ensure_artefacts(project, run_dbt=True, graph=Graph())
    assert seen[0]['DBT_SEND_ANONYMOUS_USAGE_STATS'] == 'false'
    assert seen[0]['DO_NOT_TRACK'] == '1'


def test_dbt_semantic_layer_metrics_are_read_from_the_manifest() -> None:
    manifest = {
        'semantic_models': {
            'semantic_model.shop.orders': {
                'name': 'orders',
                'original_file_path': 'models/semantic.yml',
                'depends_on': {'nodes': ['model.shop.orders']},
                'measures': [
                    {'name': 'order_total', 'agg': 'sum', 'expr': 'amount'},
                    {'name': 'orders', 'agg': 'count'},
                ],
            }
        },
        'metrics': {
            'metric.shop.revenue': {
                'name': 'revenue',
                'label': 'Revenue',
                'type': 'simple',
                'original_file_path': 'models/semantic.yml',
                'type_params': {
                    'measure': {'name': 'order_total'},
                    'input_measures': [{'name': 'order_total'}],
                },
                'filter': {
                    'where_filters': [{'where_sql_template': "{{ Dimension('order__status') }} = 'done'"}]
                },
            }
        },
    }
    found = dbt.semantic_metrics(manifest, lambda item: item.get('original_file_path'))
    # The measure no metric names is not listed.
    assert found == [
        {
            'name': 'revenue',
            'label': 'Revenue',
            'type': 'simple',
            'agg': 'sum',
            'expr': 'amount',
            'model': 'model.shop.orders',
            'filters': ["{{ Dimension('order__status') }} = 'done'"],
            'path': 'models/semantic.yml',
        },
    ]
    assert dbt.semantic_metrics({}, lambda item: None) == []


def test_a_model_the_catalog_does_not_list_keeps_its_columns_for_sql_that_reads_it(tmp_path: Path) -> None:
    """A model built after the catalog was generated is in the manifest but not the catalog: its
    documented columns are its SQL's own, and SQL that reads it still has their lineage."""
    from konstruct import Project

    (tmp_path / 'models').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: shop\nprofile: shop\n')
    sql = "select id as customer_id, name as customer_name, 'new' as segment from raw.customers"
    (tmp_path / 'models' / 'customers.sql').write_text(sql)
    model = {
        'unique_id': 'model.shop.customers',
        'resource_type': 'model',
        'name': 'customers',
        'alias': 'customers',
        'package_name': 'shop',
        'database': 'wh',
        'schema': 'main',
        'relation_name': '"wh"."main"."customers"',
        'original_file_path': 'models/customers.sql',
        'raw_code': sql,
        'compiled_code': sql,
        'language': 'sql',
        'depends_on': {'nodes': []},
        'columns': {'customer_id': {'name': 'customer_id'}, 'customer_name': {'name': 'customer_name'}},
    }
    (tmp_path / 'target').mkdir()
    (tmp_path / 'target' / 'manifest.json').write_text(
        json.dumps(
            {'metadata': {'adapter_type': 'duckdb'}, 'nodes': {model['unique_id']: model}, 'sources': {}}
        )
    )
    (tmp_path / 'target' / 'catalog.json').write_text(json.dumps({'nodes': {}, 'sources': {}}))
    (tmp_path / 'report.sql').write_text(
        'create table wh.main.report as select c.customer_id, c.segment from wh.main.customers as c'
    )
    graph = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    assert not [w for w in graph.warnings if 'lineage failed' in w]
    report = next(n for n in graph.relations() if n.name == 'report')
    column = graph.column(report.id, 'customer_id')
    assert column is not None
    assert [e.target for e in graph.out_edges(column.id, 'derived_from')] == [
        'model.shop.customers#customer_id'
    ]
