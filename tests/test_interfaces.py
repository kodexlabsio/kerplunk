"""The interfaces users build on, recorded in tests/golden/interfaces/ so that none breaks silently.

Each file holds one kind of interface as a flat map from an entry to what it is:

- ``cli.json``: every command, option and positional argument (by position), and what each
  option takes (default, repetition, values, choices), from the argparse parser;
- ``exit.json``: the exit status of each kind of outcome;
- ``json.json``: the shape of each ``--json`` document and of ``graph.json`` (every field's path
  and types, inferred from the example and small projects made to fill every list), and each
  document's ``format_version``;
- ``mcp.json``: the MCP tools and their input schemas;
- ``python.json``: the names ``konstruct`` exports, their methods, signatures (with each
  parameter's position) and fields;
- ``config.json``: the ``konstruct.toml`` keys Konstruct reads;
- ``action.json`` and ``csv.json``: the GitHub Action's inputs and outputs, and the CSV export's
  columns.

An entry that disappears or changes fails the test: restore it, or make it a deliberate breaking
change (regenerate with ``KONSTRUCT_UPDATE_GOLDEN=1`` and commit with both ``!`` after the type
and a ``BREAKING CHANGE:`` footer; the commit check refuses it otherwise). A new entry fails too
until the snapshot is regenerated, like the golden files. A JSON field breaks when it is removed,
emptied to only ``null`` or given a type it did not hold; ``null`` aside, so widening ``str`` to
``null|str`` passes, and so does a field showing fewer of its types. An entry that accepts more
than it did is not a break either: a required argument made optional, or one that takes more
types. A list the projects leave empty and a field they fill only with ``null`` fail as missing
coverage, unless ``EMPTY`` or ``UNFILLED`` gives the reason; adding one there to hide a change
still needs the breaking marks, as the commit check counts the fields gone with it as removed.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest
import yaml
from mcp import Client

import konstruct
from konstruct import Project, _core
from konstruct.interfaces import cli
from konstruct.interfaces.cli import main, parser
from konstruct.interfaces.mcp_server import build_server

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / 'tests' / 'golden' / 'interfaces'
UPDATE = bool(os.environ.get('KONSTRUCT_UPDATE_GOLDEN'))


def _commit_check() -> ModuleType:
    path = ROOT / '.kodex' / 'scripts' / 'check_commit_message.py'
    spec = importlib.util.spec_from_file_location('check_commit_message', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check(name: str, current: dict[str, Any]) -> None:
    """Compare ``current`` with the snapshot ``name``, or write it with KONSTRUCT_UPDATE_GOLDEN."""
    path = SNAPSHOTS / f'{name}.json'
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    assert path.is_file(), f'no snapshot {path}; run with KONSTRUCT_UPDATE_GOLDEN=1'
    recorded = json.loads(path.read_text(encoding='utf-8'))
    if current == recorded:
        return
    commits = _commit_check()
    uncovered = sorted(k for k in recorded if k not in current and commits.uncovered(k, current))
    # A field the projects stop reaching is reported as missing coverage, though the commit check
    # counts it as removed, as it may be.
    broken = [
        b
        for b in commits.interface_breaks(recorded, current)
        if b not in {f'{k} is removed' for k in uncovered}
    ]
    added = sorted(set(current) - set(recorded))
    message = [f'the {name} interface differs from {path.relative_to(ROOT)}']
    if broken:
        message += [
            'These break users. Restore them, or make the change deliberately: regenerate the snapshot',
            "with KONSTRUCT_UPDATE_GOLDEN=1 and commit it with both '!' after the type and a",
            'BREAKING CHANGE: footer.',
            *(f'  {item}' for item in broken),
        ]
    if uncovered:
        message += [
            'The projects no longer cover these (what holds them is empty or null); fill it again, or',
            "commit the change with both '!' after the type and a BREAKING CHANGE: footer:",
            *(f'  {k}' for k in uncovered),
        ]
    if added:
        message += [
            'New (regenerate the snapshot with KONSTRUCT_UPDATE_GOLDEN=1):',
            *(f'  {k}' for k in added),
        ]
    others = sorted(k for k in recorded if k in current and current[k] != recorded[k])
    if others and not broken:
        message += ['Changed without breaking (regenerate the snapshot):', *(f'  {k}' for k in others)]
    pytest.fail('\n'.join(message), pytrace=False)


# The command line


def _option(action: argparse.Action) -> str:
    if isinstance(action, argparse._VersionAction | argparse._HelpAction) or action.nargs == 0:
        return 'flag'
    kind = f'option {getattr(action.type, "__name__", "str")}'
    return f'{kind}, required' if action.required else kind


def _positional(action: argparse.Action) -> str:
    """``positional <type>, <arity>``: how many values it takes, and what each is read as."""
    kind = f'positional {getattr(action.type, "__name__", "str")}'
    if isinstance(action.nargs, int):
        return f'{kind}, {action.nargs} value{"s" if action.nargs != 1 else ""}, required'
    arity = {'?': 'optional', '*': 'any number', '+': 'one or more, required'}
    return f'{kind}, {arity.get(str(action.nargs), "required")}'


def cli_entries() -> dict[str, str]:
    """Each command, option (under every spelling) and positional argument (by position, as
    its name is only help text), and separately what an option takes: its default, whether it
    can be given more than once, how many values it takes and its choices. A new way to use an
    option is then an addition, and a changed default a change: every option that takes a value
    records its default, ``None`` included, so a default given where there was none is a change
    too. A positional records its type and how many values it takes, and an optional one its
    default."""
    entries: dict[str, str] = {}

    def walk(command: argparse.ArgumentParser, prefix: str) -> None:
        entries[prefix] = 'command'
        positionals = 0
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f'{prefix} {name}')
                continue
            if action.option_strings:
                for option in action.option_strings:
                    entries[f'{prefix} {option}'] = _option(action)
                named = f'{prefix} {max(action.option_strings, key=len)}'
                if action.nargs != 0 and action.default is not argparse.SUPPRESS:
                    entries[f'{named} default'] = repr(action.default)
                if isinstance(action, argparse._AppendAction | argparse._ExtendAction):
                    entries[f'{named} repeatable'] = 'yes'
                if action.nargs not in (None, 0, '?'):
                    entries[f'{named} values'] = str(action.nargs)
            else:
                named = f'{prefix} <{positionals}>'
                positionals += 1
                entries[named] = _positional(action)
                if action.nargs in ('?', '*') and action.default is not argparse.SUPPRESS:
                    entries[f'{named} default'] = repr(action.default)
            for choice in action.choices or ():
                entries[f'{named} {choice}'] = 'choice'

    walk(parser(), 'konstruct')
    return entries


def test_command_line() -> None:
    check('cli', cli_entries())


# Projects the interfaces are exercised on


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@example.com'}
    env |= {'GIT_COMMITTER_NAME': 'Ada', 'GIT_COMMITTER_EMAIL': 'ada@example.com'}
    subprocess.run(['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args], env=env, check=True)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


@pytest.fixture(scope='module')
def small(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two unconnected tables built from SQL files, one that reads a table whose columns are only
    guessed, and one that does not parse (a warning), built and committed to git with nothing
    changed since."""
    root = tmp_path_factory.mktemp('interfaces') / 'small'
    root.mkdir()
    (root / 'orders.sql').write_text('create table shop.orders as select 1 as amount')
    (root / 'refunds.sql').write_text('create table shop.refunds as select 2 as amount')
    (root / 'fees.sql').write_text('create table shop.fees as select fee from raw.charges')
    (root / 'broken.sql').write_text('create table shop.broken as select from where')
    (root / '.gitignore').write_text('konstruct-out/\n')
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Add the tables')
    Project(root).build(warehouses=False)
    return root


