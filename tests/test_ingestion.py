"""Loader configuration files: the source tables each loader reads and the tables it lands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from konstruct import Project
from konstruct.engine.config import discover_ingestion_files, load_config
from konstruct.inputs.ingestion import Feed, kind_of, read

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
