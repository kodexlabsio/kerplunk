"""Loader configuration files: the source tables each loader reads and the tables it lands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from konstruct import Project
from konstruct.engine.config import discover_ingestion_files, load_config
from konstruct.inputs import hcl
from konstruct.inputs.ingestion import Feed, kind_of, read, read_files

DLT_PIPELINE = """
import dlt
from dlt.sources.sql_database import sql_database, sql_table
from dlt.sources.rest_api import rest_api_source


@dlt.resource(name="issues", write_disposition="merge", columns={"id": {}, "title": {}})
def github_issues():
    yield from fetch("issues")


@dlt.resource
def pulls():
    yield []


@dlt.source
def github():
    return github_issues, pulls


pipeline = dlt.pipeline(pipeline_name="github", destination="duckdb", dataset_name="github_data")
pipeline.run(github())

shop = dlt.pipeline("shop", destination="snowflake")
shop.run(sql_database("postgresql://loader@db.internal/app", schema="public", table_names=["orders", "customers"]))
shop.run(sql_table(credentials=secrets, table="refunds"), table_name="refund")

api = rest_api_source({"client": {"base_url": "https://api.example.com/v1/"}, "resources": ["invoices", {"name": "plans", "table_name": "plan"}]})
billing = dlt.pipeline(pipeline_name="billing", dataset_name="billing")
billing.run(api)
billing.run(make_data())
"""


def feeds_of(
    kind: str, text: str, path: str = 'pipeline.py'
) -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    return [(f.loader, f.task, f.source, f.target) for f in read(kind, path, text).feeds]


def test_a_dlt_pipeline_is_read_without_running_it() -> None:
    reading = read('dlt', 'pipelines/github.py', DLT_PIPELINE)
    found = [(f.task, f.source, f.target) for f in reading.feeds]
    assert found == [
        ('github', ('dlt', 'github', 'issues'), ('github_data', 'issues')),
        ('github', ('dlt', 'github', 'pulls'), ('github_data', 'pulls')),
        ('shop', ('postgres', 'app', 'public', 'orders'), ('shop_dataset', 'orders')),
        ('shop', ('postgres', 'app', 'public', 'customers'), ('shop_dataset', 'customers')),
        ('shop', ('sql_database', 'refunds'), ('shop_dataset', 'refund')),
        ('billing', ('api.example.com', 'invoices'), ('billing', 'invoices')),
        ('billing', ('api.example.com', 'plans'), ('billing', 'plan')),
    ]
    assert reading.feeds[0].columns == (('id', 'id', 'direct'), ('title', 'title', 'direct'))
    assert reading.problems == [
        'pipelines/github.py:32: the data passed to run() comes from make_data(), which Konstruct does not read'
    ]


def test_a_dlt_pipeline_it_cannot_follow_is_a_problem() -> None:
    text = (
        'import dlt\np = dlt.pipeline(name)\nq = dlt.pipeline("q")\nq.run(sql_database(table_names=tables))\n'
    )
    reading = read('dlt', 'p.py', text)
    assert reading.feeds == []
    assert reading.problems == [
        'p.py:2: a dlt pipeline whose name is not a literal',
        'p.py:4: sql_database without literal table names: its tables are not known',
    ]
    assert read('dlt', 'bad.py', 'import dlt\npipeline(').problems[0].startswith('bad.py:2: cannot parse')


def test_a_dlt_schema_file() -> None:
    text = """
version: 3
engine_version: 9
name: github
tables:
  _dlt_loads: {columns: {}}
  issues:
    columns: {id: {data_type: bigint}}
    resource: issues
  issues__labels:
    parent: issues
    columns: {}
  pulls:
    resource: pulls
"""
    assert feeds_of('dlt_schema', text, 'schemas/github.schema.yaml') == [
        ('dlt', 'github', ('dlt', 'github', 'issues'), ('issues',)),
        ('dlt', 'github', ('dlt', 'github', 'pulls'), ('pulls',)),
    ]
    assert (
        read('dlt_schema', 'x.schema.yaml', 'a: [').problems[0].startswith('x.schema.yaml: cannot read YAML')
    )


MELTANO = """
version: 1
project_id: shop
plugins:
  extractors:
  - name: tap-postgres
    variant: meltanolabs
    config:
      database: app
      filter_schemas: [public]
    select:
    - public-orders.*
    - public-customers.id
    - public-customers.email
    - '!public-customers.email'
    - public-*.*
  - name: tap-github
    select: ['*.*']
  loaders:
  - name: target-snowflake
    config:
      database: ANALYTICS
      default_target_schema: RAW
  - name: target-jsonl
jobs:
- name: el
  tasks:
  - tap-postgres target-snowflake
schedules:
- name: github-daily
  extractor: tap-github
  loader: target-jsonl