@pytest.fixture(scope='module')
def changed_jaffle(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The built example in git, with a history and a change to review.

    The commits change stg_payments.amount, break and then mend stg_payments.sql (a formula that
    cannot be worked out) and exposures.yml (YAML that cannot be read), change the owner of the
    lifetime value term and add a dbt source. The working tree renames
    stg_payments.payment_method, which orders still reads, changes the lifetime value formula,
    adds a filter to orders and drops one from the DAG's SQL, changes a DAG task, tests, a
    description, an exposure and the source, adds, changes and removes terms, and changes a file
    Konstruct does not analyse. The broken version is tagged ``broken``, and the lifetime value
    term is taken back to agreed and then approved again in new words, so its history changes
    each field from a value to another."""
    root = tmp_path_factory.mktemp('interfaces') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root)
    graph = root / 'konstruct-out' / 'graph.json'
    graph.write_text(graph.read_text().replace(str(jaffle_built_graph), str(root)))
    (root / '.gitignore').write_text('target/\nlogs/\nkonstruct-out/\n*.duckdb\n')
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Add the project')
    payments = root / 'models' / 'staging' / 'stg_payments.sql'
    _edit(payments, 'amount / 100', 'amount / 100.0')
    _git(
        root,
        'commit',
        '-qa',
        '-m',
        'Divide exactly',
        '-m',
        'Co-authored-by: Grace Hopper <grace@example.com>',
    )
    working = payments.read_text()
    payments.write_text('select from where')
    exposures = root / 'models' / 'exposures.yml'
    exposures_text = exposures.read_text()
    exposures.write_text(exposures_text + 'broken: [\n')
    _git(
        root,
        'commit',
        '-qa',
        '-m',
        'Break the payments model and the exposures',
        '-m',
        'Co-authored-by: Grace Hopper <grace@example.com>',
    )
    _git(root, 'tag', 'broken')
    payments.write_text(working)
    exposures.write_text(exposures_text)
    _git(root, 'commit', '-qam', 'Mend the payments model and the exposures')
    definitions = root / 'definitions.yml'
    _edit(
        definitions, 'owner: {name: Priya Shah, role: Head of Finance}', 'owner: {name: Ada, role: Analyst}'
    )
    _git(
        root,
        'commit',
        '-qa',
        '-m',
        'Hand the lifetime value term over',
        '-m',
        'Co-authored-by: Grace Hopper <grace@example.com>',
    )
    _edit(definitions, 'status: approved\n    approved: 2026-10-30\n', 'status: agreed\n')
    _git(root, 'commit', '-qam', 'Take the lifetime value term back to agreed')
    _edit(definitions, 'Total value of every payment', 'Total value of all the payments')
    _edit(definitions, 'status: agreed\n', 'status: approved\n    approved: 2026-11-02\n')
    _git(
        root,
        'commit',
        '-qa',
        '-m',
        'Approve the lifetime value term in new words',
        '-m',
        'Co-authored-by: Grace Hopper <grace@example.com>',
    )
    sources = root / 'models' / 'sources.yml'
    sources.write_text('version: 2\nsources:\n  - name: events\n    tables:\n      - name: raw_events\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Add the events source')
    _edit(sources, 'raw_events', 'raw_visits')
    _edit(
        root / 'models' / 'customers.sql', 'sum(amount) as total_amount', 'sum(amount * 1.0) as total_amount'
    )
    _edit(
        root / 'models' / 'orders.sql',
        'from orders\n',
        "from orders\n    where orders.status != 'returned'\n",
    )
    _edit(root / 'dags' / 'sql' / 'customer_ltv.sql', 'where c.customer_lifetime_value > 0', '')
    dag = root / 'dags' / 'jaffle_shop_daily.py'
    _edit(dag, "task_id='dbt_test'", "task_id='dbt_check'")
    _edit(dag, 'run --select customers orders', 'run --select customers')
    _edit(dag, "sql='sql/customer_ltv.sql'", "sql='sql/lifetime_value.sql'")
    _edit(
        root / 'models' / 'staging' / 'stg_orders.sql',
        'from source\n',
        "from source\n    where status != 'void'\n",
    )
    schema = root / 'models' / 'schema.yml'
    _edit(schema, 'description: This is a unique identifier for a customer', 'description: The customer')
    _edit(
        schema,
        '          - unique\n          - not_null\n',
        '          - not_null\n          - accepted_values:\n              values: [1]\n',
    )
    _edit(root / 'models' / 'exposures.yml', 'customer_dashboard', 'customer_report')
    terms = definitions.read_text()
    last = terms.index('  - name: Payment method')
    definitions.write_text(
        terms[:last]
        + '  - name: Refund\n    definition: Money returned to a customer.\n    status: draft\n'
        + '  - name: Order date\n    definition: When a customer ordered.\n    sensitivity: personal\n'
        + '    columns: [stg_orders.order_date]\n'
    )
    _edit(payments, 'payment_method,', 'payment_method as method,')
    _edit(root / 'dags' / 'jaffle_shop_daily.py', "schedule='@daily'", "schedule='@hourly'")
    _edit(definitions, 'definition: Total of all payments recorded against an order', 'definition: Payments')
    _edit(root / 'models' / 'overview.md', '{% docs __overview__ %}', '{% docs __overview__ %}\nChanged.')
    return root


REVENUE_TERMS = """\
version: 1
terms:
  - name: Revenue
    definition: Money taken for completed orders.
    columns: [daily_revenue.revenue, joined_revenue.revenue]
    filters: ["orders.status = 'completed'"]
  - name: Gross revenue
    definition: Every order's amount, completed or not.
    broader: [Revenue]
    columns: [daily_performance.total_revenue]
"""


@pytest.fixture(scope='module')
def revenue(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Revenue computed in several ways, through filters, joins and terms: conflicts, and terms
    with filters, a broader and a narrower term."""
    from tests.test_conflicts import make_shop

    root = tmp_path_factory.mktemp('interfaces') / 'revenue'
    root.mkdir()
    make_shop(root, invoices=True, definitions=REVENUE_TERMS)
    (root / 'finance' / 'joined.sql').write_text(
        'create table finance.joined_revenue as select o.order_date, sum(i.total) as revenue '
        'from raw.orders o join raw.invoices i on i.id = o.id group by o.order_date'
    )
    Project(root).build()
    return root


def _every_input(root: Path) -> None:
    """One file of each kind status lists that the small project lacks, vocabularies included."""
    (root / 'orchestration').mkdir()
    shutil.copy(ROOT / 'tests' / 'fixtures' / 'dagster' / 'definitions.py', root / 'orchestration')
    shutil.copy(
        ROOT / 'tests' / 'fixtures' / 'ossie' / 'scott_model.ossie.fixture.yaml', root / 'scott.ossie.yaml'
    )
    (root / 'views.lkml').write_text('view: orders {\n  sql_table_name: shop.orders ;;\n}\n')
    (root / 'orders.tmdl').write_text('table orders\n\tcolumn amount\n')
    (root / 'job.py').write_text(
        'from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n'
        "spark.sql('create table shop.copied as select amount from shop.orders')\n"
    )
    (root / 'terms.ttl').write_text(
        '@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n'
        '<urn:revenue> a skos:Concept ; skos:prefLabel "Revenue" .\n'
    )


@pytest.fixture(scope='module')
def inputs(small: Path, jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The small project with one file of every kind status lists, two other repositories (the
    example, and one with every kind of file again), a skill, and an input changed since the
    build."""
    root = tmp_path_factory.mktemp('interfaces') / 'inputs'
    shutil.copytree(small, root)
    graph = root / 'konstruct-out' / 'graph.json'
    graph.write_text(graph.read_text().replace(str(small), str(root)))
    _every_input(root)
    other = root.parent / 'other'
    other.mkdir()
    _every_input(other)
    (other / 'visits.sql').write_text('create table web.visits as select 1 as id')
    (other / 'konstruct.toml').write_text('[vocabularies]\npaths = ["terms.ttl"]\n')
    (root / 'konstruct.toml').write_text(
        f'repositories = ["../other", {json.dumps(str(jaffle_built_graph))}]\n\n'
        '[vocabularies]\npaths = ["terms.ttl"]\n'
    )
    (root / '.claude' / 'skills' / 'konstruct').mkdir(parents=True)
    (root / '.claude' / 'skills' / 'konstruct' / 'SKILL.md').write_text('---\nname: konstruct\n---\n')
    _edit(root / 'orders.sql', '1 as amount', '3 as amount')
    return root


STRIPE = """
create schema stripe;
create table stripe.charge (id varchar, amount bigint, amount_refunded bigint, captured boolean,
    paid boolean, currency varchar, livemode boolean);
create table stripe.customer (id varchar, delinquent boolean, livemode boolean);
"""


@pytest.fixture(scope='module')
def landed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A model over tables Fivetran landed in a DuckDB file inside the project, with Fivetran's
    log (the source system's tables) and Stripe's tables, which bind the stripe ontology pack:
    a build that reads lineage from the warehouse."""
    from tests.test_loaders import LANDED, LOG, warehouse

    root = tmp_path_factory.mktemp('interfaces') / 'landed'
    root.mkdir()
    warehouse(root, LANDED, LOG, STRIPE)
    (root / 'accounts.sql').write_text(
        'create table warehouse.main.accounts as select upper(name) as account_name from salesforce.account'
    )
    Project(root).build(run_dbt=False)
    return root


@pytest.fixture(scope='module')
def measured(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A dbt project whose manifest has a semantic layer, and a Looker view and a Power BI table
    over its model with measures, one of them filtered: the metrics people define in tools."""
    root = tmp_path_factory.mktemp('interfaces') / 'measured'
    (root / 'models').mkdir(parents=True)
    (root / 'dbt_project.yml').write_text('name: shop\nprofile: shop\n')
    sql = 'select 1 as order_id, 2.0 as amount'
    (root / 'models' / 'orders.sql').write_text(sql)
    model = {
        'unique_id': 'model.shop.orders',
        'resource_type': 'model',
        'name': 'orders',
        'alias': 'orders',
        'package_name': 'shop',
        'database': 'wh',
        'schema': 'mart',
        'relation_name': '"wh"."mart"."orders"',
        'original_file_path': 'models/orders.sql',
        'fqn': ['shop', 'orders'],
        'raw_code': sql,
        'compiled_code': sql,
        'language': 'sql',
        'depends_on': {'nodes': [], 'macros': []},
        'config': {'materialized': 'table'},
        'columns': {},
        'checksum': {'checksum': '1'},
    }
    semantic = {
        'name': 'orders',
        'original_file_path': 'models/semantic.yml',
        'depends_on': {'nodes': ['model.shop.orders']},
        'measures': [
            {'name': 'order_total', 'agg': 'sum', 'expr': 'amount', 'non_additive_dimension': {'name': 'day'}}
        ],
    }
    revenue = {
        'name': 'revenue',
        'label': 'Revenue',
        'description': 'Every payment.',
        'type': 'simple',
        'original_file_path': 'models/semantic.yml',
        'type_params': {'measure': {'name': 'order_total'}},
        'filter': {'where_filters': [{'where_sql_template': "{{ Dimension('order__status') }} = 'done'"}]},
    }
    manifest = {
        'metadata': {'adapter_type': 'duckdb'},
        'nodes': {model['unique_id']: model},
        'sources': {},
        'exposures': {},
        'semantic_models': {'semantic_model.shop.orders': semantic},
        'metrics': {'metric.shop.revenue': revenue},
    }
    (root / 'target').mkdir()
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'views.lkml').write_text(
        'view: orders {\n  sql_table_name: wh.mart.orders ;;\n'
        '  measure: total { type: sum sql: ${TABLE}.amount ;; }\n'
        '  measure: big { type: count filters: [amount: ">1"] }\n}\n'
    )
    Project(root).build(run_dbt=False, warehouses=False)
    return root


@pytest.fixture(scope='module')
def emptied(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keyed tables with no rows: verify checks no key or relationship, and says why."""
    import duckdb

    root = tmp_path_factory.mktemp('interfaces') / 'emptied'
    root.mkdir()
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create table main.customers (customer_id integer primary key)')
    con.execute('create table main.orders (order_id integer primary key, customer_id integer)')
    con.close()
    (root / 'report.sql').write_text(
        'create table main.report as select o.order_id from main.orders o '
        'join main.customers c on o.customer_id = c.customer_id'
    )
    Project(root).build()
    return root


@pytest.fixture(scope='module')
def misbound(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An entity whose key has no uniqueness test: an ontology rule broken."""
    from tests.test_ontology import _sql_project

    root = tmp_path_factory.mktemp('interfaces') / 'misbound'
    root.mkdir()
    Project(_sql_project(root)).build(dialect='duckdb', warehouses=False, run_dbt=False)
    return root


@pytest.fixture(scope='module')
def sensitive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Personal data marked in definitions.yml, held, hashed, tokenised, counted, aggregated over
    large enough groups, filtered on and read through SQL whose columns are not known, with every
    [sensitivity] setting."""
    from tests.test_sensitivity import SENSITIVE_PROJECT, write_project

    root = tmp_path_factory.mktemp('interfaces') / 'sensitive'
    write_project(root, SENSITIVE_PROJECT)
    Project(root).build(dialect='duckdb', warehouses=False, run_dbt=False)
    return root


@pytest.fixture(scope='module')
def unexplained(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A DuckDB project whose graph lacks an edge the data shows: verify finds it unexplained."""
    from tests.test_verify import make_project

    root = tmp_path_factory.mktemp('interfaces') / 'unexplained'
    root.mkdir()
    make_project(root)
    path = root / 'konstruct-out' / 'graph.json'
    data = json.loads(path.read_text())
    data['edges'] = [
        e
        for e in data['edges']
        if not (
            e['kind'] == 'derived_from'
            and e['source'].endswith('#doubled')
            and e['target'].endswith('#amount')
        )
    ]
    path.write_text(json.dumps(data))
    return root


CHECKED_QUERIES = {
    'passes': 'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by region',
    'breaks': 'select c.region, sum(o.amount) as net_revenue from raw.orders o '
    'join raw.customers c on c.customer_id = o.customer_id group by c.region',
    'warns': 'select sum(gross_revenue) as revenue from mart.customer_revenue',
    'unchecked': 'select p.customer_id, p.returned '
    'from (pivot raw.orders on status using sum(amount) group by customer_id) p',
}


COMPLETED_ORDER_AMOUNT = """\
  - name: Completed order amount
    kind: measure
    definition: What the customer was charged for an order they received and kept.
    status: approved
    owner: {name: Ana Ruiz, role: Finance}
    approved: 2026-10-01
    columns: [orders.amount]
    filters: ["orders.status = 'completed'"]
"""


@pytest.fixture(scope='module')
def checked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A shop with business terms and a query file for each outcome of check-sql: one that passes,
    one that breaks an approved definition, one that only warns and one it cannot analyse."""
    from tests.test_checksql import make_shop

    root = tmp_path_factory.mktemp('interfaces') / 'checked'
    root.mkdir()
    make_shop(root)
    for name, sql in CHECKED_QUERIES.items():
        (root / f'{name}.sql').write_text(sql)
    return root


def _receipted(root: Path, add: Any) -> None:
    """A shop with receipts issued (each passed to ``add``), and then a formula and a filter
    changed, terms reworded and a column removed, committed and rebuilt, with one receipt edited
    by hand and a line that is not JSON: receipts that hold, are stale, tampered and unreadable."""
    from tests.test_receipts import commit, edit, make_shop, rebuild

    project = make_shop(root)
    base = ['-C', str(root), 'receipts']
    for refs in (['report.doubled', 'Churn'], ['Lifetime value'], ['ltv.orders'], ['ltv.customer_id']):
        add('receipts issue', *base, 'issue', *refs, '--question', 'How much?', '--answer', '70', '--json')
    [path] = (root / 'konstruct-out' / 'receipts').glob('*.jsonl')
    first = json.loads(path.read_text().splitlines()[0])
    add('receipts show', *base, 'show', first['id'], '--json')
    edit(root / 'ltv.sql', 'sum(amount)', 'sum(amount * 1.2)')
    edit(root / 'ltv.sql', "status <> 'returned'", "status not in ('returned', 'void')")
    edit(root / 'ltv.sql', ', count(*) as orders', '')
    edit(root / 'definitions.yml', 'What a customer has paid.', 'What a customer has paid, net of refunds.')
    edit(root / 'definitions.yml', 'A customer who stopped ordering.', 'A customer with no order in 90 days.')
    commit(root, 'Change the lifetime value')
    rebuild(project)
    lines = path.read_text().splitlines()
    lines[-1] = lines[-1].replace('"How much?"', '"How many?"')
    path.write_text('\n'.join([*lines, '{"not": "closed"']) + '\n')


# Exit statuses


def test_exit_statuses(
    tmp_path: Path,
    jaffle_built_graph: Path,
    small: Path,
    changed_jaffle: Path,
    revenue: Path,
    misbound: Path,
    unexplained: Path,
    checked: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    dbt_on_path: None,
) -> None:
    nothing = tmp_path / 'nothing'
    nothing.mkdir()
    newer = tmp_path / 'newer'
    (newer / 'konstruct-out').mkdir(parents=True)
    graph = json.loads((small / 'konstruct-out' / 'graph.json').read_text())
    (newer / 'konstruct-out' / 'graph.json').write_text(json.dumps({**graph, 'format_version': 99}))
    copy = tmp_path / 'unexplained'
    shutil.copytree(unexplained, copy)
    apart = tmp_path / 'apart'  # two entities, keyed by primary keys, that nothing joins
    apart.mkdir()
    import duckdb

    con = duckdb.connect(str(apart / 'shop.duckdb'))
    con.execute('create table main.stores (store_id integer primary key)')
    con.execute('create table main.regions (region_id integer primary key)')
    con.close()

    def status(*args: str) -> int:
        try:
            return main(['--no-color', '-q', *args])
        except SystemExit as stop:
            return int(stop.code or 0)

    def recorded(*args: str) -> int:
        with monkeypatch.context() as patch:
            _recorded_history(patch)
            return status('-C', str(checked), *args)

    def interrupted() -> int:
        with monkeypatch.context() as patch:
            patch.setattr(cli, 'cmd_list', lambda args, console: (_ for _ in ()).throw(KeyboardInterrupt))
            return status('-C', str(small), 'list')

    outcomes = {
        'build': status('build', str(small), '--no-warehouse'),
        'build --strict with warnings': status('build', str(small), '--no-warehouse', '--strict'),
        'no command': status(),
        'usage error': status('list', '--no-such-option'),
        'version': status('--version'),
        'question answered': status('-C', str(small), 'describe', 'orders'),
        'name matches nothing': status('-C', str(small), 'describe', 'no_such_table'),
        'path finds no connection': status('-C', str(small), 'path', 'orders', 'refunds'),
        'entity matches nothing': status('-C', str(small), 'entity', 'no such entity'),
        'relate finds no connection': status('-C', str(apart), 'relate', 'store', 'region'),
        'compile-metric compiles': status(
            '-C', str(jaffle_built_graph), 'compile-metric', 'lifetime value', '--by', 'customer'
        ),
        'compile-metric refuses a join that repeats rows': status(
            '-C', str(jaffle_built_graph), 'compile-metric', 'lifetime value', '--by', 'order.status'
        ),
        'compile-metric matches nothing': status('-C', str(small), 'compile-metric', 'no such metric'),
        'metric matches nothing': status('-C', str(small), 'metric', 'no such metric'),
        'define matches nothing': status('-C', str(small), 'define', 'no such term'),
        'define finds no term on a column': status('-C', str(small), 'define', 'orders.amount'),
        'list with an unknown kind': status('-C', str(small), 'list', 'widgets'),
        'no graph with --no-build': status('-C', str(nothing), 'list', '--no-build'),
        'a graph from a newer Konstruct': status('-C', str(newer), 'list'),
        'interrupted': interrupted(),
        'conflicts finds some': status('-C', str(revenue), 'conflicts', '--no-write'),
        'conflicts --fail-on-conflict finds some': status(
            '-C', str(revenue), 'conflicts', '--no-write', '--fail-on-conflict'
        ),
        'conflicts --fail-on-conflict finds none': status(
            '-C', str(jaffle_built_graph), 'conflicts', '--no-write', '--fail-on-conflict'
        ),
        'ontology with a broken rule': status('-C', str(misbound), 'ontology'),
        'ontology --strict with a broken rule': status('-C', str(misbound), 'ontology', '--strict'),
        'verify finds no warehouse': status('-C', str(small), 'verify', '--no-build'),
        'verify with an unexplained influence': status('-C', str(unexplained), 'verify'),
        'verify --strict with an unexplained influence': status('-C', str(copy), 'verify', '--strict'),
        'diff outside git': status('-C', str(nothing), 'diff'),
        'diff with a breaking change': status('-C', str(changed_jaffle), 'diff', '--base', 'HEAD'),
        'diff --fail-on-breaking with a breaking change': status(
            '-C', str(changed_jaffle), 'diff', '--base', 'HEAD', '--fail-on-breaking'
        ),
        'gate fails': status('-C', str(changed_jaffle), 'gate', '--base', 'HEAD', '--no-update'),
        'gate passes': status('-C', str(small), 'gate', '--base', 'HEAD', '--no-update'),
        'check-sql passes': status('-C', str(checked), 'check-sql', str(checked / 'passes.sql')),
        'check-sql breaks an approved definition': status(
            '-C', str(checked), 'check-sql', str(checked / 'breaks.sql')
        ),
        'check-sql with a warning': status('-C', str(checked), 'check-sql', str(checked / 'warns.sql')),
        'check-sql --strict with a warning': status(
            '-C', str(checked), 'check-sql', str(checked / 'warns.sql'), '--strict'
        ),
        'check-sql with a statement it cannot analyse': status(
            '-C', str(checked), 'check-sql', str(checked / 'unchecked.sql')
        ),
        'check-sql with no SQL': status('-C', str(checked), 'check-sql'),
        'check-sql --history with no warehouse to read': status('-C', str(checked), 'check-sql', '--history'),
        'check-sql --history with --days below 1': status(
            '-C', str(checked), 'check-sql', '--history', '--days', '0'
        ),
        'check-sql --history with a shadow metric of an approved term': recorded('check-sql', '--history'),
        'check-sql --history --strict with a shadow metric of an approved term': recorded(
            'check-sql', '--history', '--strict'
        ),
        **_receipt_statuses(tmp_path, status),
        **_law_statuses(tmp_path, status),
        **_lock_statuses(tmp_path, status),
    }
    capsys.readouterr()
    check('exit', outcomes)


def _receipt_statuses(tmp_path: Path, status: Any) -> dict[str, int]:
    from tests.test_receipts import edit, make_shop

    root = tmp_path / 'receipts'
    make_shop(root)
    base = ['-C', str(root), 'receipts']
    outcomes = {
        'receipts issue': status(*base, 'issue', 'ltv.total'),
        'receipts issue names nothing': status(*base, 'issue', 'no_such_column'),
        'receipts show finds no such receipt': status(*base, 'show', 'r-none'),
        'receipts check passes': status(*base, 'check'),
    }
    edit(root / 'ltv.sql', 'sum(amount)', 'sum(amount * 2)')
    outcomes['receipts check with a graph older than its inputs'] = status(*base, 'check', '--no-refresh')
    outcomes['receipts check finds a stale receipt'] = status(*base, 'check', '--no-history')
    return outcomes


def _law_statuses(tmp_path: Path, status: Any) -> dict[str, int]:
    import duckdb

    from tests.test_laws import shop

    fanned = shop(tmp_path / 'fanned')
    con = duckdb.connect(str(fanned.root / 'shop.duckdb'))
    con.execute("insert into raw.orders values (7, 2, 'placed')")
    con.close()
    laws = ['-C', str(fanned.root), 'verify', '--laws']
    return {
        'verify --laws with a broken law': status(*laws),
        'verify --laws --strict with a broken law': status(*laws, '--strict'),
        'verify --tolerance without --laws': status('-C', str(fanned.root), 'verify', '--tolerance', '0.1'),
        'verify --laws with --mutate': status(*laws, '--mutate'),
    }


def _lock_statuses(tmp_path: Path, status: Any) -> dict[str, int]:
    from tests.test_lock import LTV, REPORT, commit, git

    outcomes: dict[str, int] = {}
    finance, consumer = tmp_path / 'finance', tmp_path / 'consumer'
    finance.mkdir()
    consumer.mkdir()
    (finance / 'ltv.sql').write_text(LTV)
    git(finance, 'init', '-q', '-b', 'main')
    commit(finance, 'Add lifetime value', 'Ada')
    (consumer / 'report.sql').write_text(REPORT)
    (consumer / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../finance"]\n')
    pinned = ['-C', str(consumer), '--trust-config', 'lock']
    outcomes['lock --check with no lockfile'] = status(*pinned, '--check')
    outcomes['lock'] = status(*pinned)
    outcomes['lock --check with nothing changed'] = status(*pinned, '--check')
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount) * 2'))
    outcomes['lock --check with a changed pin'] = status(*pinned, '--check', '--refresh')
    outcomes['lock with a name that matches nothing'] = status(*pinned, 'no_such_relation')
    outcomes['lock --no-history without --check'] = status(*pinned, '--no-history')
    return outcomes


# JSON documents


def _type(value: Any) -> str:
    if value is None:
        return 'null'
    return {bool: 'bool', int: 'int', float: 'float', str: 'str', list: 'list', dict: 'object'}[type(value)]


# Objects keyed by data (entity names, node kinds, input names) rather than by field names.
MAPS = (
    'build .nodes',
    'build .edges',
    'graph.json .meta.inputs',
    'graph.json .meta.integrity.keys',
    'graph.json .meta.integrity.links',
    'graph.json .nodes[].attrs.measures',
    'graph.json .nodes[].attrs.measure_filters',
    'check-sql --history .rules',
)


def shape(document: str, value: Any) -> dict[str, str]:
    """Each field of ``value`` by path (``.key``, ``[]`` for list items, ``{}`` for the values of
    an object keyed by data) with the types it holds."""
    found: dict[str, set[str]] = {}

    def visit(item: Any, path: str) -> None:
        if path:
            found.setdefault(path, set()).add(_type(item))
        if isinstance(item, dict):
            keyed = path.endswith('.entities') or f'{document} {path}' in MAPS
            for key, child in item.items():
                visit(child, f'{path}{{}}' if keyed else f'{path}.{key}')
        elif isinstance(item, list):
            for child in item:
                visit(child, f'{path}[]')

    visit(value, '')
    return {f'json {document} {path}': '|'.join(sorted(types)) for path, types in found.items()}


# The commands each document is inferred from, by project; several runs add their fields up.
QUESTIONS: dict[str, list[list[str]]] = {
    'impact': [
        ['impact', 'stg_payments.amount'],
        ['impact', 'customers'],
        ['impact', 'customers.customer_lifetime_value'],
    ],
    'trace': [
        ['trace', 'customer_ltv.lifetime_value'],
        ['trace', 'orders'],
        ['trace', 'customers.customer_id'],
    ],
    'path': [['path', 'raw_payments', 'customer_dashboard']],
    'search': [['search', 'lifetime', 'value']],
    'describe': [
        ['describe', 'orders'],
        ['describe', 'customers.customer_lifetime_value'],
        ['describe', 'jaffle_shop_daily'],
        ['describe', 'jaffle_shop_daily.build_customer_ltv'],
        ['describe', 'jaffle_shop_daily.dbt_run_marts'],
        ['describe', 'Customer lifetime value'],
    ],
    'list': [['list'], ['list', 'columns', 'tasks', 'tests', 'dags', 'exposures']],
    'query': [
        ['query', 'what breaks if I change payment_method'],
        ['query', 'where does lifetime value come from'],
        ['query', 'what breaks if I change customer_lifetime_value'],
        ['query', 'where does customer_id come from'],
    ],
    'define': [
        ['define', 'lifetime', 'value'],
        ['define', 'customers.customer_lifetime_value'],
        ['define', 'completed', 'order'],
    ],
    'ontology': [['ontology']],
    'entity': [['entity', 'customer'], ['entity', 'order']],
    'metric': [['metric'], ['metric', 'lifetime', 'value']],
    'relate': [['relate', 'customer', 'payment'], ['relate', 'payment', 'customer']],
    'compile-metric': [
        ['compile-metric', 'lifetime', 'value', '--by', 'customer'],
        ['compile-metric', 'lifetime', 'value', '--by', 'order.status'],
        [
            'compile-metric',
            'payment',
            'amount',
            '--by',
            'customer.first_name',
            '--by',
            'order.order_date:month',
            '--where',
            "payment_method = 'coupon'",
            '--since',
            '2018-01-01',
        ],
    ],
    'explain': [
        ['explain', 'customers.customer_lifetime_value'],
        ['explain', 'customer_ltv.lifetime_value'],
        ['explain', 'customers.customer_id'],
        ['explain', 'customer_ltv.lifetime_value', '--english'],
        ['explain', 'orders.credit_card_amount', '--english'],
        ['explain', 'stg_payments.payment_id', '--english'],
    ],
    'conflicts': [['conflicts', '--no-write']],
    'sensitivity': [['sensitivity'], ['sensitivity', 'customers.first_name']],
    'erasure': [['erasure', 'customer'], ['erasure', 'orders.customer_id']],
    'status': [['status']],
}

# Lists none of the projects fills, each with why; every other list must show its items' shape.
_DOWNSTREAM = 'only a downstream walk (impact) reaches tests, exposures and relations that join or filter'
_UPSTREAM = "only a trace lists the columns that filter or join the traced column's relation"
EMPTY: dict[str, str] = {
    'json explain .english.verbatim': 'the grammar words every formula in the example; the parts it '
    'quotes are listed as strings (tests/test_english.py)',
    'json trace .tests': _DOWNSTREAM,
    'json trace .exposures': _DOWNSTREAM,
    'json trace .joins_and_filters': _DOWNSTREAM,
    'json query .lineage.tests': _DOWNSTREAM,
    'json query .lineage.exposures': _DOWNSTREAM,
    'json query .lineage.joins_and_filters': _DOWNSTREAM,
    'json impact .filtered_by': _UPSTREAM,
    'json query .impact.filtered_by': _UPSTREAM,
    'json diff .relations[].columns[].impact.filtered_by': _UPSTREAM,
    'json diff .relations[].rows_impact.filtered_by': _UPSTREAM,
    'json diff .relations[].rows_impact.joins_and_filters': (
        "a relation's impact reaches whole relations; only a column's lists those that join or filter on it"
    ),
    # A conflict's two sides are one type, Side; the projects fill each of these on the other side.
    'json conflicts .conflicts[].a.carried_into': 'the same field as .conflicts[].b.carried_into',
    'json conflicts .conflicts[].b.filters': 'the same field as .conflicts[].a.filters',
    # Plugins are installed distributions; the projects run with none installed or enabled.
    'json status .plugins': 'only an installed plugin distribution fills it',
    'json status .missing_plugins': 'only a [plugins] enabled name that is not installed fills it',
    'json status .trust.variables': "the projects' konstruct.toml and profiles.yml read no environment variable",
    'json status .ingestion_files': 'the projects have no loader configuration files',
    'json status .repositories[].inputs.ingestion_files': 'the projects have no loader configuration files',
    'json sensitivity .unmasked': 'only a warehouse that reads masking policies (Snowflake, Databricks, BigQuery) fills it',
    'json graph.json .meta.watched_siblings[].exclude': "the projects' konstruct.toml sets no exclude patterns; "
    'each is a string (tests/test_build.py)',
}
# Fields the projects only ever fill with null, each with why; every other field must show the
# type it holds, or a change to that type would pass.
_COMMIT = 'a commit shows its co-authors in .entries[].commit.co_authors; this one has none'
_EFFECT = 'the effect is the same document as release-notes .changes[].effect, filled there'
EMPTY.update(
    {
        f'json timeline {path}.co_authors': _COMMIT
        for path in ('.entries[].changes[].approval', '.entries[].compared_with', '.start', '.unknown[]')
    }
)
EMPTY.update(
    {
        'json timeline .entries[].changes[].before.synonyms': 'synonyms are added, as .after.synonyms shows',
        'json timeline .entries[].changes[].effect.key': _EFFECT,
        'json timeline .notes': 'the projects build at every commit with nothing to note (tests/test_timeline.py)',
    }
)
UNFILLED: dict[str, str] = {
    'json release-notes .changes[].effect': 'release-notes runs no SQL; verify --effect fills it',
    'json release-notes .warehouse': 'release-notes runs no SQL; verify --effect fills it',
    **{
        f'json timeline .entries[].changes[].{side}.{fact}': (
            "only verify's checks of keys and relationships fill it"
            if fact == 'check'
            else "the business terms that name the projects' entities change in no commit (tests/test_timeline.py)"
        )
        for side in ('before', 'after')
        for fact in ('check', 'term', 'description')
    },
    **{
        f'json timeline .entries[].changes[].before.{fact}': 'it is added, as .after shows'
        for fact in ('unit', 'currency')
    },
    **{
        f'json timeline .entries[].changes[].effect.{path}': _EFFECT
        for path in ('columns[].changed', 'columns[].matched', 'gained', 'lost', 'skipped')
    },
    'json lock --check .consumer': (
        "only the gate checks a consumer's lockfile from its producer; `lock --check` checks the project's own"
    ),
    'json sensitivity .columns[].masking_policy': (
        'only a warehouse that reads masking policies (Snowflake, Databricks, BigQuery) fills it'
    ),
}


def gaps(entries: dict[str, Any]) -> list[str]:
    """The fields whose shape the projects do not show, as neither EMPTY nor UNFILLED excuses
    them, and the entries there that no longer excuse anything: a list never filled (its items
    unrecorded) and a field only ever null (its type unrecorded)."""
    fields = {k: str(v).split('|') for k, v in entries.items() if k.startswith('json ')}
    empty = {k for k, types in fields.items() if 'list' in types and f'{k}[]' not in entries}
    null = {k for k, types in fields.items() if types == ['null']}
    problems = [f'fill this list, or list it in EMPTY with why: {k}' for k in sorted(empty - set(EMPTY))]
    problems += [
        f'fill this field, or list it in UNFILLED with why: {k}' for k in sorted(null - set(UNFILLED))
    ]
    problems += [f'EMPTY lists a list that is now filled: {k}' for k in EMPTY if k not in empty]
    problems += [f'UNFILLED lists a field that is now filled: {k}' for k in UNFILLED if k not in null]
    return problems


def _json(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    capsys.readouterr()
    main(['--no-color', '-q', *args])
    captured = capsys.readouterr()
    assert captured.out.strip(), f'konstruct {" ".join(args)} wrote no JSON: {captured.err}'
    return json.loads(captured.out)


def _recorded_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Query history as Snowflake records it, read from the fixture rather than a warehouse, with
    texts the warehouse withheld."""
    from konstruct.inputs import usage
    from tests.test_checksql import recorded

    rows = [(text, user, runs) for text, user, runs in recorded('snowflake')]
    history = usage._history([*rows, ('<REDACTED>', 'EVE', 2)], usage.DATABRICKS_REDACTED)
    monkeypatch.setattr(usage, 'project_history', lambda config, **options: history)


def test_json_documents(
    jaffle_built_graph: Path,
    small: Path,
    changed_jaffle: Path,
    revenue: Path,
    misbound: Path,
    sensitive: Path,
    unexplained: Path,
    inputs: Path,
    landed: Path,
    checked: Path,
    measured: Path,
    emptied: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    dbt_on_path: None,
) -> None:
    # status lists the skills installed in the home directory; leave the person's out of it.
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    documents: dict[str, list[Any]] = {}

    def add(name: str, *args: str) -> None:
        documents.setdefault(name, []).append(_json(capsys, *args))

    for name, invocations in QUESTIONS.items():
        for args in invocations:
            add(name, '-C', str(jaffle_built_graph), *args, '--json')
    add('conflicts', '-C', str(revenue), 'conflicts', '--no-write', '--json')
    add('ontology', '-C', str(misbound), 'ontology', '--json')
    add('sensitivity', '-C', str(sensitive), 'sensitivity', '--json')
    add('status', '-C', str(small), 'status', '--json')
    add('status', '-C', str(inputs), 'status', '--json')
    add('status', '-C', str(landed), 'status', '--json')
    newer = tmp_path / 'newer'
    (newer / 'konstruct-out').mkdir(parents=True)
    graph = json.loads((small / 'konstruct-out' / 'graph.json').read_text())
    (newer / 'konstruct-out' / 'graph.json').write_text(json.dumps({**graph, 'format_version': 99}))
    add('status', '-C', str(newer), 'status', '--json')
    for args in (['define', 'revenue'], ['define', 'gross', 'revenue'], ['query', 'what is revenue']):
        add(args[0], '-C', str(revenue), *args, '--json')
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, copy)
    add('build', 'build', str(copy), '--update', '--json')
    shutil.copytree(small, tmp_path / 'small')
    add('build', 'build', str(tmp_path / 'small'), '--no-warehouse', '--json')
    add('verify', '-C', str(copy), 'verify', '--json')
    # Verified, the ontology's keys and relationships carry what verify found.
    add('ontology', '-C', str(copy), 'ontology', '--json')
    add('entity', '-C', str(copy), 'entity', 'customer', '--json')
    add('relate', '-C', str(copy), 'relate', 'customer', 'payment', '--json')
    add('verify', '-C', str(copy), 'verify', '-m', 'orders', '--json')  # a run limited to one model
    add('verify', '-C', str(unexplained), 'verify', '--json')
    from tests.test_verify import make_project

    (tmp_path / 'noisy').mkdir()
    noisy = make_project(
        tmp_path / 'noisy', 'create table main.noisy as select id, random() as r from raw.orders'
    )
    add('verify', '-C', str(noisy.root), 'verify', '--json')  # a model verify skips, and why
    add('verify', '-C', str(emptied), 'verify', '--json')  # keys and relationships it cannot check
    add('metric', '-C', str(measured), 'metric', '--json')
    from tests import test_checksql

    filtered = tmp_path / 'filtered'
    filtered.mkdir()
    test_checksql.make_shop(filtered, definitions=test_checksql.DEFINITIONS + COMPLETED_ORDER_AMOUNT)
    # A required filter the query applies, and one the metric's own column already applies.
    add(
        'compile-metric',
        '-C',
        str(filtered),
        'compile-metric',
        'completed order amount',
        '--by',
        'order',
        '--json',
    )
    add('compile-metric', '-C', str(filtered), 'compile-metric', 'net revenue', '--by', 'region', '--json')
    for root in (copy, small, revenue, unexplained, landed, measured):
        documents.setdefault('graph.json', []).append(
            json.loads((root / 'konstruct-out' / 'graph.json').read_text())
        )
    git = ['-C', str(changed_jaffle)]
    add('history', *git, 'history', 'stg_payments.amount', '--json')
    add('history', *git, 'history', 'customers.customer_lifetime_value', '--json')
    add('release-notes', *git, 'release-notes', '--since', 'HEAD~7', '--json')
    add('verify', *git, 'verify', '--effect', '--since', 'HEAD~7', '--json')
    from tests.test_release_notes import make_shop

    (tmp_path / 'released').mkdir()
    shop = make_shop(tmp_path / 'released').root
    (shop / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by customer_id'
    )
    from tests.test_history import commit as commit_to

    commit_to(shop, 'Count void orders again', 'Ada', '2026-06-01T10:00:00+00:00')
    (shop / 'ltv.sql').unlink()
    (shop / 'lifetime.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) * 2 as total from raw.orders group by customer_id'
    )
    commit_to(
        shop,
        'Move lifetime value and double it\n\nCo-authored-by: Grace Hopper <grace@example.com>',
        'Ada',
        '2026-07-01T10:00:00+00:00',
    )
    Project(shop).build()
    released = ['-C', str(shop)]
    add('release-notes', *released, 'release-notes', '--json')
    add('verify', *released, 'verify', '--effect', '--json')
    # verify --effect --timeline prints the timeline's document, with each change's effect.
    add('timeline', *released, 'verify', '--effect', '--timeline', '--json')
    from tests.test_timeline import _definitions, _files
    from tests.test_timeline import make_shop as make_timeline_shop

    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'user-cache'))
    meaning = make_timeline_shop(tmp_path / 'timeline' / 'shop')
    (meaning / 'konstruct.toml').write_text('not toml = = =\n')
    commit_to(meaning, 'Break the configuration', 'Linus', '2026-04-01T10:00:00+00:00')
    (meaning / 'konstruct.toml').unlink()
    required = _definitions(filters=True, name='Customer lifetime value', status='approved')
    _files(
        meaning,
        **{
            'definitions.yml': required.replace(
                '    grain: one row per order\n',
                '    grain: one row per order\n    synonyms: [sales]\n    unit: currency\n    currency: GBP\n',
            )
        },
    )
    commit_to(
        meaning,
        'Require the filter again\n\nCo-authored-by: Grace Hopper <grace@example.com>',
        'Sam',
        '2026-04-02T10:00:00+00:00',
    )
    # A business term renamed: the properties bound to it name another term.
    definitions = meaning / 'definitions.yml'
    renamed = definitions.read_text().replace('- name: Customer lifetime value', '- name: Customer value')
    definitions.write_text(renamed)
    commit_to(meaning, 'Rename customer lifetime value', 'Priya', '2026-04-03T10:00:00+00:00')
    timed = ['-C', str(meaning)]
    add('timeline', *timed, 'timeline', '--json')  # from the first commit, with every kind of change
    add('timeline', *timed, 'timeline', '--since', 'v0', '--file', 'definitions.yml', '--json')
    add('diff', *git, 'diff', '--base', 'HEAD', '--format', 'json')
    add('diff', *git, 'diff', '--base', 'broken', '--format', 'json')  # a version that does not parse
    add('gate', *git, 'gate', '--base', 'HEAD', '--no-update', '--format', 'json')
    add('gate', '-C', str(small), 'gate', '--base', 'HEAD', '--format', 'json')
    for name in CHECKED_QUERIES:
        add('check-sql', '-C', str(checked), 'check-sql', str(checked / f'{name}.sql'), '--json')
    _recorded_history(monkeypatch)
    add('check-sql --history', '-C', str(checked), 'check-sql', '--history', '--json')
    _receipted(tmp_path / 'receipts', add)
    add('receipts check', '-C', str(tmp_path / 'receipts'), 'receipts', 'check', '--json')
    _laws_documents(tmp_path, add)
    _lock_documents(tmp_path, add)
    entries: dict[str, Any] = {}
    for name, runs in documents.items():
        versions = {run.get('format_version') for run in runs}
        assert len(versions) == 1 and isinstance(next(iter(versions)), int), f'{name} has no format_version'
        entries[f'format {name}'] = versions.pop()
        merged: dict[str, set[str]] = {}
        for run in runs:
            for key, types in shape(name, run).items():
                merged.setdefault(key, set()).update(types.split('|'))
        entries |= {key: '|'.join(sorted(types)) for key, types in merged.items()}
    # A list the projects leave empty has no recorded items, and a field they leave null no
    # recorded type, so a change to either would pass.
    problems = gaps(entries)
    assert problems == [], '\n'.join(problems)
    check('json', entries)


def _laws_documents(tmp_path: Path, add: Any) -> None:
    """`verify --laws --json` on the shop of test_laws: with a join no test covers, so a measure
    has no law, and a model the probes cannot judge, proposed and confirmed; and confirmed on data
    with a planted fan-out, so laws break."""
    import duckdb

    from tests.test_laws import MODELS, SELECTING_MODELS, raw, shop

    joined = MODELS['payments_enriched'].replace(
        'from p left join o on o.order_id = p.order_id',
        f'from p left join o on o.order_id = p.order_id left join {raw("items")} i on i.order_id = p.order_id',
    )
    models = {**MODELS, 'payments_enriched': joined, 'latest': SELECTING_MODELS['latest']}
    blocked = shop(tmp_path / 'blocked', models)
    add('verify --laws', '-C', str(blocked.root), 'verify', '--laws', '--propose', '--json')
    add('verify --laws', '-C', str(blocked.root), 'verify', '--laws', '--json')
    fanned = shop(tmp_path / 'fanned')
    con = duckdb.connect(str(fanned.root / 'shop.duckdb'))
    con.execute("insert into raw.orders values (7, 2, 'placed')")
    con.close()
    add('verify --laws', '-C', str(fanned.root), 'verify', '--laws', '--json')


def _lock_documents(tmp_path: Path, add: Any) -> None:
    """`lock --json` and `lock --check --json` in the consumer of test_lock, as its producer
    changes a pinned column and the consumer accepts the change."""
    from tests.test_lock import LTV, REPORT, TERM, commit, git

    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    finance.mkdir()
    marketing.mkdir()
    (finance / 'ltv.sql').write_text(LTV)
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='What a customer has paid.', status='approved', approved='2026-10-30')
    )
    git(finance, 'init', '-q', '-b', 'main')
    commit(finance, 'Add lifetime value', 'Ada')
    (finance / 'orders.sql').write_text('create table main.paid as select id, amount from raw.orders')
    commit(finance, 'Add paid orders', 'Ada')
    (marketing / 'report.sql').write_text(REPORT)
    (marketing / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../finance"]\n')
    consumer = ['-C', str(marketing), '--trust-config']
    add('lock', *consumer, 'lock', '--json')
    add('lock --check', *consumer, 'lock', '--check', '--json')
    # A column read since the lock, and not pinned yet, is a note.
    (marketing / 'paid.sql').write_text('create table main.paid_report as select amount from main.paid')
    main(['-q', *consumer, 'build', '--update'])
    add('lock --check', *consumer, 'lock', '--check', '--json')
    (marketing / 'paid.sql').unlink()
    main(['-q', *consumer, 'build', '--update'])
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount) * 2'))
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='Twice what a customer has paid.', status='approved', approved='2026-11-01')
    )
    commit(finance, 'Double lifetime value', 'Ana')
    add('lock --check', *consumer, 'lock', '--check', '--refresh', '--json')
    add('lock', *consumer, 'lock', '--json')
    add('lock', *consumer, 'lock', '--update', '--json')
    add('lock', *consumer, 'lock', 'main.ltv', '--no-write', '--json')
    # A pinned column the producer no longer builds is missing, and removed once accepted.
    (finance / 'ltv.sql').write_text(LTV.replace('as total', 'as lifetime_total'))
    commit(finance, 'Rename total', 'Ana')
    add('lock', *consumer, 'lock', '--refresh', '--no-write', '--json')
    add('lock', *consumer, 'lock', '--update', '--no-write', '--json')


def test_a_field_emptied_to_null_is_missing_coverage(
    revenue: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field the code stops filling, here every conflict's reason, has no type left to compare,
    so the coverage check fails rather than the field passing as compatible."""
    from konstruct.analysis.conflicts import Conflict

    recorded = json.loads((SNAPSHOTS / 'json.json').read_text(encoding='utf-8'))
    others = {k: v for k, v in recorded.items() if not k.startswith('json conflicts ')}
    document = _json(capsys, '-C', str(revenue), 'conflicts', '--no-write', '--json')
    assert gaps(others | shape('conflicts', document)) == []
    filled = Conflict.to_dict
    monkeypatch.setattr(Conflict, 'to_dict', lambda self: {**filled(self), 'reason': None})
    document = _json(capsys, '-C', str(revenue), 'conflicts', '--no-write', '--json')
    assert gaps(others | shape('conflicts', document)) == [
        'fill this field, or list it in UNFILLED with why: json conflicts .conflicts[].reason'
    ]


def test_the_documented_format_versions_are_the_ones_written() -> None:
    """The table in the Compatibility section lists each document and its format version."""
    recorded = json.loads((SNAPSHOTS / 'json.json').read_text(encoding='utf-8'))
    written = {
        key.removeprefix('format '): value for key, value in recorded.items() if key.startswith('format ')
    }
    text = (ROOT / 'docs' / 'reference' / 'outputs.md').read_text(encoding='utf-8')
    table = text.split('| Document | `format_version` |', 1)[1].split('\n\n', 1)[0]
    documented: dict[str, int] = {}
    for row in table.strip().splitlines()[1:]:
        names, version = (cell.strip() for cell in row.strip('|').split('|'))
        for name in names.replace(' and ', ', ').split(', '):
            documented[name.strip('`').removesuffix(' --json')] = int(version)
    files = {'conflicts.json': 'conflicts', 'verify.json': 'verify', 'laws.json': 'verify --laws'}
    assert {files.get(k, k): v for k, v in documented.items()} == written


# MCP


def test_mcp_tools(jaffle_built_graph: Path) -> None:
    async def tools() -> list[Any]:
        async with Client(build_server(jaffle_built_graph)) as client:
            return list((await client.list_tools()).tools)

    entries: dict[str, str] = {}
    for tool in anyio.run(tools):
        entries[f'mcp {tool.name}'] = 'tool'
        schema = tool.input_schema
        required = set(schema.get('required', []))
        for name, spec in schema.get('properties', {}).items():
            options = spec.get('anyOf', [spec])
            kinds = [
                f'array of {o.get("items", {}).get("type", "any")}'
                if o.get('type') == 'array'
                else str(o.get('type'))
                for o in options
            ]
            value = '|'.join(sorted(kinds))
            value += ', required' if name in required else f' = {spec.get("default")!r}'
            entries[f'mcp {tool.name} {name}'] = value
    check('mcp', entries)


# The Python API


def _signature(prefix: str, function: Any, entries: dict[str, str]) -> None:
    """Each parameter with its position (``#0``), unless it is keyword-only, as moving one breaks
    a call that passes it by position."""
    signature = inspect.signature(function)
    position = 0
    for name, parameter in signature.parameters.items():
        if name in ('self', 'cls'):
            continue
        value = str(parameter.annotation) if parameter.annotation is not parameter.empty else 'any'
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            value = f'#{position} {value}'
            position += 1
        elif parameter.kind is parameter.KEYWORD_ONLY:
            value = f'keyword {value}'
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            value += f', {parameter.kind.description}'
        elif parameter.default is parameter.empty:
            value += ', required'
        else:
            value += f' = {parameter.default!r}'
        entries[f'{prefix} {name}'] = value
    if signature.return_annotation is not signature.empty:
        entries[f'{prefix} ->'] = str(signature.return_annotation)


STUBS = ROOT / 'konstruct' / '_core.pyi'


def _stub_signature(prefix: str, function: ast.FunctionDef, entries: dict[str, str]) -> None:
    """``_signature`` for a function of the native core's stub, which is where the types of what
    the core exports are written: a parameter's position, annotation and default."""
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    positional = [a for a in positional if a.arg not in ('self', 'cls')]
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    entries_for: list[tuple[ast.arg, ast.expr | None, str]] = [
        (a, d, 'positional') for a, d in zip(positional, defaults, strict=True)
    ]
    entries_for += [
        (a, d, 'keyword') for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
    ]
    position = 0
    for argument, default, kind in entries_for:
        value = ast.unparse(argument.annotation) if argument.annotation is not None else 'any'
        if kind == 'positional':
            value = f'#{position} {value}'
            position += 1
        else:
            value = f'keyword {value}'
        value += f' = {ast.literal_eval(default)!r}' if default is not None else ', required'
        entries[f'{prefix} {argument.arg}'] = value
    if arguments.kwarg is not None:
        annotation = ast.unparse(arguments.kwarg.annotation) if arguments.kwarg.annotation else 'any'
        entries[f'{prefix} {arguments.kwarg.arg}'] = f'{annotation}, variadic keyword'
    if function.returns is not None:
        entries[f'{prefix} ->'] = ast.unparse(function.returns)


def _stub_entries(prefix: str, name: str, entries: dict[str, str]) -> None:
    """What ``python_entries`` records of a class the native core exports, from its stub: the
    fields of a record, ``__init__`` for anything else, and the methods and properties."""
    tree = ast.parse(STUBS.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    record = name in ('Node', 'Edge', 'Step')
    for item in cls.body:
        if isinstance(item, ast.AnnAssign) and record and not ast.unparse(item.target).startswith('__'):
            entries[f'{prefix}.{ast.unparse(item.target)}'] = f'field {ast.unparse(item.annotation)}'
    for item in cls.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        decorators = {ast.unparse(d) for d in item.decorator_list}
        if item.name == '__init__':
            if not record:
                _stub_signature(prefix, item, entries)
        elif item.name.startswith('_'):
            continue
        elif 'property' in decorators:
            entries[f'{prefix}.{item.name}'] = (
                f'property {ast.unparse(item.returns) if item.returns else "any"}'
            )
        elif 'classmethod' in decorators:
            entries[f'{prefix}.{item.name}'] = 'classmethod'
            _stub_signature(f'{prefix}.{item.name}', item, entries)
        else:
            entries[f'{prefix}.{item.name}'] = 'method'
            _stub_signature(f'{prefix}.{item.name}', item, entries)


def python_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for name in sorted(konstruct.__all__):
        value = getattr(konstruct, name)
        prefix = f'python konstruct.{name}'
        if not inspect.isclass(value):
            entries[prefix] = type(value).__name__
            continue
        entries[prefix] = 'exception' if issubclass(value, BaseException) else 'class'
        if issubclass(value, BaseException):
            continue
        if getattr(_core, name, None) is value:
            _stub_entries(prefix, name, entries)
            continue
        if dataclasses.is_dataclass(value):
            for item in dataclasses.fields(value):
                entries[f'{prefix}.{item.name}'] = f'field {item.type}'
        else:
            _signature(prefix, value.__init__, entries)
        fields = {f.name for f in dataclasses.fields(value)} if dataclasses.is_dataclass(value) else set()
        for attribute, member in vars(value).items():
            if attribute.startswith('_') or attribute in fields:
                continue
            if isinstance(member, property):
                annotation = inspect.signature(member.fget).return_annotation if member.fget else 'any'
                entries[f'{prefix}.{attribute}'] = f'property {annotation}'
            elif isinstance(member, classmethod | staticmethod):
                entries[f'{prefix}.{attribute}'] = type(member).__name__
                _signature(f'{prefix}.{attribute}', getattr(value, attribute), entries)
            elif inspect.isfunction(member):
                entries[f'{prefix}.{attribute}'] = 'method'
                _signature(f'{prefix}.{attribute}', member, entries)
            else:
                entries[f'{prefix}.{attribute}'] = 'attribute'
    return entries


def test_python_api() -> None:
    check('python', python_entries())


# The GitHub Action and the CSV export


def test_github_action() -> None:
    action = yaml.safe_load((ROOT / 'action.yml').read_text(encoding='utf-8'))
    entries = {}
    for name, spec in (action.get('inputs') or {}).items():
        entries[f'action input {name}'] = 'required' if spec.get('required') else f'= {spec.get("default")!r}'
    for name in action.get('outputs') or {}:
        entries[f'action output {name}'] = 'output'
    check('action', entries)


def test_csv_columns(jaffle_built_graph: Path) -> None:
    """The CSV export's header, each column with its position, as readers index by either."""
    header = Project(jaffle_built_graph).export('csv').splitlines()[0].split(',')
    check('csv', {f'csv {column}': f'#{position}' for position, column in enumerate(header)})


# konstruct.toml


class Recorder(dict[str, Any]):
    """An empty configuration table that records every key Konstruct looks up in it."""

    def __init__(self, prefix: str, seen: set[str]) -> None:
        super().__init__()
        self.prefix, self.seen = prefix, seen

    def get(self, key: str, default: Any = None) -> Any:
        self.seen.add(self.prefix + key)
        return Recorder(f'{self.prefix}{key}.', self.seen) if isinstance(default, dict) else default

    def __contains__(self, key: object) -> bool:
        self.seen.add(f'{self.prefix}{key}')
        return False


WAREHOUSES = ('duckdb', 'postgres', 'iceberg', 'snowflake', 'databricks', 'trino', 'bigquery')


def test_configuration_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.engine import config

    seen: set[str] = set()
    (tmp_path / 'konstruct.toml').write_text('')
    monkeypatch.setattr(config, '_read_toml', lambda path: Recorder('', seen))
    config.load_config(tmp_path)
    # A [[warehouse]] entry's keys are read when it connects; record them for each type.
    monkeypatch.setattr(config, 'expand_env', lambda value: value)
    for kind in WAREHOUSES:
        # A missing driver raises; the keys read until then are recorded.
        with contextlib.suppress(Exception):
            config.WarehouseConfig(kind, Recorder(f'warehouse.{kind}.', seen)).connector(tmp_path)
    seen |= {'warehouse', 'warehouse.type'}
    # [sensitivity] is read where the analysis runs, by Project and the gate, not by the build.
    from konstruct.analysis.sensitivity import Settings

    Settings.read(Recorder('sensitivity.', seen))
    seen.add('sensitivity')
    check('config', dict.fromkeys(sorted(seen), 'key'))
