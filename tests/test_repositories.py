"""Several repositories built into one graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from konstruct import Project
from konstruct.interfaces.status import inspect


def test_lineage_crosses_repositories(tmp_path: Path) -> None:
    finance = tmp_path / 'finance'
    marketing = tmp_path / 'marketing'
    finance.mkdir()
    marketing.mkdir()
    (finance / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by customer_id'
    )
    (marketing / 'report.sql').write_text(
        'create table main.report as select customer_id, total * 2 as doubled from main.ltv'
    )
    (marketing / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Doubled value, definition: Twice the total., columns: [main.report.doubled]}\n'
    )
    (finance / 'konstruct.toml').write_text(
        'dialect = "duckdb"\nrepositories = ["../marketing", "../missing"]\n'
    )
    project = Project(finance)
    result = project.build(warehouses=False, run_dbt=False)
    graph = result.graph
    assert [c.label for c in project.trace('report.doubled').columns] == ['ltv.total', 'orders.amount']
    assert project.impact('orders.amount').columns[-1].label == 'report.doubled'
    report = next(n for n in graph.relations() if n.relation == 'main.report')
    assert report.attrs['repository'] == 'marketing'
    assert graph.nodes['term.doubled_value'].path == '../marketing/definitions.yml'
    assert graph.meta['repositories'] == ['../marketing']
    assert '../marketing/report.sql' in graph.meta['watched']
    assert any(s.get('repository') == 'marketing' for s in graph.meta['sources'])
    assert 'repositories: ../missing is not a directory' in graph.warnings
    assert (finance / 'konstruct-out' / 'repositories' / 'marketing').is_dir()
    assert not (marketing / 'konstruct-out').exists()
    status = inspect(finance)
    assert [(r['name'], r['path'], r['found']) for r in status.repositories] == [
        ('marketing', '../marketing', '1 SQL file, 1 definitions file'),
        ('missing', '../missing', 'not a directory'),
    ]
    assert 'repository  marketing (../marketing): 1 SQL file, 1 definitions file' in str(status)


def _model(package: str, name: str, raw: str, depends: list[str], schema: str) -> dict[str, Any]:
    return {
        'unique_id': f'model.{package}.{name}',
        'resource_type': 'model',
        'name': name,
        'alias': name,
        'package_name': package,
        'database': 'warehouse',
        'schema': schema,
        'relation_name': f'"warehouse"."{schema}"."{name}"',
        'original_file_path': f'models/{name}.sql',
        'fqn': [package, name],
        'raw_code': raw,
        'language': 'sql',
        'depends_on': {'nodes': depends, 'macros': []},
        'config': {'materialized': 'table'},
        'tags': [],
        'columns': {},
    }


def _source(package: str, source: str, name: str, schema: str, columns: list[str]) -> dict[str, Any]:
    return {
        'unique_id': f'source.{package}.{source}.{name}',
        'resource_type': 'source',
        'name': name,
        'identifier': name,
        'source_name': source,
        'package_name': package,
        'database': 'warehouse',
        'schema': schema,
        'relation_name': f'"warehouse"."{schema}"."{name}"',
        'columns': {c: {'name': c} for c in columns},
    }


def _dbt_repository(
    root: Path, package: str, nodes: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> None:
    (root / 'target').mkdir(parents=True)
    (root / 'models').mkdir()
    (root / 'dbt_project.yml').write_text(f'name: {package}\n')
    for node in nodes:
        (root / node['original_file_path']).write_text(node['raw_code'])
    manifest = {
        'metadata': {'adapter_type': 'postgres'},
        'nodes': {n['unique_id']: n for n in nodes},
        'sources': {s['unique_id']: s for s in sources},
        'exposures': {},
    }
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))


def test_dbt_projects_in_two_repositories(tmp_path: Path) -> None:
    """finance builds analytics.ltv; marketing reads it as a source. Both packages are called
    shop and both have a model stg, in different schemas: they stay two models."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    _dbt_repository(
        finance,
        'shop',
        [
            _model(
                'shop',
                'stg',
                "select id, amount from {{ source('raw', 'orders') }}",
                ['source.shop.raw.orders'],
                'analytics',
            ),
            _model(
                'shop',
                'ltv',
                "select id, sum(amount) as total from {{ ref('stg') }} group by id",
                ['model.shop.stg'],
                'analytics',
            ),
        ],
        [_source('shop', 'raw', 'orders', 'raw', ['id', 'amount'])],
    )
    _dbt_repository(
        marketing,
        'shop',
        [
            _model(
                'shop',
                'stg',
                "select id, clicks from {{ source('web', 'events') }}",
                ['source.shop.web.events'],
                'marketing',
            ),
            _model(
                'shop',
                'report',
                "select id, total * 2 as doubled from {{ source('fin', 'ltv') }}",
                ['source.shop.fin.ltv'],
                'marketing',
            ),
        ],
        [
            _source('shop', 'fin', 'ltv', 'analytics', []),
            _source('shop', 'web', 'events', 'web', ['id', 'clicks']),
        ],
    )
    (finance / 'konstruct.toml').write_text('repositories = ["../marketing"]\n')
    project = Project(finance)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert graph.nodes['model.shop.stg'].relation == 'warehouse.analytics.stg'
    assert graph.nodes['model.shop.stg@marketing'].relation == 'warehouse.marketing.stg'
    assert [c.name for c in graph.columns_of('model.shop.stg')] == ['id', 'amount']
    assert [c.name for c in graph.columns_of('model.shop.stg@marketing')] == ['id', 'clicks']
    assert [c.id for c in project.impact('events.clicks').columns] == ['model.shop.stg@marketing#clicks']
    # The model finance builds is the source marketing reads, column by column.
    assert [c.label for c in project.trace('report.doubled').columns] == [
        'fin.ltv.total',
        'ltv.total',
        'stg.amount',
        'raw.orders.amount',
    ]
    dbt_sources = [s for s in graph.meta['sources'] if s['input'] == 'dbt']
    assert [(s['path'], s.get('repository')) for s in dbt_sources] == [
        ('.', None),
        ('../marketing', 'marketing'),
    ]


