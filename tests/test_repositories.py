"""Several repositories built into one graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.interfaces.status import inspect

# These projects connect with their own konstruct.toml and profiles.yml, as CI on a trusted branch does.
pytestmark = pytest.mark.usefixtures('trust_config')


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
    assert '  repository   marketing (../marketing): 1 SQL file, 1 definitions file' in str(status)


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


def test_a_relation_this_repository_reads_takes_its_definition_from_the_one_that_builds_it(
    tmp_path: Path,
) -> None:
    """Built from the reader, the relation the other repository builds is one node with the
    builder's file, conditions and expressions, so formulas compose across the two."""
    reader, builder = tmp_path / 'reader', tmp_path / 'builder'
    reader.mkdir()
    builder.mkdir()
    (builder / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders '
        "where status <> 'void' group by customer_id"
    )
    (reader / 'report.sql').write_text(
        'create table main.report as select total * 2 as doubled from main.ltv'
    )
    (reader / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../builder"]\n')
    project = Project(reader)
    graph = project.build(warehouses=False, run_dbt=False).graph
    ltv = graph.nodes['table.main.ltv']
    assert (ltv.path, ltv.attrs['repository']) == ('../builder/ltv.sql', 'builder')
    assert ltv.attrs['conditions'] == [
        'GROUP BY: raw.orders.customer_id',
        "WHERE: raw.orders.status <> 'void'",
    ]
    assert graph.nodes['table.main.ltv#total'].attrs['expression'] == 'SUM(raw.orders.amount)'
    assert 'repository' not in graph.nodes['table.main.report'].attrs
    assert project.explain('report.doubled').composed == 'SUM(orders.amount) * 2'


def test_the_builder_marks_a_relation_whichever_repository_is_merged_first(tmp_path: Path) -> None:
    """Merged before the builder or after it, the reader leaves the same graph: the relation and
    the columns the builder builds are the builder's nodes, with its file, mark and attributes,
    and a column only the reader names is the reader's."""
    a, b, c = tmp_path / 'a', tmp_path / 'b', tmp_path / 'c'
    for root in (a, b, c):
        root.mkdir()
    (b / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by 1'
    )
    (c / 'report.sql').write_text(
        'create table main.report as select total * 2 as doubled, gone from main.ltv'
    )
    built = []
    for order in ('["../c", "../b"]', '["../b", "../c"]'):
        (a / 'konstruct.toml').write_text(f'dialect = "duckdb"\nrepositories = {order}\n')
        graph = Project(a).build(warehouses=False, run_dbt=False).graph
        ltv = graph.nodes['table.main.ltv']
        assert (ltv.path, ltv.attrs['repository']) == ('../b/ltv.sql', 'b')
        assert graph.nodes['table.main.ltv#gone'].attrs['repository'] == 'c'
        assert graph.nodes['table.main.report'].attrs['repository'] == 'c'
        built.append(
            {
                n.id: (n.path, n.description, n.data_type, sorted(n.origins), n.attrs)
                for n in graph.nodes.values()
                if n.id.startswith('table.main.')
            }
        )
    assert built[0] == built[1]


def _ltv_repository(root: Path, ltv: str = 'sum(amount)') -> None:
    _dbt_repository(
        root,
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
                f"select id, {ltv} as total from {{{{ ref('stg') }}}} group by id",
                ['model.shop.stg'],
                'analytics',
            ),
        ],
        [_source('shop', 'raw', 'orders', 'raw', ['id', 'amount'])],
    )