"""


def test_meltano_pairs_each_extractor_with_its_loader() -> None:
    reading = read('meltano', 'meltano.yml', MELTANO)
    assert [(f.task, f.source, f.target, f.columns) for f in reading.feeds] == [
        (
            'tap-postgres target-snowflake',
            ('postgres', 'app', 'public', 'orders'),
            ('ANALYTICS', 'RAW', 'orders'),
            (),
        ),
        (
            'tap-postgres target-snowflake',
            ('postgres', 'app', 'public', 'customers'),
            ('ANALYTICS', 'RAW', 'customers'),
            (('id', 'id', 'direct'),),
        ),
    ]
    assert reading.problems == [
        'meltano.yml: tap-github selects no stream by name, so the tables it lands are not known'
    ]


def test_meltano_without_a_target_schema_finds_the_table_by_name() -> None:
    """A loader from Meltano Hub has its own default schema (target-postgres and target-duckdb
    take the extractor's load_schema), so without one in the file the schema is not known."""
    text = """
plugins:
  extractors: [{name: tap-mysql, config: {database: shop}, select: [shop-orders.*]}]
  loaders: [{name: target-postgres}]
"""
    assert feeds_of('meltano', text, 'meltano.yml') == [
        ('meltano', 'tap-mysql target-postgres', ('mysql', 'shop', 'shop', 'orders'), ('orders',))
    ]


def test_meltano_plugins_inherit_from_others() -> None:
    text = """
plugins:
  extractors:
  - {name: tap-postgres, namespace: pg, config: {database: app}, select: [public-orders.*]}
  - {name: tap-postgres--sales, inherit_from: tap-postgres, config: {host: sales}}
  - {name: tap-postgres--eu, inherit_from: tap-postgres--sales, select: [public-invoices.*]}
  loaders:
  - {name: target-postgres, config: {default_target_schema: $MELTANO_EXTRACT__LOAD_SCHEMA}}
jobs:
- {name: sales, tasks: [tap-postgres--sales target-postgres tap-postgres--eu target-postgres]}
"""
    assert [(f.task, f.source, f.target) for f in read('meltano', 'meltano.yml', text).feeds] == [
        ('tap-postgres--sales target-postgres', ('postgres', 'app', 'public', 'orders'), ('pg', 'orders')),
        ('tap-postgres--eu target-postgres', ('postgres', 'app', 'public', 'invoices'), ('pg', 'invoices')),
    ]


def test_a_meltano_task_pairs_each_extractor_with_the_loader_after_it() -> None:
    """A task of thousands of names is read once, without pairing every extractor with every loader."""
    import time

    extractors = ''.join(f'  - {{name: tap-{i}, select: [s-t{i}.*]}}\n' for i in range(2_000))
    loaders = ''.join(f'  - {{name: target-{i}}}\n' for i in range(2_000))
    task = (
        ' '.join(f'tap-{i}' for i in range(2_000))
        + ' target-0 '
        + ' '.join(f'target-{i}' for i in range(2_000))
    )
    text = (
        f'plugins:\n  extractors:\n{extractors}  loaders:\n{loaders}jobs:\n- name: j\n  tasks: ["{task}"]\n'
    )
    start = time.process_time()
    reading = read('meltano', 'meltano.yml', text)
    assert time.process_time() - start < 2
    assert {f.task.split()[1] for f in reading.feeds} == {'target-0'} and len(reading.feeds) == 2_000


@pytest.mark.parametrize(
    ('extractor', 'schema'),
    [
        ('{name: tap-mysql, select: [shop-orders.*]}', 'tap_mysql'),
        ('{name: tap-mysql, namespace: mysql_shop, select: [shop-orders.*]}', 'mysql_shop'),
        ('{name: tap-mysql, load_schema: raw_shop, select: [shop-orders.*]}', 'raw_shop'),
    ],
)
def test_meltanos_load_schema_names_the_landed_schema(extractor: str, schema: str) -> None:
    """A loader's schema is often ``$MELTANO_EXTRACT__LOAD_SCHEMA``: the extractor's
    ``load_schema``, which defaults to its namespace."""
    text = (
        f'plugins:\n  extractors: [{extractor}]\n'
        '  loaders: [{name: target-postgres, config: {default_target_schema: $MELTANO_EXTRACT__LOAD_SCHEMA}}]\n'
    )
    assert [f.target for f in read('meltano', 'meltano.yml', text).feeds] == [(schema, 'orders')]


CATALOG = {
    'streams': [
        {
            'tap_stream_id': 'public-orders',
            'stream': 'orders',
            'table_name': 'orders',
            'schema': {'properties': {'id': {}, 'total': {}, 'secret': {}}},
            'metadata': [
                {'breadcrumb': [], 'metadata': {'selected': True, 'schema-name': 'public'}},
                {'breadcrumb': ['properties', 'id'], 'metadata': {'inclusion': 'automatic'}},
                {'breadcrumb': ['properties', 'secret'], 'metadata': {'selected': False}},
            ],
        },
        {
            'tap_stream_id': 'public-audit',
            'stream': 'audit',
            'schema': {'properties': {'id': {}}},
            'metadata': [{'breadcrumb': [], 'metadata': {'selected': False}}],
        },
    ]
}


def test_a_singer_catalog_and_state() -> None:
    reading = read('singer_catalog', 'extract/tap-postgres.catalog.json', json.dumps(CATALOG))
    assert [(f.task, f.source, f.target, f.columns) for f in reading.feeds] == [
        (
            'tap-postgres',
            ('postgres', 'public', 'orders'),
            ('public', 'orders'),
            (('id', 'id', 'direct'), ('total', 'total', 'direct')),
        )
    ]
    state = {'singer_state': {'bookmarks': {'public-orders': {'replication_key_value': 3}, 'users': {}}}}
    assert feeds_of('singer_state', json.dumps(state), 'state/tap-postgres.state.json') == [
        ('singer', 'tap-postgres', ('postgres', 'public', 'orders'), ('public', 'orders')),
        ('singer', 'tap-postgres', ('postgres', 'users'), ('users',)),
    ]
    assert read('singer_catalog', 'c.json', '{').problems[0].startswith('c.json: cannot read JSON')


@pytest.mark.parametrize(
    ('name', 'text', 'kind'),
    [
        ('pipeline.py', 'import dlt\np = dlt.pipeline("x")\n', 'dlt'),
        ('other.py', 'import requests\nrequests.get(url)\n', None),
        ('mentions.py', '# we used to import dlt here\npipeline("x")\n', None),
        ('github.schema.yaml', 'engine_version: 9\ntables: {}\n', 'dlt_schema'),
        ('model.schema.yml', 'version: 2\nmodels: []\n', None),
        ('meltano.yml', 'plugins: {}\n', 'meltano'),
        ('catalog.json', '{"streams": [{"tap_stream_id": "a"}]}', 'singer_catalog'),
        ('state.json', '{"bookmarks": {"public-orders": {"replication_key_value": 3}}}', 'singer_state'),
        ('tap-github/state.json', '{"bookmarks": {"issues": {"since": "2026-01-01"}}}', 'singer_state'),
        ('state.json', '{"currently_syncing": null, "bookmarks": {"issues": {}}}', 'singer_state'),
        # A web application's state: bookmarks, but not a tap's.
        ('web/state.json', '{"theme": "dark", "bookmarks": {"toolbar": {"visible": true}}}', None),
        ('state.json', '{"bookmarks": {"toolbar": true}}', None),
        ('state.json', '{"bookmarks": [1]', None),
        # An e-reader's reading positions, and a fixture under a feature named tap-to-pay.
        ('reader/state.json', '{"bookmarks": {"book-12": {"position": 3051, "version": 2}}}', None),
        ('features/tap-to-pay/fixtures/state.json', '{"bookmarks": {"card": {"last": 4}}}', None),
        ('tap-github.state.json', '{"bookmarks": {"issues": {"since": "2026-01-01"}}}', 'singer_state'),
        ('package.json', '{"name": "x", "bookmarks": {}}', None),
        ('dbt_project.yml', 'name: shop\n', None),
        ('main.tf', 'resource "fivetran_connector" "pg" {}\n', 'terraform'),
        ('vpc.tf', 'resource "aws_vpc" "main" {}\n', None),
        ('configuration.yaml', 'definition_type: connection\n', 'airbyte_octavia'),
        ('settings.yaml', 'definition_type_note: x\n', None),
        ('connection.json', '{"syncCatalog": {}}', 'airbyte_api'),
        ('crm.json', '{"sourceId": "a", "destinationId": "b", "configurations": {}}', 'airbyte_api'),
        # A camera's configuration: a source and streams, but no destination.
        ('camera.json', '{"sourceId": "cam-1", "configurations": {"streams": [{"name": "front"}]}}', None),
        ('connector.json', '{"connector.class": "x"}', 'kafka_connect'),
        ('worker.properties', 'connector.class=x\n', 'kafka_connect'),
        ('log4j.properties', 'log4j.rootLogger=INFO\n', None),
        ('flow.yaml', 'captures: {}\n', 'estuary'),
        ('orders.flow.yaml', 'collections: {}\n', None),
    ],
)
def test_discovery_reads_only_the_formats_it_knows(name: str, text: str, kind: str | None) -> None:
    assert kind_of(name, text) == kind


def test_discovery_is_linear_in_blank_lines() -> None:
    """A pattern anchored at each line that also matches newlines rescans the rest of the file
    from every line: quadratic, about 6 s here."""
    import time

    start = time.process_time()
    assert kind_of('blank.py', ' \n' * 20_000) is None
    assert time.process_time() - start < 0.5


def test_feeds_survive_the_cache() -> None:
    feed = read('dlt', 'p.py', DLT_PIPELINE).feeds[0]
    assert Feed.from_dict(feed.to_dict()) == feed


def test_a_web_applications_state_file_is_not_a_singer_state(tmp_path: Path) -> None:
    (tmp_path / 'web').mkdir()
    state = {'theme': 'dark', 'bookmarks': {'toolbar': {'visible': True}, 'recent': {}}}
    (tmp_path / 'web' / 'state.json').write_text(json.dumps(state))
    assert discover_ingestion_files(load_config(tmp_path)) == []
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not [n for n in graph.nodes.values() if 'singer' in n.origins]
    assert read('singer_state', 'web/state.json', json.dumps(state)).feeds == []


NESTED_JSON = '{"streams": ' + '[' * 100_000 + ']' * 100_000 + '}'
NESTED_YAML = 'a: ' + '[' * 20_000 + ']' * 20_000
NESTED_PYTHON = 'import dlt\np = dlt.pipeline("a")\np.run(' + '+'.join(['1'] * 100_000) + ')'
# Nine levels of nine aliases: a few hundred bytes that stand for 387 million values.
ALIASES = ''.join(
    f'{level}: &{level} [' + ', '.join([f'*{chr(ord(level) - 1)}' if level > 'a' else 'x'] * 9) + ']\n'
    for level in 'abcdefghi'
)


@pytest.mark.parametrize(
    ('kind', 'path', 'text'),
    [
        ('dlt', 'p.py', NESTED_PYTHON),
        # 200,000 unary minus signs exhaust the parser's memory rather than its recursion limit.
        ('dlt', 'p.py', 'import dlt\np = dlt.pipeline("a")\np.run(' + '-' * 200_000 + 'x)'),
        (
            'dlt_schema',
            'x.schema.yaml',
            f'{ALIASES}name: *i\nengine_version: 1\ntables: {{t: {{resource: *i}}}}',
        ),
        ('meltano', 'meltano.yml', f'{ALIASES}plugins: {{extractors: [{{name: tap-x, select: *i}}]}}'),
        ('dlt', 'p.py', 'import dlt\np = dlt.pipeline("a")\np.run(rest_api_source({"resources": 3}))'),
        ('dlt_schema', 'x.schema.yaml', NESTED_YAML),
        ('dlt_schema', 'x.schema.yaml', 'engine_version: 1\ntables: 3'),
        ('meltano', 'meltano.yml', NESTED_YAML),
        ('meltano', 'meltano.yml', '42'),
        ('meltano', 'meltano.yml', 'plugins: {extractors: 3}'),
        ('meltano', 'meltano.yml', 'plugins: {}\x7f'),
        ('singer_catalog', 'tap-x.catalog.json', NESTED_JSON),
        ('singer_catalog', 'tap-x.catalog.json', '{"streams": 3}'),
        ('singer_state', 'tap-x.state.json', NESTED_JSON),
        ('singer_state', 'tap-x.state.json', '{"bookmarks": 3}'),
    ],
)
def test_a_malformed_file_is_a_problem_not_a_failure(kind: str, path: str, text: str) -> None:
    """Deep nesting and values of the wrong shape: the file gives a problem and no feeds."""
    reading = read(kind, path, text)
    assert reading.feeds == []
    assert len(reading.problems) == 1 and reading.problems[0].startswith(f'{path}: ')


def test_a_name_longer_than_any_warehouse_allows_is_not_a_name() -> None:
    text = (
        'plugins:\n  extractors: [{name: tap-x, select: [s-orders.*]}]\n  loaders: [{name: target-y, config: {default_target_schema: '
        + 'a' * 2_000
        + '}}]\n'
    )
    reading = read('meltano', 'meltano.yml', text)
    assert reading.feeds == []
    assert reading.problems == [
        'meltano.yml: tap-x target-y: a name of 2,000 characters is not a table name, so the feed is left out'
    ]


@pytest.mark.parametrize(
    ('loader_config', 'problem'),
    [
        ('default_target_schema: $TARGET_SCHEMA', '$TARGET_SCHEMA'),
        ('{default_target_schema: raw, database: $PG_DB}', '$PG_DB'),
        ('{default_target_schema: "${SCHEMA}"}', '${SCHEMA}'),
    ],
)
def test_a_meltano_setting_from_the_environment_is_not_a_name(loader_config: str, problem: str) -> None:
    config = loader_config if loader_config.startswith('{') else '{' + loader_config + '}'
    text = (
        'plugins:\n'
        '  extractors: [{name: tap-postgres, select: [public-orders.*]}]\n'
        f'  loaders: [{{name: target-duckdb, config: {config}}}]\n'
    )
    reading = read('meltano', 'meltano.yml', text)
    assert reading.feeds == []
    assert reading.problems == [
        f'meltano.yml: tap-postgres target-duckdb: {problem} is not a literal name, so the feed is left out'
    ]


def test_meltanos_extractor_namespace_names_the_schema() -> None:
    text = (
        'plugins:\n'
        '  extractors: [{name: tap-postgres, select: [public-orders.*]}]\n'
        '  loaders: [{name: target-duckdb, config: {default_target_schema: $MELTANO_EXTRACTOR_NAMESPACE}}]\n'
    )
    assert [f.target for f in read('meltano', 'meltano.yml', text).feeds] == [('tap_postgres', 'orders')]


def test_a_dlt_dataset_that_is_not_a_literal() -> None:
    text = (
        'import dlt, os\n'
        'p = dlt.pipeline("shop", dataset_name=os.environ["DATASET"])\n'
        'p.run(sql_table("postgresql://h/app", table="orders"))\n'
    )
    reading = read('dlt', 'p.py', text)
    assert reading.feeds == []
    assert reading.problems == [
        'p.py:2: dlt pipeline shop: its dataset_name is not a literal, so its tables are not known'
    ]


def test_a_malformed_file_does_not_stop_the_build(tmp_path: Path) -> None:
    (tmp_path / 'meltano.yml').write_text('plugins: {extractors: 3}\n')
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert [w for w in graph.warnings if w.startswith('meltano: meltano.yml: cannot read: ')]


def test_a_loader_task_does_not_join_an_orchestrators_task(tmp_path: Path) -> None:
    """A dbt source's loader can be named like an orchestrator (``loader: dagster``), whose
    tasks share the ``task.<name>.`` ids."""
    from konstruct.core.graph import Node
    from konstruct.engine.build import Builder

    builder = Builder(load_config(tmp_path), update=False)
    asset = builder.graph.add_node(Node(id='task.dagster.raw', kind='task', name='raw', origins=['dagster']))
    task = builder.loader_task('dagster', 'raw', schema='raw')
    assert task is not asset and task.attrs['loader'] == 'dagster'
    assert builder.loader_task('dagster', 'raw') is task
    assert 'loader' not in asset.attrs


def duckdb_warehouse(root: Path, script: str) -> None:
    import duckdb

    con = duckdb.connect(str(root / 'warehouse.duckdb'))
    con.execute(script)
    con.close()


def test_the_build_links_source_systems_to_the_tables_loaders_land(tmp_path: Path) -> None:
    duckdb_warehouse(
        tmp_path,
        'create schema shop_dataset;'
        'create table shop_dataset.orders (id bigint, total double, _dlt_load_id varchar, _dlt_id varchar);'
        'create schema raw;'
        'create table raw.customers (id bigint, email varchar, _sdc_received_at timestamp, '
        '_sdc_batched_at timestamp, _sdc_sequence bigint, _sdc_table_version bigint);',
    )
    (tmp_path / 'pipelines').mkdir()
    (tmp_path / 'pipelines' / 'shop.py').write_text(
        'import dlt\n'
        'from dlt.sources.sql_database import sql_table\n'
        'shop = dlt.pipeline("shop", destination="duckdb")\n'
        'shop.run(sql_table("postgresql://u@h/app", table="orders", schema="public"))\n'
        'shop.run(sql_table("postgresql://u@h/app", table="refunds", schema="public"))\n'
    )
    (tmp_path / 'meltano.yml').write_text(
        'plugins:\n'
        '  extractors: [{name: tap-postgres, config: {database: app}, select: [public-customers.id, public-customers.email]}]\n'
        '  loaders: [{name: target-duckdb, config: {default_target_schema: raw}}]\n'
    )
    (tmp_path / 'notes.json').write_text('{"bookmarks": "not a state file"}')
    (tmp_path / 'accounts.sql').write_text(
        'create table warehouse.main.accounts as select id, upper(email) as email from raw.customers'
    )
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    # dlt: the warehouse's sync task for the schema is the pipeline's, with the file as evidence.
    orders = 'table.warehouse.shop_dataset.orders'
    assert ('system.postgres.app.public.orders', 'task.dlt.warehouse.shop_dataset') in feeds
    assert ('task.dlt.warehouse.shop_dataset', orders) in feeds
    task = graph.nodes['task.dlt.warehouse.shop_dataset']
    assert (task.path, task.attrs['pipeline']) == ('pipelines/shop.py', 'shop')
    edge = next(e for e in graph.in_edges(orders, 'feeds') if e.source == task.id)
    assert edge.attrs['evidence'] == 'pipelines/shop.py:4'
    source = graph.nodes['system.postgres.app.public.orders']
    assert (source.kind, source.relation, source.attrs['outside_warehouse']) == ('table', None, True)
    # A file found by discovery adds no table the warehouse does not have.
    assert not [n for n in graph.nodes if 'refunds' in n]
    # Meltano: the Singer columns say a Singer target landed it; meltano.yml says which, and
    # its columns trace to the source system's.
    customers = graph.nodes['table.warehouse.raw.customers']
    assert customers.attrs['loaded_by'] == 'meltano'
    assert ('task.meltano.warehouse.raw', 'table.warehouse.raw.customers') in feeds
    assert [c.label for c in project.trace('accounts.email').columns] == [
        'customers.email',
        'postgres.app.public.customers.email',
    ]
    assert project.explain('accounts.email').complete
    assert graph.meta['inputs']['loader_source_tables'] == 2
    assert {'input': 'meltano', 'path': 'meltano.yml', 'detail': '1 landed table'} in graph.meta['sources']
    assert not graph.warnings
    # A file listed in [ingestion] paths lands a table the warehouse does not have yet.
    (tmp_path / 'konstruct.toml').write_text('[ingestion]\npaths = ["pipelines", "meltano.yml"]\n')
    graph = project.build(run_dbt=False).graph
    refunds = graph.nodes['table.shop_dataset.refunds']
    assert refunds.attrs['loaded_by'] == 'dlt' and refunds.attrs['inferred']
    assert ('task.dlt.shop', 'table.shop_dataset.refunds') in {(e.source, e.target) for e in graph.edges}
    assert graph.meta['inputs']['loader_source_tables'] == 3


def test_ingestion_paths_and_exclusions(tmp_path: Path) -> None:
    (tmp_path / 'etl').mkdir()
    (tmp_path / 'etl' / 'meltano.yml').write_text('plugins: {}\n')
    (tmp_path / 'scratch').mkdir()
    (tmp_path / 'scratch' / 'meltano.yml').write_text('plugins: {}\n')
    (tmp_path / 'big.json').write_text('{"tap_stream_id": 1, "streams": []' + ' ' * 2_000_001 + '}')
    (tmp_path / 'konstruct.toml').write_text('exclude = ["scratch/**"]\n')
    assert [
        (p.relative_to(tmp_path).as_posix(), k) for p, k in discover_ingestion_files(load_config(tmp_path))
    ] == [('etl/meltano.yml', 'meltano')]
    (tmp_path / 'konstruct.toml').write_text('[ingestion]\npaths = ["scratch"]\n')
    assert [
        p.relative_to(tmp_path).as_posix() for p, _ in discover_ingestion_files(load_config(tmp_path))
    ] == ['scratch/meltano.yml']


def test_status_lists_the_loader_configuration(tmp_path: Path) -> None:
    from konstruct.interfaces.status import inspect, render_config

    (tmp_path / 'meltano.yml').write_text('plugins: {}\n')
    status = inspect(tmp_path)
    assert status.ingestion_files == ['meltano.yml']
    assert ('ingestion', '1 loader configuration file: meltano.yml') in status.lines()
    assert '[ingestion]\npaths = ["meltano.yml"]' in render_config(load_config(tmp_path), status, [])


OCTAVIA_CONNECTION = """
definition_type: connection
resource_name: app_to_warehouse
source_configuration_path: sources/app/configuration.yaml
destination_configuration_path: destinations/warehouse/configuration.yaml
configuration:
  namespace_definition: customformat
  namespace_format: "${SOURCE_NAMESPACE}_raw"
  prefix: app_
  sync_catalog:
    streams:
    - stream: {name: orders, namespace: public, json_schema: {properties: {id: {}, total: {}}}}
      config: {selected: true}
    - stream: {name: users, namespace: public, json_schema: {properties: {id: {}, email: {}}}}
      config: {selected: true, field_selection_enabled: true, selected_fields: [{field_path: [id]}]}
    - stream: {name: audit, namespace: public}
      config: {selected: false}
"""


def test_an_octavia_project() -> None:
    files = [
        ('octavia/connections/app/configuration.yaml', OCTAVIA_CONNECTION),
        (
            'octavia/sources/app/configuration.yaml',
            'definition_type: source\ndefinition_image: airbyte/source-postgres\nconfiguration: {database: app}\n',
        ),
        (
            'octavia/destinations/warehouse/configuration.yaml',
            'definition_type: destination\ndefinition_image: airbyte/destination-snowflake\n'
            'configuration: {schema: RAW}\n',
        ),
        ('octavia/connections/broken/configuration.yaml', 'definition_type: connection\n  bad: ['),
    ]
    reading = read_files('airbyte_octavia', files)
    assert [(f.task, f.source, f.target, f.columns) for f in reading.feeds] == [
        (
            'app_to_warehouse',
            ('postgres', 'app', 'public', 'orders'),
            ('public_raw', 'app_orders'),
            (('id', 'id', 'direct'), ('total', 'total', 'direct')),
        ),
        (
            'app_to_warehouse',
            ('postgres', 'app', 'public', 'users'),
            ('public_raw', 'app_users'),
            (('id', 'id', 'direct'),),
        ),
    ]
    assert reading.problems[0].startswith('octavia/connections/broken/configuration.yaml: cannot read YAML')


def test_airbyte_api_exports() -> None:
    config_api = {
        'name': 'Stripe',
        'namespaceDefinition': 'destination',
        'prefix': '',
        'source': {'sourceName': 'Stripe', 'connectionConfiguration': {}},
        'destination': {'connectionConfiguration': {'dataset_id': 'stripe_raw'}},
        'syncCatalog': {
            'streams': [
                {
                    'stream': {'name': 'charges', 'jsonSchema': {'properties': {'id': {}, 'amount': {}}}},
                    'config': {'selected': True},
                },
                {
                    'stream': {'name': 'refunds'},
                    'config': {'selected': True, 'mappers': [{'type': 'hashing'}]},
                },
            ]
        },
    }
    public_api = {
        'data': [
            {
                'name': 'crm sync',
                'sourceId': 'b5b2',
                'destinationId': '5725',
                'namespaceDefinition': 'source',
                'configurations': {
                    'streams': [
                        {'name': 'contacts', 'namespace': 'crm', 'selectedFields': [{'fieldPath': ['email']}]}
                    ]
                },
            }
        ]
    }
    found = [
        (f.source, f.target, f.columns) for f in read('airbyte_api', 'c.json', json.dumps(config_api)).feeds
    ]
    assert found == [
        (
            ('stripe', 'charges'),
            ('stripe_raw', 'charges'),
            (('id', 'id', 'direct'), ('amount', 'amount', 'direct')),
        ),
        (('stripe', 'refunds'), ('stripe_raw', 'refunds'), ()),
    ]
    assert [
        (f.task, f.source, f.target) for f in read('airbyte_api', 'p.json', json.dumps(public_api)).feeds
    ] == [('crm sync', ('crm_sync', 'crm', 'contacts'), ('crm', 'contacts'))]


def airbyte_connection(definition: str, format_: str, schemas: list[str]) -> str:
    """A connection exported from Airbyte's configuration API, with one stream and no namespace."""
    return json.dumps(
        {
            'name': 'app',
            'namespaceDefinition': definition,
            'namespaceFormat': format_,
            'source': {
                'sourceName': 'Postgres',
                'connectionConfiguration': {'database': 'app', 'schemas': schemas},
            },
            'destination': {'connectionConfiguration': {'schema': 'raw'}},
            'syncCatalog': {'streams': [{'stream': {'name': 'orders'}, 'config': {'selected': True}}]},
        }
    )


@pytest.mark.parametrize(
    ('definition', 'format_', 'schemas', 'source', 'target'),
    [
        # The source's only schema is each stream's namespace.
        ('source', '', ['public'], ('postgres', 'app', 'public', 'orders'), ('public', 'orders')),
        (
            'customformat',
            '${SOURCE_NAMESPACE}_raw',
            ['public'],
            ('postgres', 'app', 'public', 'orders'),
            ('public_raw', 'orders'),
        ),
        ('destination', '', [], ('postgres', 'app', 'orders'), ('raw', 'orders')),
        ('customformat', 'airbyte_raw', [], ('postgres', 'app', 'orders'), ('airbyte_raw', 'orders')),
        # Without a namespace, Airbyte lands in the destination's default, and a blank custom
        # format does too; ${SOURCE_NAMESPACE} is then empty.
        ('source', '', ['public', 'sales'], ('postgres', 'app', 'orders'), ('raw', 'orders')),
        ('source', '', [], ('postgres', 'app', 'orders'), ('raw', 'orders')),
        ('customformat', ' ', [], ('postgres', 'app', 'orders'), ('raw', 'orders')),
        ('customformat', '${SOURCE_NAMESPACE}_raw', [], ('postgres', 'app', 'orders'), ('_raw', 'orders')),
    ],
)
def test_an_airbyte_stream_without_a_namespace(
    definition: str, format_: str, schemas: list[str], source: tuple[str, ...], target: tuple[str, ...]
) -> None:
    reading = read('airbyte_api', 'c.json', airbyte_connection(definition, format_, schemas))
    assert [(f.source, f.target) for f in reading.feeds] == [(source, target)]
    assert reading.problems == []


def test_an_octavia_setting_from_the_environment_is_not_a_name() -> None:
    files = [
        (
            'octavia/connections/app/configuration.yaml',
            OCTAVIA_CONNECTION.replace('customformat', 'destination'),
        ),
        (
            'octavia/sources/app/configuration.yaml',
            'definition_type: source\nconfiguration: {database: app}\n',
        ),
        (
            'octavia/destinations/warehouse/configuration.yaml',
            'definition_type: destination\nconfiguration: {schema: "${SNOWFLAKE_SCHEMA}"}\n',
        ),
    ]
    reading = read_files('airbyte_octavia', files)
    assert reading.feeds == []
    assert (
        reading.problems
        == [
            'octavia/connections/app/configuration.yaml: app_to_warehouse: ${SNOWFLAKE_SCHEMA} is not a literal name, '
            'so the feed is left out'
        ]
        * 2
    )


LOCK = {
    'plugin_type': 'loaders',
    'name': 'target-postgres',
    'variant': 'meltanolabs',
    'namespace': 'target_postgres',
    'settings': [
        {'name': 'database', 'kind': 'string'},
        {'name': 'default_target_schema', 'value': '$MELTANO_EXTRACT__LOAD_SCHEMA'},
    ],
}


def test_a_meltano_lock_file_gives_the_loaders_default_schema(tmp_path: Path) -> None:
    """``meltano lock`` writes each Hub plugin's definition beside meltano.yml, defaults included."""
    text = (
        'plugins:\n  extractors: [{name: tap-mysql, select: [shop-orders.*]}]\n'
        '  loaders: [{name: target-postgres, variant: meltanolabs}]\n'
    )
    files = [('meltano.yml', text), ('plugins/loaders/target-postgres--meltanolabs.lock', json.dumps(LOCK))]
    assert [f.target for f in read_files('meltano', files).feeds] == [('tap_mysql', 'orders')]
    # The build reads the lock files beside meltano.yml.
    (tmp_path / 'plugins' / 'loaders').mkdir(parents=True)
    (tmp_path / 'plugins' / 'loaders' / 'target-postgres--meltanolabs.lock').write_text(json.dumps(LOCK))
    (tmp_path / 'meltano.yml').write_text(text)
    (tmp_path / 'konstruct.toml').write_text('[ingestion]\npaths = ["meltano.yml"]\n')
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert graph.nodes['table.tap_mysql.orders'].attrs['loaded_by'] == 'meltano'
    assert [s['path'] for s in graph.meta['sources'] if s.get('path')] == ['meltano.yml']


def test_a_lock_file_that_is_not_configuration_is_not_read(tmp_path: Path) -> None:
    """A named pipe would block the build for ever, and a lock file past 2 MB is left out, as
    discovery leaves out any such file."""
    import os

    locks = tmp_path / 'plugins' / 'loaders'
    locks.mkdir(parents=True)
    (locks / 'target-postgres--meltanolabs.lock').write_text(json.dumps(LOCK) + ' ' * 2_000_000)
    if hasattr(os, 'mkfifo'):
        os.mkfifo(locks / 'target-duckdb--meltanolabs.lock')
    (tmp_path / 'meltano.yml').write_text(
        'plugins:\n  extractors: [{name: tap-mysql, select: [shop-orders.*]}]\n'
        '  loaders: [{name: target-postgres, variant: meltanolabs}]\n'
    )
    (tmp_path / 'konstruct.toml').write_text('[ingestion]\npaths = ["meltano.yml"]\n')
    graph = Project(tmp_path).build(run_dbt=False).graph
    # Without the lock file's default schema, the table is found by its name.
    assert graph.nodes['table.orders'].attrs['loaded_by'] == 'meltano'
    assert 'table.tap_mysql.orders' not in graph.nodes


TERRAFORM = """
resource "airbyte_source_postgres" "app" {
  name = "app"
  configuration = {
    database = "app"
    host     = var.host
  }
}

resource "airbyte_destination_snowflake" "warehouse" {
  configuration = { database = "ANALYTICS", schema = "RAW" }
}

resource "airbyte_connection" "app" {
  name           = "app to warehouse"
  source_id      = airbyte_source_postgres.app.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "custom_format"
  namespace_format     = "$${SOURCE_NAMESPACE}"
  configurations = {
    streams = [
      { name = "orders", namespace = "public", selected_fields = [{ field_path = ["id"] }, { field_path = ["total"] }] },
      { name = "users", namespace = "public" },
    ]
  }
}

resource "fivetran_connector" "pg" {
  group_id = fivetran_group.warehouse.id
  service  = "postgres"
  destination_schema {
    prefix = "pg"
  }
  config {
    database = "shop"
  }
}

resource "fivetran_connection" "sf" {
  service = "salesforce"
  destination_schema = { name = "salesforce" }
  config = jsonencode({ is_sandbox = false })
}

resource "fivetran_connector_schema_config" "pg" {
  connector_id           = fivetran_connector.pg.id
  schema_change_handling = "BLOCK_ALL"
  schemas = {
    "public" = {
      tables = {
        "customers" = {
          columns = {
            "email" = { hashed = true }
            "id"    = {}
            "notes" = { enabled = false }
          }
        }
        "gone" = { enabled = false }
      }
    }
    "archive" = { enabled = false }
  }
}

resource "fivetran_connector_schema_config" "sf" {
  connection_id = fivetran_connection.sf.id
  schema {
    name = "salesforce"
    table {
      name = "account"
      column {
        name = "name"
      }
    }
  }
}

resource "fivetran_connector_schema_config" "elsewhere" {
  connector_id = var.connector_id
}
"""


def test_terraform_airbyte_and_fivetran_resources() -> None:
    reading = read_files('terraform', [('infra/main.tf', TERRAFORM)])
    assert [(f.loader, f.task, f.source, f.target, f.columns) for f in reading.feeds] == [
        (
            'airbyte',
            'app to warehouse',
            ('postgres', 'app', 'public', 'orders'),
            ('public', 'orders'),
            (('id', 'id', 'direct'), ('total', 'total', 'direct')),
        ),
        ('airbyte', 'app to warehouse', ('postgres', 'app', 'public', 'users'), ('public', 'users'), ()),
        (
            'fivetran',
            'pg',
            ('postgres', 'shop', 'public', 'customers'),
            ('pg_public', 'customers'),
            (('email', 'email', 'transformed'), ('id', 'id', 'direct')),
        ),
        (
            'fivetran',
            'salesforce',
            ('salesforce', 'account'),
            ('salesforce', 'account'),
            (('name', 'name', 'direct'),),
        ),
    ]
    assert reading.problems == [
        'infra/main.tf:78: a schema configuration whose connection is not in this module'
    ]
    bad = read_files('terraform', [('bad.tf', 'resource "fivetran_connector" "x" {\n  service = "a\n}\n')])
    assert bad.problems == ['bad.tf:2: cannot read Terraform: line 2: unterminated string']


def test_the_hcl_reader_keeps_what_terraform_evaluates_as_text() -> None:
    text = """
# a comment
locals {
  /* a block
     comment */
  names = [for s in var.list : upper(s)]
  pick  = var.on ? "a" : "b"
  heredoc = <<-EOT
    hello
  EOT
  map   = { for k, v in var.m : k => v }
  sum   = 1 + 2
  neg   = -1
  float = 1.5
  call  = lookup(var.m, "k", null)
  flags = [true, false, null]
  "quoted key" = "with \\"escape\\" and ${var.x}"
}
"""
    (block,) = hcl.parse(text)
    attrs = block.arguments
    assert attrs['names'] == hcl.Ref('[for s in var.list : upper(s)]')
    assert attrs['pick'] == hcl.Ref('var.on ? "a" : "b"')
    assert attrs['heredoc'] == '    hello\n'
    assert attrs['map'] == hcl.Ref('{ for k, v in var.m : k => v }')
    assert attrs['sum'] == hcl.Ref('1 + 2')
    assert (attrs['neg'], attrs['float'], attrs['flags']) == (-1, 1.5, [True, False, None])
    assert attrs['call'] == hcl.Ref('lookup(var.m, "k", null)')
    assert attrs['quoted key'] == 'with "escape" and ${var.x}'
    for broken in ('x = [1,', 'x {', 'x = {a 1}', '= 1', 'x = <<EOT\nnever closed', 'x = @'):
        with pytest.raises(hcl.HclError):
            hcl.parse(broken)


NESTED_HCL = 'resource "airbyte_connection" "a" {\n  x = ' + '[' * 5_000 + ']' * 5_000 + '\n}\n'


@pytest.mark.parametrize(
    ('kind', 'path', 'text'),
    [
        ('terraform', 'main.tf', 'resource "fivetran_connector" "a" {\n  x = b.'),
        ('terraform', 'main.tf', NESTED_HCL),
        (
            'terraform',
            'main.tf',
            'resource "fivetran_connector" "pg" {\n  service = "postgres"\n}\n'
            'resource "fivetran_connector_schema_config" "pg" {\n'
            '  connector_id = fivetran_connector.pg.id\n  schemas = { public = { tables = 3 } }\n}\n',
        ),
        ('airbyte_octavia', 'connections/a/configuration.yaml', NESTED_YAML),
        (
            'airbyte_octavia',
            'connections/a/configuration.yaml',
            'definition_type: connection\nconfiguration: 3',
        ),
        ('airbyte_api', 'c.json', NESTED_JSON.replace('streams', 'syncCatalog')),
        ('airbyte_api', 'c.json', '{"syncCatalog": 3}'),
    ],
)
def test_a_malformed_airbyte_or_terraform_file_is_a_problem(kind: str, path: str, text: str) -> None:
    reading = read_files(kind, [(path, text)])
    assert reading.feeds == []
    assert len(reading.problems) == 1 and reading.problems[0].startswith(f'{path}:')


def test_a_malformed_file_in_a_module_leaves_the_others() -> None:
    files = [('tf/main.tf', TERRAFORM), ('tf/broken.tf', NESTED_HCL)]
    reading, alone = read_files('terraform', files), read_files('terraform', files[:1])
    assert reading.feeds == alone.feeds != []
    assert reading.problems == ['tf/broken.tf: cannot read: it is nested too deeply', *alone.problems]


AIRBYTE_TF = """
resource "airbyte_source_postgres" "app" {
  configuration = { database = "app" }
}
resource "airbyte_connection" "app" {
  name = "app"
  source_id = airbyte_source_postgres.app.source_id
  SETTING
  configurations = { streams = [{ name = "orders", namespace = "public" }] }
}
"""


@pytest.mark.parametrize(
    ('setting', 'problem'),
    [
        ('namespace_definition = var.namespace', 'connection app: its namespace definition var.namespace'),
        ('prefix = var.prefix', 'connection app: its namespace prefix var.prefix'),
        (
            'namespace_definition = "custom_format"\n  namespace_format = "${var.schema}"',
            'connection app: its namespace format ${var.schema}',
        ),
    ],
)
def test_a_terraform_value_that_terraform_evaluates_is_not_a_name(setting: str, problem: str) -> None:
    reading = read_files('terraform', [('main.tf', AIRBYTE_TF.replace('SETTING', setting))])
    assert reading.feeds == []
    assert reading.problems == [f'main.tf:5: {problem} is not a literal, so its tables are not known']


def test_a_fivetran_prefix_from_a_variable_is_not_a_name() -> None:
    text = (
        'resource "fivetran_connector" "pg" {\n  service = "postgres"\n'
        '  destination_schema { prefix = var.prefix }\n}\n'
        'resource "fivetran_connector_schema_config" "pg" {\n  connector_id = fivetran_connector.pg.id\n'
        '  schemas = { public = { tables = { orders = {} } } }\n}\n'
    )
    reading = read_files('terraform', [('main.tf', text)])
    assert reading.feeds == []
    assert reading.problems == ['main.tf:5: pg: var.prefix is not a literal name, so the feed is left out']


def test_a_terraform_module_lands_fivetran_tables_with_their_source(tmp_path: Path) -> None:
    duckdb_warehouse(
        tmp_path,
        'create schema pg_public;'
        'create table pg_public.customers (id bigint, email varchar, _fivetran_synced timestamp);',
    )
    (tmp_path / 'infra').mkdir()
    (tmp_path / 'infra' / 'connectors.tf').write_text(
        TERRAFORM.split('resource "fivetran_connector_schema_config"')[0]
    )
    (tmp_path / 'infra' / 'schemas.tf').write_text(
        'resource "fivetran_connector_schema_config"'
        + TERRAFORM.split('resource "fivetran_connector_schema_config"', 1)[1]
    )
    (tmp_path / 'infra' / 'variables.tf').write_text('variable "host" {}\n')
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    sync = 'task.fivetran.warehouse.pg_public'
    assert ('system.postgres.shop.public.customers', sync) in feeds
    assert (sync, 'table.warehouse.pg_public.customers') in feeds
    assert graph.nodes[sync].path == 'infra/schemas.tf'
    email = graph.column('table.warehouse.pg_public.customers', 'email')
    assert email is not None
    (edge,) = graph.out_edges(email.id, 'derived_from')
    assert (edge.target, edge.attrs['transform']) == (
        'system.postgres.shop.public.customers#email',
        'transformed',
    )
    assert {'input': 'fivetran', 'path': 'infra/schemas.tf', 'detail': '2 landed tables'} in graph.meta[
        'sources'
    ]
    assert not [s for s in graph.meta['sources'] if s.get('path') == 'infra/variables.tf']


DEBEZIUM = {
    'name': 'app-cdc',
    'config': {
        'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
        'database.dbname': 'app',
        'topic.prefix': 'app',
        'table.include.list': 'public.orders,public\\.customers',
        'column.include.list': 'public.customers.id,public.customers.email',
        'transforms': 'unwrap',
        'transforms.unwrap.type': 'io.debezium.transforms.ExtractNewRecordState',
    },
}
JDBC_SINK = {
    'name': 'warehouse-sink',
    'config': {
        'connector.class': 'io.debezium.connector.jdbc.JdbcSinkConnector',
        'connection.url': 'jdbc:postgresql://warehouse:5432/analytics',
        'topics': 'app.public.orders,app.public.customers',
        'transforms': 'route',
        'transforms.route.type': 'org.apache.kafka.connect.transforms.RegexRouter',
        'transforms.route.regex': 'app\\.public\\.(.*)',
        'transforms.route.replacement': '$1',
        'collection.name.format': 'raw.${topic}',
    },
}


def test_debezium_connectors_capture_their_tables_into_topics() -> None:
    reading = read('kafka_connect', 'connect/app.json', json.dumps(DEBEZIUM))
    assert [(f.loader, f.task, f.source, f.target, f.stream, f.columns) for f in reading.feeds] == [
        (
            'debezium',
            'app-cdc',
            ('postgres', 'app', 'public', 'orders'),
            ('kafka', 'app.public.orders'),
            True,
            (),
        ),
        (
            'debezium',
            'app-cdc',
            ('postgres', 'app', 'public', 'customers'),
            ('kafka', 'app.public.customers'),
            True,
            (('id', 'id', 'direct'), ('email', 'email', 'direct')),
        ),
    ]
    mysql = {
        'connector.class': 'io.debezium.connector.mysql.MySqlConnector',
        'database.server.name': 'shop',
        'table.include.list': 'inventory.products',
        'transforms': 'reroute',
        'transforms.reroute.type': 'io.debezium.transforms.ByLogicalTableRouter',
        'transforms.reroute.topic.regex': 'shop\\.inventory\\.(.*)',
        'transforms.reroute.topic.replacement': 'cdc_$1',
    }
    assert feeds_of('kafka_connect', json.dumps(mysql), 'mysql.json') == [
        ('debezium', 'mysql', ('mysql', 'inventory', 'products'), ('kafka', 'cdc_products'))
    ]
    sqlserver = {
        'connector.class': 'io.debezium.connector.sqlserver.SqlServerConnector',
        'database.names': 'erp',
        'topic.prefix': 'erp',
        'table.include.list': 'dbo.invoices',
    }
    assert feeds_of('kafka_connect', json.dumps(sqlserver), 'erp.json') == [
        ('debezium', 'erp', ('sqlserver', 'erp', 'dbo', 'invoices'), ('kafka', 'erp.erp.dbo.invoices'))
    ]


def test_connectors_whose_tables_are_not_spelled_out() -> None:
    patterns = {
        'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
        'topic.prefix': 'app',
        'table.include.list': 'public\\.order.*',
    }
    unprefixed = {
        'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
        'table.include.list': 'a.b',
    }
    one_part = {
        'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
        'topic.prefix': 'p',
        'table.include.list': 'orders',
    }
    regex_sink = {
        'connector.class': 'io.debezium.connector.jdbc.JdbcSinkConnector',
        'topics.regex': 'app\\..*',
    }
    data = [
        {'name': 'patterns', 'config': patterns},
        {'name': 'unprefixed', 'config': unprefixed},
        {'name': 'one', 'config': one_part},
        {'name': 'regex', 'config': regex_sink},
        {
            'name': 'files',
            'config': {'connector.class': 'io.confluent.connect.s3.S3SinkConnector', 'topics': 'a'},
        },
        'not a connector',
    ]
    reading = read('kafka_connect', 'c.json', json.dumps(data))
    assert reading.feeds == []
    assert reading.problems == [
        'c.json: Debezium connector patterns: its tables are not listed by name, so its topics are not known',
        'c.json: Debezium connector unprefixed: no topic prefix, so its topics are not known',
        'c.json: Debezium connector one: orders is not <schema>.<table>',
        'c.json: sink regex reads topics by a pattern, so its tables are not known',
    ]
    assert read('kafka_connect', 'x.json', '{').problems[0].startswith('x.json: cannot read JSON')


def test_sink_connectors_land_topics_in_tables() -> None:
    assert feeds_of('kafka_connect', json.dumps(JDBC_SINK), 'sink.json') == [
        ('kafka_connect', 'warehouse-sink', ('kafka', 'app.public.orders'), ('analytics', 'raw', 'orders')),
        (
            'kafka_connect',
            'warehouse-sink',
            ('kafka', 'app.public.customers'),
            ('analytics', 'raw', 'customers'),
        ),
    ]
    debezium_default = {
        'connector.class': 'io.debezium.connector.jdbc.JdbcSinkConnector',
        'topics': 'app.public.orders',
    }
    confluent = {
        'connector.class': 'io.confluent.connect.jdbc.JdbcSinkConnector',
        'topics': 'orders',
        'table.name.format': 'kafka_${topic}',
        'connection.url': 'jdbc:sqlserver://h:1433;databaseName=dw',
    }
    snowflake = {
        'connector.class': 'com.snowflake.kafka.connector.SnowflakeSinkConnector',
        'topics': 'orders,app.public.users,web-events',
        'snowflake.topic2table.map': 'app.public.users:USERS',
        'snowflake.database.name': 'RAW',
        'snowflake.schema.name': 'KAFKA',
    }
    bigquery = {
        'connector.class': 'com.wepay.kafka.connect.bigquery.BigQuerySinkConnector',
        'topics': 'app.orders',
        'sanitizeTopics': 'true',
        'project': 'proj',
        'defaultDataset': 'kafka',
    }
    iceberg = {
        'connector.class': 'org.apache.iceberg.connect.IcebergSinkConnector',
        'topics': 'events',
        'iceberg.tables': 'lake.events,lake.events_copy',
    }
    dynamic = {
        'connector.class': 'io.tabular.iceberg.connect.IcebergSinkConnector',
        'topics': 'events',
        'iceberg.tables.dynamic-enabled': 'true',
    }
    connectors = [debezium_default, confluent, snowflake, bigquery, iceberg, dynamic]
    reading = read(
        'kafka_connect',
        'sinks.json',
        json.dumps([{'name': f's{i}', 'config': c} for i, c in enumerate(connectors)]),
    )
    assert [(f.task, f.source[1], f.target) for f in reading.feeds] == [
        ('s0', 'app.public.orders', ('app_public_orders',)),
        ('s1', 'orders', ('dw', 'kafka_orders')),
        ('s2', 'orders', ('RAW', 'KAFKA', 'ORDERS')),
        ('s2', 'app.public.users', ('RAW', 'KAFKA', 'USERS')),
        ('s3', 'app.orders', ('proj', 'kafka', 'app_orders')),
        ('s4', 'events', ('lake', 'events_copy')),
        ('s4', 'events', ('lake', 'events')),
    ]
    assert reading.problems == [
        'sinks.json: sink s2: topic web-events is not a table name and has no snowflake.topic2table.map entry',
        'sinks.json: sink s5 routes records to tables at run time, so its tables are not known',
    ]


PASSTHROUGH = {'collection.naming.strategy': 'io.debezium.sink.naming.PassthroughCollectionNamingStrategy'}


@pytest.mark.parametrize(
    ('config', 'target'),
    [
        ({'collection.name.format': 'raw.${source.schema}_${source.table}'}, ('raw', 'public_orders')),
        ({'collection.name.format': 'raw.${source.table}'}, ('raw', 'orders')),
        ({**PASSTHROUGH, 'collection.name.format': 'kafka_${topic}'}, ('kafka_app', 'public', 'orders')),
    ],
)
def test_a_debezium_jdbc_sink_names_tables_from_the_source_block(
    config: dict[str, str], target: tuple[str, ...]
) -> None:
    sink = {'connector.class': 'io.debezium.connector.jdbc.JdbcSinkConnector', 'topics': 'app.public.orders'}
    reading = read('kafka_connect', 'sink.json', json.dumps({'name': 's', 'config': {**sink, **config}}))
    assert [f.target for f in reading.feeds] == [target]
    assert reading.problems == []


@pytest.mark.parametrize(
    ('topics', 'config', 'placeholder'),
    [
        ('app.public.orders', {'collection.name.format': '${source.db}.${source.table}'}, '${source.db}'),
        # A topic that is not <prefix>.<schema>.<table>, and a strategy that fills no source field.
        ('orders', {'collection.name.format': 'raw.${source.table}'}, '${source.table}'),
        (
            'app.public.orders',
            {**PASSTHROUGH, 'collection.name.format': 'raw.${source.table}'},
            '${source.table}',
        ),
    ],
)
def test_a_debezium_jdbc_sink_placeholder_that_is_not_known(
    topics: str, config: dict[str, str], placeholder: str
) -> None:
    sink = {'connector.class': 'io.debezium.connector.jdbc.JdbcSinkConnector', 'topics': topics}
    reading = read('kafka_connect', 'sink.json', json.dumps({'name': 's', 'config': {**sink, **config}}))
    assert reading.feeds == []
    assert reading.problems == [
        f'sink.json: sink s: {placeholder} in collection.name.format is known only from the events '
        f'of topic {topics}, so its table is not known'
    ]


def test_properties_discovery_is_linear_in_blank_lines() -> None:
    import time

    start = time.process_time()
    assert kind_of('worker.properties', ' \n' * 20_000) is None
    assert time.process_time() - start < 0.5


def test_a_standalone_worker_properties_file() -> None:
    text = '# a sink\nname=props-sink\nconnector.class=io.confluent.connect.jdbc.JdbcSinkConnector\ntopics=orders\n! note\n'
    assert feeds_of('kafka_connect', text, 'connect/sink.properties') == [
        ('kafka_connect', 'props-sink', ('kafka', 'orders'), ('orders',))
    ]


@pytest.mark.parametrize(
    ('kind', 'path', 'text'),
    [
        ('kafka_connect', 'c.json', '{"config": ' + '[' * 100_000 + ']' * 100_000 + '}'),
        (
            'kafka_connect',
            'c.json',
            '{"name": "x", "config": {"connector.class": "io.debezium.connector.postgresql.PostgresConnector",'
            ' "topic.prefix": "app", "table.include.list": 3}}',
        ),
        ('estuary', 'flow.yaml', NESTED_YAML),
        ('estuary', 'flow.yaml', '42'),
        ('estuary', 'flow.yaml', 'captures: {a: {bindings: 3}}'),
        (
            'estuary',
            'flow.yaml',
            f'{ALIASES}captures: {{a: {{bindings: [{{resource: {{stream: *i}}, target: *i}}]}}}}',
        ),
    ],
)
def test_a_malformed_connector_or_catalog_is_a_problem(kind: str, path: str, text: str) -> None:
    reading = read(kind, path, text)
    assert reading.feeds == []
    assert len(reading.problems) == 1 and reading.problems[0].startswith(f'{path}: ')


@pytest.mark.parametrize(
    ('config', 'problem'),
    [
        (
            {
                'connector.class': 'io.confluent.connect.jdbc.JdbcSinkConnector',
                'topics': 'orders',
                'connection.url': '${file:/secrets/db.properties:url}',
            },
            '${file:/secrets/db.properties:url}',
        ),
        (
            {
                'connector.class': 'com.snowflake.kafka.connector.SnowflakeSinkConnector',
                'topics': 'orders',
                'snowflake.database.name': '${env:SNOWFLAKE_DATABASE}',
                'snowflake.schema.name': 'KAFKA',
            },
            '${env:SNOWFLAKE_DATABASE}',
        ),
        (
            {
                'connector.class': 'com.wepay.kafka.connect.bigquery.BigQuerySinkConnector',
                'topics': 'orders',
                'project': '${env:GCP_PROJECT}',
                'defaultDataset': 'kafka',
            },
            '${env:GCP_PROJECT}',
        ),
        (
            {
                'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
                'topic.prefix': 'app',
                'database.dbname': '${file:/secrets/pg.properties:dbname}',
                'table.include.list': 'public.orders',
            },
            '${file:/secrets/pg.properties:dbname}',
        ),
    ],
)
def test_a_connector_setting_from_a_config_provider_is_not_a_name(
    config: dict[str, str], problem: str
) -> None:
    """Kafka Connect fills ``${file:...}`` and ``${env:...}`` from its config providers when the
    connector starts; the file does not say what they are."""
    reading = read('kafka_connect', 'c.json', json.dumps({'name': 'c', 'config': config}))
    assert reading.feeds == []
    assert reading.problems == [f'c.json: c: {problem} is not a literal name, so the feed is left out']


def sink_routed(topics: str, regex: str, replacement: str = 'x', transforms: int = 1) -> str:
    """A JDBC sink with ``transforms`` RegexRouter transformations, each the same."""
    config = {
        'connector.class': 'io.confluent.connect.jdbc.JdbcSinkConnector',
        'topics': topics,
        'transforms': ','.join(f'r{i}' for i in range(transforms)),
    }
    for i in range(transforms):
        config[f'transforms.r{i}.type'] = 'org.apache.kafka.connect.transforms.RegexRouter'
        config[f'transforms.r{i}.regex'] = regex
        config[f'transforms.r{i}.replacement'] = replacement
    return json.dumps({'name': 'c', 'config': config})


@pytest.mark.parametrize(
    'regex',
    [
        '(a+)+$',
        '(a|aa)+$',
        '.*.*.*.*b',
        '(?x)(a+) +b',
    ],
)
def test_a_routing_expression_runs_in_linear_time(regex: str) -> None:
    """Each of these backtracks for years in a matcher like Python's on topics it does not
    match; the routers run on finite automata."""
    import time

    topics = ','.join(f'{"a" * 200}{i}' for i in range(1_500))
    start = time.process_time()
    reading = read('kafka_connect', 'c.json', sink_routed(topics, regex))
    assert time.process_time() - start < 2
    assert len(reading.feeds) == 1_500 and reading.problems == []


@pytest.mark.parametrize(
    ('regex', 'replacement', 'reason'),
    [
        (r'(a)\1', 'x', 'it uses a backreference, which Konstruct does not run'),
        ('a(?=b)', 'x', 'it uses lookaround, which Konstruct does not run'),
        ('a*+b', 'x', 'it uses a possessive repeat, which Konstruct does not run'),
        ('(a', 'x', 'its expression is not valid: unclosed group'),
        ('(a)', '$2', 'its replacement names group 2, which the expression lacks'),
        ('(a)', '$1' * 125, 'its replacement is longer than a topic can be'),
        # Bounded repeats compile to a state for each repetition: these pass 64 KiB.
        ('([a-z]{1,40}){1,40}b', 'x', 'its expression is too large'),
        ('a{0,249}' * 8 + 'b', 'x', 'its expression is too large'),
        # Classes are ASCII, as in Java, so a Unicode class is not one.
        (r'\pL+', 'x', 'its expression is not valid: Unicode not allowed here'),
    ],
)
def test_a_routing_expression_that_cannot_be_run_is_a_problem(
    regex: str, replacement: str, reason: str
) -> None:
    reading = read('kafka_connect', 'c.json', sink_routed('orders', regex, replacement))
    assert reading.feeds == []
    assert reading.problems == [
        f'c.json: sink c: transformation r0 is not run, as {reason}, so its topics are not known'
    ]


def test_routing_replaces_with_javas_group_references() -> None:
    reading = read(
        'kafka_connect',
        'c.json',
        sink_routed('app.public.orders', r'(\w+)\.(?<schema>\w+)\.(\w+)', '${schema}_$3'),
    )
    assert [f.target for f in reading.feeds] == [('public_orders',)]


def test_routing_expressions_are_compiled_once_and_at_most_64_a_file() -> None:
    """Compiling an expression takes time of its own: a file of a thousand connectors reuses each
    one, and a file of more than 64 distinct expressions is a problem."""
    import time

    def sinks(patterns: list[str]) -> str:
        return json.dumps(
            [
                json.loads(sink_routed('orders', p, 'y'))['config'] | {'name': f's{i}'}
                for i, p in enumerate(patterns)
            ]
        )

    start = time.process_time()
    same = read('kafka_connect', 'c.json', sinks([r'[a-z]{0,40}\w{0,40}x'] * 1_000))
    distinct = read('kafka_connect', 'c.json', sinks([f'[a-z]{{0,40}}\\w{{0,40}}x{i}' for i in range(1_000)]))
    assert time.process_time() - start < 2
    assert len(same.feeds) == 1_000 and same.problems == []
    assert len(distinct.feeds) == 64
    assert distinct.problems[0] == (
        'c.json: sink s64: the file holds more than 64 routing expressions, so its topics are not known'
    )


def test_a_topic_longer_than_kafka_allows_is_not_routed_at_all() -> None:
    """A Debezium topic prefix is free text: routing a long one through a replacement that repeats
    its group would build topics of hundreds of megabytes."""
    config = {
        'connector.class': 'io.debezium.connector.postgresql.PostgresConnector',
        'table.include.list': 'public.orders',
        'topic.prefix': 'p' * 1_500_000,
        'transforms': 'r',
        'transforms.r.type': 'org.apache.kafka.connect.transforms.RegexRouter',
        'transforms.r.regex': '(.*)',
        'transforms.r.replacement': '$1' * 124,
    }
    reading = read('kafka_connect', 'c.json', json.dumps({'name': 'c', 'config': config}))
    assert reading.feeds == []
    assert reading.problems == [
        'c.json: Debezium connector c: a topic of 1,500,014 characters is longer than Kafka allows, '
        'so its topics are not known'
    ]


def test_a_routed_topic_longer_than_kafka_allows_is_a_problem() -> None:
    """A replacement can repeat a group: 124 copies of a 200-character topic."""
    reading = read('kafka_connect', 'c.json', sink_routed('a' * 200, '(.*)', '$1' * 124))
    assert reading.feeds == []
    assert reading.problems == [
        'c.json: sink c: transformation r0 gives a topic longer than Kafka allows, so its topics are not known'
    ]


def test_a_connector_with_too_many_transformations_is_a_problem() -> None:
    reading = read('kafka_connect', 'c.json', sink_routed('orders', 'x(.*)', '$1', transforms=33))
    assert reading.feeds == []
    assert reading.problems == [
        'c.json: sink c: it lists more than 32 transformations, so its topics are not known'
    ]


FLOW = """
captures:
  acme/source-postgres:
    endpoint:
      connector:
        image: ghcr.io/estuary/source-postgres:v1
        config: {address: db:5432, database: app}
    bindings:
      - resource: {namespace: public, stream: orders}
        target: acme/public/orders
      - resource: {namespace: public, stream: audit}
        target: acme/public/audit
        disable: true
  acme/source-hubspot:
    endpoint:
      connector: {image: ghcr.io/estuary/source-hubspot:v1, config: hubspot.config.yaml}
    bindings:
      - resource: {stream: contacts}
        target: acme/hubspot/contacts
materializations:
  acme/warehouse:
    endpoint:
      connector:
        image: ghcr.io/estuary/materialize-snowflake:v1
        config: {database: ANALYTICS, schema: ESTUARY}
    bindings:
      - resource: {table: orders}
        source: acme/public/orders
      - resource: {table: contacts, schema: CRM}
        source: {name: acme/hubspot/contacts}
"""


def test_an_estuary_catalog_through_its_collections() -> None:
    assert [(f.task, f.source, f.target, f.stream) for f in read('estuary', 'flow.yaml', FLOW).feeds] == [
        (
            'acme/source-postgres',
            ('postgres', 'app', 'public', 'orders'),
            ('estuary', 'acme/public/orders'),
            True,
        ),
        ('acme/source-hubspot', ('hubspot', 'contacts'), ('estuary', 'acme/hubspot/contacts'), True),
        ('acme/warehouse', ('estuary', 'acme/public/orders'), ('ANALYTICS', 'ESTUARY', 'orders'), False),
        ('acme/warehouse', ('estuary', 'acme/hubspot/contacts'), ('ANALYTICS', 'CRM', 'contacts'), False),
    ]
    assert read('estuary', 'flow.yaml', 'captures: [').problems[0].startswith('flow.yaml: cannot read YAML')


def test_a_debezium_connector_and_its_sink_make_one_chain(tmp_path: Path) -> None:
    duckdb_warehouse(
        tmp_path,
        'create schema raw;'
        'create table raw.customers (id bigint, email varchar, __op varchar, __source_ts_ms bigint);'
        'create table raw.orders (id bigint, total double, __op varchar, __source_ts_ms bigint);',
    )
    (tmp_path / 'connect').mkdir()
    (tmp_path / 'connect' / 'source.json').write_text(json.dumps(DEBEZIUM))
    (tmp_path / 'connect' / 'sink.json').write_text(json.dumps(JDBC_SINK).replace('analytics', 'warehouse'))
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    topic = 'system.kafka.app.public.customers'
    assert ('system.postgres.app.public.customers', 'task.debezium.app-cdc') in feeds
    assert ('task.debezium.app-cdc', topic) in feeds
    assert (topic, 'task.kafka_connect.warehouse.raw') in feeds
    assert ('task.kafka_connect.warehouse.raw', 'table.warehouse.raw.customers') in feeds
    # The sink's configuration says Kafka Connect landed the Debezium change events.
    assert graph.nodes['table.warehouse.raw.customers'].attrs['loaded_by'] == 'kafka_connect'
    upstream = {s.node.id for s in graph.trace(graph.nodes['table.warehouse.raw.customers'])}
    assert 'system.postgres.app.public.customers' in upstream


def test_discovered_streams_add_no_tables(tmp_path: Path) -> None:
    """A connector and a Flow catalog that discovery found, with no warehouse: their topics,
    collections and source tables lead to no table the graph has, so none is added."""
    (tmp_path / 'connect').mkdir()
    (tmp_path / 'connect' / 'debezium.json').write_text(json.dumps(DEBEZIUM))
    (tmp_path / 'flows').mkdir()
    (tmp_path / 'flows' / 'flow.yaml').write_text(FLOW)
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert [n for n in graph.nodes if n.startswith(('system.', 'task.'))] == []