def test_build_options_apply_to_every_repository(tmp_path: Path) -> None:
    first, second, third = (tmp_path / p / 'analytics' for p in 'abc')
    for root in (first, second, third):
        root.mkdir(parents=True)
        (root / 'x.sql').write_text(f'create table main.{root.parent.name} as select 1 as n')
    con = duckdb.connect(str(second / 'shop.duckdb'))
    con.execute('create table people (id int)')
    con.close()
    (first / 'konstruct.toml').write_text('repositories = ["../../b/analytics", "../../c/analytics"]\n')
    result = Project(first).build(warehouses=False, run_dbt=False)
    assert not any(s['input'] == 'duckdb' for s in result.graph.meta['sources'])
    assert result.inputs['sql_files'] == 3
    assert (first / 'konstruct-out' / 'repositories' / 'analytics').is_dir()
    assert (first / 'konstruct-out' / 'repositories' / 'c-analytics').is_dir()
    sources = Project(first).build(run_dbt=False).graph.meta['sources']
    assert [s.get('repository') for s in sources if s['input'] == 'duckdb'] == ['analytics']


def test_terms_bind_to_columns_another_repository_builds(tmp_path: Path) -> None:
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'x.sql').write_text('create table main.x as select amount, region from raw.sales')
    (b / 'y.sql').write_text('create table main.y as select amount * 2 as doubled, region from main.x')
    (a / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Doubled\n    definition: Twice the amount.\n'
        '    columns: [main.y.doubled]\n    filters: ["region = \'EU\'"]\n'
    )
    (b / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Amount, definition: The sale., columns: [main.x.amount]}\n'
    )
    (a / 'konstruct.toml').write_text('repositories = ["../b"]\n')
    graph = Project(a).build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges('term.doubled', 'defines')] == [
        'y.doubled'
    ]
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges('term.amount', 'defines')] == [
        'x.amount'
    ]
    assert not [w for w in graph.warnings if 'bound column' in w or 'required filter' in w]


def test_select_star_over_another_repositorys_relation(tmp_path: Path) -> None:
    """Each repository's SQL is analysed before the graphs are merged, so a star over a relation
    another repository builds is not expanded; columns the SQL names are linked."""
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'x.sql').write_text('create table main.x as select amount, qty from raw.sales')
    (b / 'y.sql').write_text('create table main.y as select * from main.x')
    (b / 'z.sql').write_text('create table main.z as select qty from main.x')
    (a / 'konstruct.toml').write_text('repositories = ["../b"]\n')
    project = Project(a)
    graph = project.build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    assert any('SELECT * over relations with unknown columns: main.x' in w for w in graph.warnings)
    assert [c.label for c in project.impact('x.qty').columns] == ['z.qty']


def test_status_does_not_follow_the_repositories_others_list(tmp_path: Path) -> None:
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'konstruct.toml').write_text('repositories = ["../b"]\n')
    (b / 'konstruct.toml').write_text('repositories = ["../a"]\n')
    (b / 'x.sql').write_text('create table main.x as select 1 as n')
    status = inspect(a)
    assert [(r['name'], r['found']) for r in status.repositories] == [('b', '1 SQL file')]


def test_a_term_two_repositories_define(tmp_path: Path) -> None:
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'x.sql').write_text('create table main.x as select amount from raw.sales')
    (b / 'y.sql').write_text('create table main.y as select amount * 2 as doubled from main.x')
    (a / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Revenue, definition: Doubled., columns: [main.y.doubled]}\n'
    )
    (b / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Revenue, definition: Doubled., columns: [main.y.doubled, main.y.nothing]}\n'
    )
    (a / 'konstruct.toml').write_text('repositories = ["../b"]\n')
    graph = Project(a).build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    for term in ('term.revenue', 'term.revenue@b'):
        assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges(term, 'defines')] == ['y.doubled']
    bound = [w for w in graph.warnings if 'bound column' in w]
    assert (
        len(bound) == 1
        and bound[0].startswith('b: definitions: definitions.yml:3:')
        and 'main.y.nothing' in bound[0]
    )
    assert (
        "definitions: term 'Revenue' is defined in more than one repository "
        '(../b/definitions.yml:3, definitions.yml:3)'
    ) in graph.warnings
    rules = {r.name: r.violations for r in Project(a).ontology().rules}
    [missing] = rules['bound-columns-exist']
    assert missing.startswith("b: definitions.yml:3: term 'Revenue': bound column 'main.y.nothing'")