def test_plain_sql_reading_a_dbt_model_another_repository_builds(tmp_path: Path) -> None:
    """The reader's table and the builder's model are one relation in the warehouse: the model
    feeds the table, column by column, as it feeds a dbt source that reads it."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    _ltv_repository(finance)
    marketing.mkdir()
    (marketing / 'report.sql').write_text(
        'create table warehouse.marketing.report as '
        'select id, total * 2 as doubled from warehouse.analytics.ltv'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
    project = Project(marketing)
    graph = project.build(warehouses=False, run_dbt=False).graph
    [edge] = graph.out_edges('table.warehouse.analytics.ltv#total', 'derived_from')
    assert (edge.target, edge.attrs.get('same_relation')) == ('model.shop.ltv#total', True)
    assert [c.id for c in project.trace('report.doubled').columns][:2] == [
        'table.warehouse.analytics.ltv#total',
        'model.shop.ltv#total',
    ]
    assert [c.label for c in project.trace('report.doubled').columns][2:] == [
        'stg.amount',
        'raw.orders.amount',
    ]
    # Columns the reader does not name are the model's too.
    assert graph.column('table.warehouse.analytics.ltv', 'id') is not None


def test_plain_sql_naming_a_dbt_model_without_its_database(tmp_path: Path) -> None:
    """A reader that names the relation by schema and table only reads the one relation built
    under that name, so the model feeds it as it feeds a reader that names it in full."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    _ltv_repository(finance)
    marketing.mkdir()
    (marketing / 'report.sql').write_text(
        'create table marketing.report as select id, total * 2 as doubled from analytics.ltv'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
    project = Project(marketing)
    graph = project.build(warehouses=False, run_dbt=False).graph
    [edge] = graph.out_edges('table.analytics.ltv#total', 'derived_from')
    assert (edge.target, edge.attrs.get('same_relation')) == ('model.shop.ltv#total', True)
    assert [c.label for c in project.trace('report.doubled').columns][2:] == [
        'stg.amount',
        'raw.orders.amount',
    ]


def test_a_short_name_two_built_relations_end_with_is_not_linked(tmp_path: Path) -> None:
    """``main.ltv`` could be either repository's table, so neither feeds it."""
    a, b, c = tmp_path / 'a', tmp_path / 'b', tmp_path / 'c'
    for root, database in ((a, 'one'), (b, 'two')):
        root.mkdir()
        (root / 'ltv.sql').write_text(
            f'create table {database}.main.ltv as select sum(amount) as total from raw.orders'
        )
    c.mkdir()
    (c / 'report.sql').write_text('create table main.report as select total * 2 as doubled from main.ltv')
    (c / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../a", "../b"]\n')
    graph = Project(c).build(warehouses=False, run_dbt=False).graph
    assert graph.out_edges('table.main.ltv#total', 'derived_from') == []


def test_a_short_name_another_relation_also_ends_with_is_not_linked(tmp_path: Path) -> None:
    """``orders`` could be finance's staging.orders or the raw.orders marketing also reads, so
    staging.orders does not feed it."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    finance.mkdir()
    marketing.mkdir()
    (finance / 'orders.sql').write_text('create table staging.orders as select id, amount from raw.orders')
    (marketing / 'report.sql').write_text(
        'create table marketing.report as select o.amount, r.id from orders o join raw.orders r on r.id = o.id'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../finance"]\n')
    graph = Project(marketing).build(warehouses=False, run_dbt=False).graph
    assert graph.out_edges('table.orders#amount', 'derived_from') == []


def test_two_repositories_building_one_relation_is_a_warning(tmp_path: Path) -> None:
    a, b, c = tmp_path / 'a', tmp_path / 'b', tmp_path / 'c'
    for root in (a, b):
        root.mkdir()
        (root / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    c.mkdir()
    (c / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../a", "../b"]\n')
    (c / 'report.sql').write_text('create table main.report as select total from main.ltv')
    graph = Project(c).build(warehouses=False, run_dbt=False).graph
    assert "repositories: main.ltv is built by both a and b; the graph keeps a's definition" in graph.warnings


def test_formulas_compose_through_a_relation_another_repository_builds(tmp_path: Path) -> None:
    """Read as a dbt source or by plain SQL, the relation's columns have no formula of their own:
    they are the builder's columns, so formulas compose through to the builder's sources."""
    finance, marketing, sql = tmp_path / 'finance', tmp_path / 'marketing', tmp_path / 'sql'
    _ltv_repository(finance)
    _dbt_repository(
        marketing,
        'shop',
        [
            _model(
                'shop',
                'report',
                "select id, total * 2 as doubled from {{ source('fin', 'ltv') }}",
                ['source.shop.fin.ltv'],
                'marketing',
            )
        ],
        [_source('shop', 'fin', 'ltv', 'analytics', [])],
    )
    sql.mkdir()
    (sql / 'report.sql').write_text(
        'create table warehouse.marketing.report as '
        'select id, total * 2 as doubled from warehouse.analytics.ltv'
    )
    for root in (marketing, sql):
        (root / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
        project = Project(root)
        project.build(warehouses=False, run_dbt=False)
        explained = project.explain('report.doubled')
        assert (explained.composed, explained.complete) == ('SUM(raw.orders.amount) * 2', True)
        assert explained.sources == ['raw.orders.amount']
