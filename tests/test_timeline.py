"""The ontology time machine: the world at each commit of a range, what each commit changed in the
business's meaning, who made and approved it, and what a formula change does on today's data."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import QueryError
from konstruct.analysis.diff import DiffError
from konstruct.analysis.timeline import Timeline, effects, timeline
from konstruct.core.graph import Graph
from konstruct.inputs import dbt
from konstruct.inputs.dbt_commit import manifest_at
from konstruct.semantics.world_facts import KINDS, World, WorldChange, world_diff
from tests.test_history import commit, git
from tests.test_release_notes import make_pairs
from tests.test_release_notes import make_shop as make_sql_shop

PROJECT = 'name: shop\nprofile: shop\n'
CUSTOMERS = 'select id as customer_id, name as customer_name from raw.customers\n'
ORDERS = 'select id as order_id, customer_id, amount, status from raw.orders\n'
VALUE = (
    "select customer_id, sum(amount) as lifetime_value from {{ ref('orders') }}\nwhere status <> 'void'\n"
    'group by customer_id\n'
)
SCHEMA = """version: 2
models:
  - name: customers
    columns:
      - name: customer_id
        data_tests: [{customer_tests}]
  - name: orders
    columns:
      - name: order_id
        data_tests: [unique, not_null]
      - name: customer_id{relationship}
"""
RELATIONSHIP = """
        data_tests:
          - relationships:
              to: ref('customers')
              field: customer_id"""
DEFINITIONS = """version: 1
terms:
  - name: Revenue
    kind: measure
    definition: What customers paid for their orders.
    owner: {{name: Priya}}
    status: agreed
    grain: one row per order
    columns: [orders.amount]{filters}
  - name: {value_name}
    definition: What a customer has paid across every order.
    owner: {{name: Priya}}
    status: {value_status}
    grain: one row per customer
    columns: [customer_value.lifetime_value]
"""
FILTERS = '\n    filters: ["orders.status <> \'returned\'"]'


def _files(root: Path, **changes: str) -> None:
    for name, text in changes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _definitions(filters: bool = True, name: str = 'Lifetime value', status: str = 'draft') -> str:
    text = DEFINITIONS.format(filters=FILTERS if filters else '', value_name=name, value_status=status)
    if status == 'approved':
        text = text.replace('    status: approved\n', '    status: approved\n    approved: 2026-03-05\n')
    return text


def _manifest(root: Path) -> None:
    """The dbt artefacts as `dbt parse` would leave them at HEAD: never committed."""
    nodes: dict[str, Any] = {}
    for name in ('customers', 'orders', 'customer_value'):
        sql = (root / 'models' / f'{name}.sql').read_text()
        nodes[f'model.shop.{name}'] = {
            'unique_id': f'model.shop.{name}',
            'resource_type': 'model',
            'name': name,
            'alias': name,
            'package_name': 'shop',
            'database': 'wh',
            'schema': 'main',
            'relation_name': f'"wh"."main"."{name}"',
            'original_file_path': f'models/{name}.sql',
            'fqn': ['shop', name],
            'raw_code': sql,
            'compiled_code': sql.replace("{{ ref('orders') }}", '"wh"."main"."orders"'),
            'language': 'sql',
            'depends_on': {'nodes': ['model.shop.orders'] if name == 'customer_value' else [], 'macros': []},
            'config': {'materialized': 'table'},
            'columns': {},
        }
    nodes['test.shop.unique_orders_order_id.1'] = {
        'unique_id': 'test.shop.unique_orders_order_id.1',
        'resource_type': 'test',
        'name': 'unique_orders_order_id',
        'package_name': 'shop',
        'original_file_path': 'models/schema.yml',
        'test_metadata': {'name': 'unique', 'kwargs': {'column_name': 'order_id'}},
        'column_name': 'order_id',
        'attached_node': 'model.shop.orders',
        'depends_on': {'nodes': ['model.shop.orders']},
    }
    (root / 'target').mkdir(exist_ok=True)
    (root / 'target' / 'manifest.json').write_text(
        json.dumps({'metadata': {'adapter_type': 'duckdb'}, 'nodes': nodes, 'sources': {}, 'exposures': {}})
    )


def make_shop(root: Path, *, definitions: bool = True) -> Path:
    """A dbt project in git with five planted changes to its meaning and a reformat."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q', '-b', 'main')
    _files(
        root,
        **{
            '.gitignore': 'target/\nkonstruct-out/\n',
            'dbt_project.yml': PROJECT,
            'models/customers.sql': CUSTOMERS,
            'models/orders.sql': ORDERS,
            'models/customer_value.sql': VALUE,
            'models/schema.yml': SCHEMA.format(customer_tests='unique, not_null', relationship=''),
        },
    )
    if definitions:
        _files(root, **{'definitions.yml': _definitions()})
    commit(root, 'Add the shop', 'Ada', '2026-01-05T10:00:00+00:00')
    git(root, 'tag', 'v0')
    if definitions:
        _files(root, **{'definitions.yml': _definitions(filters=False)})
        commit(root, 'Count returned orders in revenue', 'Sam', '2026-02-01T10:00:00+00:00')
        _files(root, **{'definitions.yml': _definitions(filters=False, name='Customer lifetime value')})
        commit(root, 'Rename lifetime value', 'Sam', '2026-02-10T10:00:00+00:00')
    schema = SCHEMA.format(customer_tests='unique, not_null', relationship=RELATIONSHIP)
    _files(root, **{'models/schema.yml': schema})
    commit(root, 'Test that orders belong to customers', 'Ada', '2026-02-20T10:00:00+00:00')
    if definitions:
        approved = _definitions(filters=False, name='Customer lifetime value', status='approved')
        _files(root, **{'definitions.yml': approved})
        commit(root, 'Approve customer lifetime value', 'Priya', '2026-03-05T10:00:00+00:00')
    _files(root, **{'models/schema.yml': SCHEMA.format(customer_tests='not_null', relationship=RELATIONSHIP)})
    commit(root, 'Drop the customer key test', 'Ada', '2026-03-10T10:00:00+00:00')
    _files(root, **{'models/customers.sql': CUSTOMERS.upper().replace('FROM', '\n  FROM')})
    commit(root, 'Reformat customers', 'Linus', '2026-03-20T10:00:00+00:00')
    _manifest(root)
    return root


@pytest.fixture(autouse=True)
def private_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's cache, in the test's own directory, and not in CI, so worlds are cached."""
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'user-cache'))
    return tmp_path / 'user-cache' / 'konstruct' / 'timeline'


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    return make_shop(tmp_path / 'shop')


def _sha(root: Path, ref: str) -> str:
    return git(root, 'rev-parse', '--short=7', ref).strip()


def _entries(result: Timeline) -> list[tuple[str, str, list[tuple[str, list[str]]]]]:
    return [
        (e.commit.subject, e.commit.author, [(c.headline(), c.english) for c in e.changes])
        for e in result.entries
    ]


RELATIONSHIP_ADDED = (
    "relationship 'order -> customer (orders.customer_id = customers.customer_id)'",
    'each order now refers to a customer through orders.customer_id = customers.customer_id, many-to-one, '
    'trusted as declared',
)


def test_each_commit_says_what_it_changed_in_the_business(shop: Path) -> None:
    result = timeline(shop, since='v0')
    assert result.commits == 6 and result.unknown == [] and result.notes == []
    assert result.start is not None and result.start.subject == 'Add the shop'
    assert _entries(result) == [
        (
            'Count returned orders in revenue',
            'Sam',
            [
                (
                    "metric 'Revenue': filters changed",
                    [
                        "answers no longer have to keep only rows where [status] has a value other than 'returned'"
                    ],
                )
            ],
        ),
        (
            'Rename lifetime value',
            'Sam',
            [
                (
                    "metric 'Customer lifetime value': renamed from 'Lifetime value'",
                    ["renamed from 'Lifetime value' to 'Customer lifetime value'"],
                )
            ],
        ),
        (
            'Test that orders belong to customers',
            'Ada',
            [
                (f'{RELATIONSHIP_ADDED[0]}: added', [RELATIONSHIP_ADDED[1]]),
                ("property 'order.customer_id': refers_to changed", ['it now refers to customer']),
            ],
        ),
        (
            'Approve customer lifetime value',
            'Priya',
            [
                (
                    "metric 'Customer lifetime value': approved, status changed",
                    ['approved on 2026-03-05', 'its status changed from draft to approved'],
                )
            ],
        ),
        (
            'Drop the customer key test',
            'Ada',
            [
                (
                    "entity 'customer': confidence, evidence, keys, relations changed",
                    [
                        'trust fell from declared to inferred',
                        'evidence no longer: unique test on customers.customer_id',
                        "evidence now: grain of 'Customer lifetime value'",
                        'keys no longer: customers.customer_id',
                        'keys now: customer_value.customer_id',
                        'relations no longer: customers',
                        'relations now: customer_value',
                    ],
                ),
                (
                    f'{RELATIONSHIP_ADDED[0]}: removed',
                    [
                        'each order no longer refers to a customer through orders.customer_id = customers.customer_id'
                    ],
                ),
                (
                    "property 'customer.customer_name': removed",
                    ['customer no longer has the property customer_name'],
                ),
                (
                    "property 'customer.lifetime_value': added",
                    ['customer has a new property, lifetime_value'],
                ),
                ("property 'order.customer_id': refers_to changed", ['it no longer refers to customer']),
                (
                    "metric 'customer count': formula changed",
                    [
                        'it was the number of distinct values of [customer id (customers)]; it is now the number '
                        'of distinct values of [customer id (customer_value)]'
                    ],
                ),
            ],
        ),
    ]
    # The commit that only reformats SQL is read but changes nothing.
    assert 'Reformat customers' not in [e.commit.subject for e in result.entries]
    filters = result.entries[0].changes[0]
    assert filters.fields == {'filters': (["orders.status <> 'returned'"], [])}
    renamed = result.entries[1].changes[0]
    assert (renamed.status, renamed.renamed_from, renamed.fields) == ('renamed', 'Lifetime value', {})
    approval = result.entries[3].changes[0]
    assert approval.fields == {'approved': (None, '2026-03-05'), 'status': ('draft', 'approved')}
    assert approval.after is not None and approval.after['owner'] == 'Priya'
    trust = result.entries[4].changes[0]
    assert trust.fields['confidence'] == ('declared', 'inferred')


def test_release_notes_say_who_changed_and_approved_each_metric(shop: Path) -> None:
    result = timeline(shop, since='v0')
    approve, rename, drop = _sha(shop, 'HEAD~2'), _sha(shop, 'HEAD~4'), _sha(shop, 'HEAD~1')
    assert result.release_notes() == [
        f'customer count: the formula changed in {drop} (Ada); it was the number of distinct values of '
        '[customer id (customers)]; it is now the number of distinct values of [customer id (customer_value)]',
        f'Customer lifetime value: approved in {approve} (Priya)',
        f"Customer lifetime value: renamed from 'Lifetime value' in {rename} (Sam); approved in a commit by "
        f'Priya on 2026-03-05 ({approve})',
        "Revenue: the required filter that keeps only rows where [status] has a value other than 'returned' "
        f'was removed in {_sha(shop, "HEAD~5")} (Sam); agreed, not approved since',
    ]
    renamed = result.entries[1].changes[0]
    assert renamed.approval is not None and renamed.approval.author == 'Priya'
    assert result.entries[0].changes[0].approval is None


def test_a_project_without_definitions_uses_the_derived_world(tmp_path: Path) -> None:
    root = make_shop(tmp_path / 'shop', definitions=False)
    assert not (root / 'definitions.yml').exists()
    result = timeline(root, since='v0')
    assert result.commits == 3
    assert _entries(result) == [
        (
            'Test that orders belong to customers',
            'Ada',
            [
                (f'{RELATIONSHIP_ADDED[0]}: added', [RELATIONSHIP_ADDED[1]]),
                ("property 'order.customer_id': refers_to changed", ['it now refers to customer']),
            ],
        ),
        (
            'Drop the customer key test',
            'Ada',
            [
                ("entity 'customer': removed", ['the entity is no longer found']),
                (
                    f'{RELATIONSHIP_ADDED[0]}: removed',
                    [
                        'each order no longer refers to a customer through orders.customer_id = customers.customer_id'
                    ],
                ),
                (
                    "property 'customer.customer_name': removed",
                    ['customer no longer has the property customer_name'],
                ),
                ("property 'order.customer_id': refers_to changed", ['it no longer refers to customer']),
                ("metric 'customer count': removed", ['the metric is no longer found']),
                ("metric 'lifetime value': removed", ['the metric is no longer found']),
            ],
        ),
    ]
    proposed = result.entries[1].changes[-1].before
    assert proposed is not None and proposed['proposed'] is True and proposed['status'] is None


def test_the_first_commit_is_compared_with_an_empty_project(shop: Path) -> None:
    result = timeline(shop, until='v0')
    assert result.commits == 1 and result.start is None
    (entry,) = result.entries
    assert [c.headline() for c in entry.changes if c.kind != 'property'] == [
        "entity 'customer': added",
        "entity 'order': added",
        "metric 'Lifetime value': added",
        "metric 'Revenue': added",
        "metric 'customer count': added",
        "metric 'order amount': added",
        "metric 'order count': added",
    ]
    revenue = next(c for c in entry.changes if c.name == 'Revenue')
    assert revenue.english == [
        "a new metric, trusted as declared; answers keep only rows where [status] has a value other than 'returned'"
    ]
    assert entry.changes[0].english == ['a new entity, identified by customer_id, trusted as declared']


def test_the_range_options(shop: Path) -> None:
    subjects = [e.commit.subject for e in timeline(shop).entries]
    assert subjects[0] == 'Add the shop' and len(subjects) == 6
    # since a commit, a tag or a date; until a commit.
    by_sha = timeline(shop, since=_sha(shop, 'HEAD~3'))
    assert [e.commit.subject for e in by_sha.entries] == [
        'Approve customer lifetime value',
        'Drop the customer key test',
    ]
    assert by_sha.commits == 3 and by_sha.since == _sha(shop, 'HEAD~3')
    by_date = timeline(shop, since='2026-02-15')
    assert [e.commit.subject for e in by_date.entries] == [s for s in subjects if s != 'Add the shop'][2:]
    until = timeline(shop, since='v0', until='HEAD~4')
    assert [e.commit.subject for e in until.entries] == [
        'Count returned orders in revenue',
        'Rename lifetime value',
    ]
    assert until.until == 'HEAD~4'
    # Only commits that changed these paths, each still compared with its whole parent.
    paths = timeline(shop, since='v0', paths=['models/schema.yml'])
    assert paths.commits == 2 and paths.paths == ['models/schema.yml']
    assert [e.commit.subject for e in paths.entries] == [
        'Test that orders belong to customers',
        'Drop the customer key test',
    ]
    limited = timeline(shop, limit=2)
    assert limited.commits == 2 and limited.notes == [
        'only the last 2 commits were read; raise the limit to read more'
    ]
    assert [e.commit.subject for e in limited.entries] == ['Drop the customer key test']
    assert timeline(shop, since='HEAD').commits == 0
    with pytest.raises(DiffError, match='is not a commit'):
        timeline(shop, since='no-such-ref')
    with pytest.raises(ValueError, match='at least 1'):
        timeline(shop, limit=0)


def test_the_result_as_text_markdown_and_json(shop: Path) -> None:
    result = timeline(shop, since='v0')
    text = str(result)
    assert text.startswith(
        'Timeline since v0 (HEAD)\n11 changes to the business in 5 of 6 commits since v0.\n'
    )
    assert (
        "    metric 'Revenue': filters changed\n      - answers no longer have to keep only rows where"
        in text
    )
    markdown = result.markdown()
    assert markdown.startswith(
        '## Timeline since v0\n\n11 changes to the business in 5 of 6 commits since v0.\n'
    )
    assert '- **relationship ' in markdown and 'Sam: Rename lifetime value' in markdown
    data = json.loads(json.dumps(result.to_dict()))
    assert data['format_version'] == 1 and data['commits'] == 6 and len(data['entries']) == 5
    assert data['release_notes'] == result.release_notes()
    first = data['entries'][0]
    assert first['commit']['author'] == 'Sam'
    assert first['changes'][0]['fields'] == [
        {'name': 'filters', 'before': ["orders.status <> 'returned'"], 'after': []}
    ]
    assert timeline(shop, since='HEAD').to_dict()['entries'] == []


def test_worlds_are_cached_by_commit(
    shop: Path, private_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = timeline(shop, since='v0')
    worlds = sorted(private_cache.glob('world-*.json'))
    assert len(worlds) == 7 and len(list(private_cache.glob('analysis-*.json'))) == 1
    assert all(oct(p.stat().st_mode & 0o777) == '0o600' for p in worlds)
    assert oct(private_cache.stat().st_mode & 0o777) == '0o700'

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('a cached world was built again')

    monkeypatch.setattr('konstruct.engine.build.Builder.run', refuse)
    again = timeline(shop, since='v0')
    assert again.to_dict() == first.to_dict()
    # Nothing is kept in the repository.
    assert not (shop / 'konstruct-out').exists()


def test_a_cache_that_is_not_private_is_not_used(
    shop: Path, private_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_cache.mkdir(parents=True)
    private_cache.chmod(0o777)
    result = timeline(shop, since='HEAD~1')
    assert f'{private_cache} is not private to you, so no world is cached' in result.notes
    assert list(private_cache.iterdir()) == []
    private_cache.chmod(0o700)
    # A cached world that someone else could have written is built again, not read.
    timeline(shop, since='HEAD~1')
    for world in private_cache.glob('world-*.json'):
        world.write_text('{"format_version": 1, "world": {"metrics": {"Planted": {}}}}')
        world.chmod(0o666)
    planted = timeline(shop, since='HEAD~1')
    assert all(c.name != 'Planted' for _, c in planted.changes())
    # In CI there is no cache.
    monkeypatch.setenv('CI', 'true')
    for world in private_cache.glob('*.json'):
        world.unlink()
    timeline(shop, since='HEAD~1')
    assert list(private_cache.glob('*.json')) == []
    # Without the cache, nothing is written either.
    monkeypatch.delenv('CI')
    timeline(shop, since='HEAD~1', cache=False)
    assert list(private_cache.glob('*.json')) == []


def test_two_projects_in_one_repository_keep_apart(tmp_path: Path) -> None:
    root = tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    for name, measure in (('a', 'Revenue'), ('b', 'Cost')):
        _files(
            root,
            **{
                f'{name}/ltv.sql': 'create table main.ltv as select customer_id, sum(amount) as total from raw.orders '
                'group by customer_id\n',
                f'{name}/definitions.yml': f'terms:\n  - name: {measure}\n    kind: measure\n    columns: [main.ltv.total]\n',
            },
        )
    commit(root, 'Add both', 'Ada', '2026-01-01T10:00:00+00:00')
    assert [c.name for _, c in timeline(root / 'a').changes('metric')] == ['Revenue']
    assert [c.name for _, c in timeline(root / 'b').changes('metric')] == ['Cost']


def test_a_commit_that_does_not_build_does_not_hide_the_next(shop: Path) -> None:
    (shop / 'konstruct.toml').write_text('this is not toml = = =\n')
    commit(shop, 'Break the configuration', 'Linus', '2026-04-01T10:00:00+00:00')
    (shop / 'konstruct.toml').unlink()
    _files(
        shop,
        **{
            'definitions.yml': _definitions(filters=True, name='Customer lifetime value', status='approved'),
            'models/orders.sql': ORDERS.replace('amount,', 'amount * 2 as amount,'),
        },
    )
    _manifest(shop)
    commit(shop, 'Mend it, require the filter again and double revenue', 'Linus', '2026-04-02T10:00:00+00:00')
    result = timeline(shop, since='HEAD~2')
    assert [c.subject for c in result.unknown] == ['Break the configuration']
    (entry,) = result.entries
    assert entry.commit.subject == 'Mend it, require the filter again and double revenue'
    assert entry.compared_with is not None and entry.compared_with.subject == 'Reformat customers'
    # Every other fact of the models is as it was: the commit after one that did not build is
    # built from all of its own files.
    assert [c.headline() for c in entry.changes] == [
        "property 'customer.lifetime_value': formulas changed",
        "property 'order.amount': formulas changed",
        "metric 'Customer lifetime value': formula changed",
        "metric 'Revenue': filters changed",
        "metric 'order amount': formula changed",
    ]
    text = str(result)
    assert 'compared with ' in text and 'note: the world could not be built at ' in text
    assert 'Compared with `' in result.markdown()


def test_formula_and_row_changes_point_at_their_effect_on_todays_data(tmp_path: Path) -> None:
    project = make_sql_shop(tmp_path)
    result = timeline(tmp_path, graph=project.graph)
    assert [(e.commit.subject, [c.headline() for c in e.changes]) for e in result.entries] == [
        ('Add lifetime value', ["metric 'Lifetime value': added"]),
        ('Leave void orders out', ["metric 'Lifetime value': rows changed"]),
        ('Count each order twice', ["metric 'Lifetime value': formula changed"]),
        ('Approve lifetime value again', ["metric 'Lifetime value': approved changed"]),
    ]
    rows = result.entries[1].changes[0]
    assert rows.english == ["now: rows are kept only where [status] has a value other than 'void'"]
    assert rows.to_dict()['effect'] is None  # the timeline reads git and files only
    effects(result, project.graph, tmp_path, tmp_path / 'shop.duckdb')
    formula = result.entries[2].changes[0]
    assert formula.effect is not None and formula.effect_column == 'ltv.total'
    assert formula.effect.columns[0].total_before == 8729 and formula.effect.columns[0].total_after == 17458
    assert rows.effect is not None and rows.effect_column == 'ltv'
    assert result.warehouse is not None and result.warehouse.endswith('shop.duckdb')
    twice, void = _sha(tmp_path, 'HEAD~3'), _sha(tmp_path, 'HEAD~4')
    approved = _sha(tmp_path, 'HEAD~2')
    notes = result.release_notes()
    assert notes[1] == (
        f'Lifetime value: the formula changed in {twice} (Linus); it was the sum of [amount]; it is now the '
        f'sum of ([amount] times 2); approved in a commit by Grace on 2026-03-05 ({approved}); '
        "on today's data: 20 rows in both; the sum of total rises from 8,729 to 17,458 (+100.0%); "
        '20 values of total only before and 20 only after'
    )
    assert notes[2].startswith(
        f'Lifetime value: the rows it is computed over changed in {void} (Linus); now: rows are kept '
        "only where [status] has a value other than 'void'; approved in a commit by Grace"
    )
    assert 'the sum of total falls from 10,100 to 8,729 (-13.6%)' in notes[2]
    assert notes[0] == f'Lifetime value: approved in {approved} (Grace)'
    assert "      - on today's data: 20 rows in both" in str(result)
    assert "  - on today's data: 20 rows in both" in result.markdown()


def test_effects_open_no_warehouse_without_a_formula_change(shop: Path) -> None:
    result = timeline(shop, since='v0')
    assert effects(result, Graph(), shop, shop / 'no-such.duckdb') is result
    assert result.warehouse is None


def test_a_project_in_a_directory_added_later(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'README').write_text('a repository\n')
    commit(tmp_path, 'Start', 'Ada', '2026-01-01T10:00:00+00:00')
    project = tmp_path / 'warehouse'
    project.mkdir()
    (project / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total_amount from raw.orders '
        'group by customer_id\n'
    )
    (project / 'definitions.yml').write_text(
        'terms:\n  - name: Lifetime value\n    kind: measure\n    columns: [main.ltv.total_amount]\n'
    )
    commit(tmp_path, 'Add the warehouse', 'Ada', '2026-01-02T10:00:00+00:00')
    (tmp_path / 'README').write_text('a repository, changed\n')
    commit(tmp_path, 'Change the readme', 'Ada', '2026-01-03T10:00:00+00:00')
    result = timeline(project)
    assert result.commits == 1 and result.start is not None and result.start.subject == 'Start'
    (entry,) = result.entries
    assert entry.commit.subject == 'Add the warehouse'
    assert [(c.headline(), c.english) for c in entry.changes] == [
        ("metric 'Lifetime value': added", ['a new metric, trusted as declared: it is the sum of [amount]'])
    ]


def test_the_example_project_with_its_dbt_manifest(jaffle_built_graph: Path, tmp_path: Path) -> None:
    """jaffle_shop with its real manifest: a project macro changed at a commit is a change, as the
    commit's macros render its models, and a model whose file differs from today's has the
    columns its SQL gives rather than today's catalog."""
    from tests.conftest import run_dbt

    root = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root, ignore=shutil.ignore_patterns('konstruct-out'))
    (root / '.gitignore').write_text('target/\n*.duckdb\n*.wal\nlogs/\n')
    payments = root / 'models' / 'staging' / 'stg_payments.sql'
    payments.write_text(
        payments.read_text().replace('amount / 100 as amount', "{{ to_dollars('amount') }} as amount")
    )
    _files(
        root, **{'macros/to_dollars.sql': '{% macro to_dollars(column) %}{{ column }} / 100{% endmacro %}\n'}
    )
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the example', 'Ada', '2026-01-05T10:00:00+00:00')
    _files(
        root, **{'macros/to_dollars.sql': '{% macro to_dollars(column) %}{{ column }} / 1000{% endmacro %}\n'}
    )
    commit(root, 'Count in thousands', 'Sam', '2026-02-01T10:00:00+00:00')
    customers = root / 'models' / 'customers.sql'
    customers.write_text(
        customers.read_text().replace('first_name,', 'first_name,\n        last_name as surname,', 1)
    )
    commit(root, 'Give customers a surname', 'Ada', '2026-03-01T10:00:00+00:00')
    run_dbt(root, 'parse')  # the manifest as it is after the last commit, as a build would leave it
    result = timeline(root)
    assert result.unknown == [] and result.commits == 3
    first, macro, surname = result.entries
    assert {'Customer lifetime value', 'Order amount'} <= {
        c.name for c in first.changes if c.kind == 'metric'
    }
    assert {c.name for c in first.changes if c.kind == 'entity'} >= {'customer', 'order'}
    changed = {c.name: c for c in macro.changes if c.kind == 'metric'}
    assert changed['Customer lifetime value'].fields['formula'] == (
        'SUM(raw_payments.amount / 100)',
        'SUM(raw_payments.amount / 1000)',
    )
    assert [c.headline() for c in surname.changes] == ["property 'customer.surname': added"]
    assert any("read from their SQL, not dbt's catalog: customers" in n for n in result.notes)
    assert not any('lineage' in n for n in result.notes)


@st.composite
def worlds(draw: st.DrawFn) -> World:
    names = st.sampled_from(['a', 'b', 'c', 'd'])
    value = st.one_of(
        st.none(), st.sampled_from(['x', 'y']), st.lists(st.sampled_from(['p', 'q']), max_size=2)
    )
    facts = st.dictionaries(st.sampled_from(['formula', 'status', 'evidence']), value, max_size=3)

    def keyed(kind: str) -> dict[str, dict[str, Any]]:
        found = draw(st.dictionaries(names, facts, max_size=4))
        signature = 'columns' if kind == 'metric' else 'keys'
        for name, item in found.items():
            if kind in ('metric', 'entity') and draw(st.booleans()):
                item[signature] = draw(
                    st.lists(st.sampled_from(['t.k', 't.j']), min_size=1, max_size=2, unique=True)
                )
            if kind == 'property' and draw(st.booleans()):
                item['entity'] = 'e'
                item['formulas'] = [{'column': 't.c', 'formula': draw(st.sampled_from(['a', 'b']))}]  # type: ignore[list-item]
            if kind in _NAMED:
                item['name'] = name
        return found

    return World(
        entities=keyed('entity'),
        relationships=keyed('relationship'),
        properties=keyed('property'),
        metrics=keyed('metric'),
    )


_NAMED = ('metric', 'property')


def _shape(changes: list[WorldChange]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            c.kind,
            c.name,
            c.status,
            c.renamed_from,
            c.before == c.after,
            sorted(c.fields.items(), key=str),
            str(c.before),
            str(c.after),
        )
        for c in changes
    )


@settings(max_examples=300, deadline=None)
@given(worlds(), worlds())
def test_a_diff_mirrors_the_diff_the_other_way(a: World, b: World) -> None:
    assert world_diff(a, a) == [] and world_diff(b, b) == []
    forward, backward = world_diff(a, b), world_diff(b, a)
    assert _shape([c.mirrored() for c in forward]) == _shape(backward)
    # Every difference is accounted for: applying the changes to a gives b's names.
    for kind in KINDS:
        names = set(a.of(kind))
        for change in forward:
            if change.kind != kind:
                continue
            if change.status in ('removed', 'renamed'):
                names.discard(change.renamed_from or change.name)
            if change.status in ('added', 'renamed'):
                names.add(change.name)
        assert names == set(b.of(kind))
    assert World.from_dict(json.loads(json.dumps(a.to_dict()))) == a


def _project(root: Path, config: str = '', **files: str) -> dbt.DbtProject:
    _files(root, **{'dbt_project.yml': PROJECT + config, **files})
    found = dbt.read_project(root)
    assert found is not None
    return found


def _model(name: str, kind: str = 'model', **extra: Any) -> dict[str, Any]:
    folder = {'model': 'models', 'snapshot': 'snapshots', 'seed': 'seeds'}[kind]
    return {
        'unique_id': f'{kind}.shop.{name}',
        'resource_type': kind,
        'name': name,
        'alias': name,
        'package_name': 'shop',
        'database': 'wh',
        'schema': 'main',
        'relation_name': f'"wh"."main"."{name}"',
        'original_file_path': f'{folder}/{name}.{"csv" if kind == "seed" else "sql"}',
        'raw_code': 'select 1',
        'compiled_code': 'select 1',
        'config': {'materialized': 'table'},
        'depends_on': {'nodes': [], 'macros': ['macro.shop.cents']},
        **extra,
    }


SNAPSHOT = """{% snapshot orders_snapshot %}
{{ config(unique_key='order_id', strategy='check') }}
select * from {{ ref('orders') }}
{% endsnapshot %}
"""


def test_the_manifest_at_a_commit_follows_its_files(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        'model-paths: [transform]\nsnapshot-paths: [history]\ntest-paths: [checks]\nseed-paths: [data]\n',
        **{
            'transform/orders.sql': "{{ config(materialized='incremental', unique_key='order_id') }}\n"
            "select 2 as order_id from {{ source('raw', 'orders') }}\n",
            'transform/same.sql': 'select 1\n',
            'transform/staging/payments.sql': "select * from {{ ref('orders') }} join {{ ref('shop', 'same') }}",
            'history/orders_snapshot.sql': SNAPSHOT,
            'history/snapshots.yml': 'snapshots: [{name: yaml_snapshot, relation: "ref(\'orders\')"}]\n',
            'checks/no_negative.sql': 'select 1 where false',
            'data/codes.csv': 'code,label\na,b\n',
            'transform/schema.yml': """version: 2
models:
  - name: orders
    description: One row per order.
    constraints: [{type: primary_key, columns: [order_id]}]
    config: {tags: [daily]}
    data_tests:
      - dbt_utils.unique_combination_of_columns:
          arguments: {combination_of_columns: [order_id]}
    columns:
      - name: order_id
        description: The order.
        tests: [{unique: {config: {severity: warn}}}, {name: order_key, test_name: not_null}]
      - name: payment_id
        data_tests:
          - relationships: {to: "ref('payments')", field: payment_id}
          - relationships: {to: "source('raw', 'payments')", field: id}
          - relationships: {to: "ref('missing')", field: id}
sources:
  - name: raw
    schema: landing
    tables:
      - name: orders
        columns: [{name: id, data_tests: [unique]}]
      - name: refunds
        identifier: refund_events
""",
            'transform/broken.yml': 'models: [',
            'transform/list.yml': '- just a list',
        },
    )
    (tmp_path / 'transform' / 'secret.sql').symlink_to(tmp_path / 'checks' / 'no_negative.sql')
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {
            'model.shop.orders': _model(
                'orders', columns={'old': {'name': 'old'}}, original_file_path='transform/orders.sql'
            ),
            'model.shop.same': _model('same', raw_code='select 1', original_file_path='transform/same.sql'),
            'model.shop.gone': _model('gone'),
            'model.other.elsewhere': _model('elsewhere', package_name='other'),
            'snapshot.shop.orders_snapshot': _model(
                'orders_snapshot', 'snapshot', original_file_path='history/orders_snapshot.sql'
            ),
            'snapshot.shop.yaml_snapshot': _model(
                'yaml_snapshot',
                'snapshot',
                original_file_path='history/snapshots.yml',
                raw_code='select * from wh.main.orders',
            ),
            'seed.shop.retired': _model('retired', 'seed'),
            'test.shop.unique_orders_old.1': {
                'resource_type': 'test',
                'package_name': 'shop',
                'test_metadata': {'name': 'unique'},
                'original_file_path': 'transform/schema.yml',
            },
            'test.shop.no_negative': {
                'resource_type': 'test',
                'original_file_path': 'checks/no_negative.sql',
                'package_name': 'shop',
            },
            'test.shop.removed_singular': {
                'resource_type': 'test',
                'original_file_path': 'checks/removed.sql',
                'package_name': 'shop',
            },
        },
        'sources': {
            'source.shop.raw.orders': {
                'source_name': 'raw',
                'name': 'orders',
                'package_name': 'shop',
                'relation_name': '"wh"."raw"."orders"',
            },
            'source.shop.raw.gone': {'source_name': 'raw', 'name': 'gone', 'package_name': 'shop'},
            'source.shop.raw.payments': {'source_name': 'raw', 'name': 'payments', 'package_name': 'other'},
        },
    }
    catalog: dict[str, Any] = {
        'nodes': {k: {'columns': {}} for k in ('model.shop.orders', 'model.shop.same', 'model.shop.gone')}
    }
    before = copy.deepcopy(manifest)
    at = manifest_at(manifest, catalog, project)
    nodes = at.manifest['nodes']
    assert manifest == before  # neither argument is changed
    assert sorted(nodes) == [
        'model.other.elsewhere',
        'model.shop.orders',
        'model.shop.payments',
        'model.shop.same',
        'seed.shop.codes',
        'snapshot.shop.orders_snapshot',
        'snapshot.shop.yaml_snapshot',
        'test.shop.no_negative',
        *sorted(k for k in nodes if k.startswith('test.shop.') and k != 'test.shop.no_negative'),
    ]
    orders = nodes['model.shop.orders']
    assert orders['raw_code'].startswith('{{ config(') and 'compiled_code' not in orders
    assert orders['config'] == {'materialized': 'incremental', 'unique_key': 'order_id', 'tags': ['daily']}
    assert orders['depends_on'] == {'nodes': ['source.shop.raw.orders'], 'macros': ['macro.shop.cents']}
    assert orders['description'] == 'One row per order.'
    assert orders['constraints'] == [{'type': 'primary_key', 'columns': ['order_id']}]
    assert orders['columns'] == {
        'order_id': {'name': 'order_id', 'description': 'The order.'},
        'payment_id': {'name': 'payment_id'},
    }
    assert nodes['model.shop.same']['raw_code'] == 'select 1'
    payments = nodes['model.shop.payments']
    assert payments['relation_name'] == '"wh"."main"."payments"' and payments['fqn'] == [
        'shop',
        'staging',
        'payments',
    ]
    assert payments['depends_on']['nodes'] == ['model.shop.orders', 'model.shop.same']
    snapshot = nodes['snapshot.shop.orders_snapshot']
    assert snapshot['raw_code'].strip().startswith("{{ config(unique_key='order_id'")
    assert '{% snapshot' not in snapshot['raw_code'] and snapshot['config']['unique_key'] == 'order_id'
    # A YAML snapshot has no file of SQL to render, so it keeps the SQL dbt compiled.
    assert nodes['snapshot.shop.yaml_snapshot']['raw_code'] == 'select * from wh.main.orders'
    assert nodes['snapshot.shop.yaml_snapshot']['compiled_code'] == 'select 1'
    assert nodes['seed.shop.codes']['columns'] == {'code': {'name': 'code'}, 'label': {'name': 'label'}}
    sources = at.manifest['sources']
    assert sorted(sources) == [
        'source.shop.raw.orders',
        'source.shop.raw.payments',
        'source.shop.raw.refunds',
    ]
    assert sources['source.shop.raw.refunds']['relation_name'] == '"wh"."landing"."refund_events"'
    assert sources['source.shop.raw.orders']['columns'] == {'id': {'name': 'id'}}
    tests = sorted(
        (i['test_metadata']['name'], i['name'], i['column_name'], i['depends_on']['nodes'], i['config'])
        for i in nodes.values()
        if i.get('resource_type') == 'test' and i.get('test_metadata')
    )
    assert tests == [
        ('not_null', 'order_key', 'order_id', ['model.shop.orders'], {}),
        ('relationships', 'relationships_orders_payment_id', 'payment_id', ['model.shop.orders'], {}),
        (
            'relationships',
            'relationships_orders_payment_id',
            'payment_id',
            ['model.shop.orders', 'model.shop.payments'],
            {},
        ),
        (
            'relationships',
            'relationships_orders_payment_id',
            'payment_id',
            ['model.shop.orders', 'source.shop.raw.payments'],
            {},
        ),
        ('unique', 'unique_orders_order_id', 'order_id', ['model.shop.orders'], {'severity': 'warn'}),
        ('unique', 'unique_raw_orders_id', 'id', ['source.shop.raw.orders'], {}),
        (
            'unique_combination_of_columns',
            'unique_combination_of_columns_orders',
            None,
            ['model.shop.orders'],
            {},
        ),
    ]
    combination = next(
        i for i in nodes.values() if i.get('test_metadata', {}).get('name') == 'unique_combination_of_columns'
    )
    assert combination['test_metadata']['kwargs'] == {'combination_of_columns': ['order_id']}
    assert combination['test_metadata']['namespace'] == 'dbt_utils'
    # Only a changed model's catalog entry is left out; a file through a link is not read.
    assert at.catalog == {'nodes': {'model.shop.same': {'columns': {}}}}
    assert at.uncatalogued == ['orders']
    assert 'model.shop.secret' not in nodes
    # With no model to take a database and schema from, no new model can be named.
    empty = manifest_at({'nodes': {}}, None, project).manifest['nodes']
    assert [i['resource_type'] for i in empty.values()] == ['test']


def test_a_renamed_project_keeps_todays_package(tmp_path: Path) -> None:
    project = _project(tmp_path / 'p', **{'models/orders.sql': 'select 3 as id'})
    project.name = 'shop_before_the_rename'
    manifest = {'nodes': {'model.shop.orders': _model('orders')}, 'sources': {}}
    at = manifest_at(manifest, None, project)
    assert at.manifest['nodes']['model.shop.orders']['raw_code'] == 'select 3 as id'


def test_only_models_rendered_without_their_package_macros_are_named(tmp_path: Path) -> None:
    """The macros of installed packages are not in git: a model dbt's compiled SQL is kept for has
    them, and one rendered again at a commit does not."""
    from konstruct.inputs.dbt_commit import compile_inputs, installed_packages

    project = _project(tmp_path, **{'models/a.sql': 'select 10', 'models/b.sql': 'select 2'})
    _files(tmp_path, **{'dbt_packages/dbt_utils/macros/x.sql': ''})
    assert installed_packages(project) == {'dbt_utils'}
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {
            'model.shop.a': _model('a', depends_on={'macros': ['macro.dbt_utils.star']}),
            'model.shop.b': _model('b', raw_code='select 2', depends_on={'macros': ['macro.dbt_utils.star']}),
        },
    }
    today = compile_inputs(project)
    kept = manifest_at(manifest, None, project, compiled_from=today, packages={'dbt_utils'})
    assert kept.package_macros == ['a']  # b keeps the SQL dbt compiled; a changed and is rendered
    rendered = manifest_at(manifest, None, project, packages={'dbt_utils'})
    assert rendered.package_macros == ['a', 'b']
    assert manifest_at(manifest, None, project).package_macros == []
    assert installed_packages(_project(tmp_path / 'none')) == set()


def test_the_timeline_of_a_generated_project(tmp_path: Path) -> None:
    """Three commits on the generated project (tests/test_scale.py), each model with a primary key:
    a formula changed, a key dropped and a measure defined. Built once, then read from the cache."""
    import time

    from tests.test_scale import MODELS, make_project

    make_project(tmp_path, MODELS)
    keys = ''.join(
        f'  - name: m{i:03}\n    constraints: [{{type: primary_key, columns: [c00]}}]\n'
        for i in range(MODELS)
    )
    _files(
        tmp_path,
        **{'models/schema.yml': f'version: 2\nmodels:\n{keys}', '.gitignore': 'target/\nkonstruct-out/\n'},
    )
    git(tmp_path, 'init', '-q', '-b', 'main')
    commit(tmp_path, 'Generate', 'Ada', '2026-01-01T10:00:00+00:00')
    middle, last = f'm{MODELS * 3 // 4:03}', f'm{MODELS - 1:03}'
    sql = (tmp_path / 'models' / f'{middle}.sql').read_text()
    _files(tmp_path, **{f'models/{middle}.sql': sql.replace('b.c01 * 2 as c01', 'b.c01 * 3 as c01')})
    commit(tmp_path, 'Triple c01', 'Sam', '2026-01-02T10:00:00+00:00')
    dropped = f'  - name: {last}\n    constraints: [{{type: primary_key, columns: [c00]}}]\n'
    _files(tmp_path, **{'models/schema.yml': f'version: 2\nmodels:\n{keys}'.replace(dropped, '')})
    commit(tmp_path, 'Drop a key', 'Ada', '2026-01-03T10:00:00+00:00')
    term = f'terms:\n  - name: Middle value\n    kind: measure\n    columns: [{middle}.c01]\n'
    _files(tmp_path, **{'definitions.yml': term})
    commit(tmp_path, 'Define the middle value', 'Priya', '2026-01-04T10:00:00+00:00')
    started = time.perf_counter()
    result = timeline(tmp_path, since='HEAD~3')
    cold = time.perf_counter() - started
    assert [(e.commit.subject, e.changes[0].headline()) for e in result.entries] == [
        ('Triple c01', f"property '{middle}.c01': formulas changed"),
        ('Drop a key', f"entity '{last}': removed"),
        ('Define the middle value', f"property '{middle}.c01': terms changed"),
    ]
    tripled = result.entries[0].changes[0]
    assert [line.endswith('times 3') for line in tripled.english] == [True]
    assert [c.name for c in result.entries[2].changes if c.kind == 'metric'] == ['Middle value']
    started = time.perf_counter()
    assert timeline(tmp_path, since='HEAD~3').to_dict() == result.to_dict()
    cached = time.perf_counter() - started
    assert cached < cold / 3, (cold, cached)
    print(f'\n{MODELS} models, 4 worlds: {cold:.1f} s, then {cached:.2f} s from the cache')


def test_an_old_commit_cannot_write_outside_the_temporary_tree(shop: Path, tmp_path: Path) -> None:
    """A commit's dbt_project.yml decides where its target and models are; one that names a
    directory outside the project is not read, and nothing is written there."""
    escape = tmp_path / 'escape'
    for target in ('../../../escape', str(escape)):
        _files(shop, **{'dbt_project.yml': PROJECT + f'target-path: {target}\n'})
        commit(shop, f'Point the target at {target}', 'Mallory', '2026-04-01T10:00:00+00:00')
    _files(
        shop,
        **{
            'dbt_project.yml': PROJECT
            + 'model-paths: [models, ../../../../../../..'
            + str(tmp_path / 'secret')
            + ']\n'
        },
    )
    _files(tmp_path, **{'secret/leak.sql': 'select 1 as secret_column\n'})
    commit(shop, 'Read models from elsewhere', 'Mallory', '2026-04-02T10:00:00+00:00')
    result = timeline(shop, since='HEAD~3')
    assert not escape.exists() and not (shop.parents[2] / 'escape').exists()
    assert not any('secret' in json.dumps(c.to_dict()) for _, c in result.changes())
    assert sum('names paths outside the project' in n for n in result.notes) == 3


def test_a_commits_gitattributes_cannot_hide_its_change(shop: Path) -> None:
    """git archive would leave out a file .gitattributes marks export-ignore, so the change would
    be credited to whoever came next; the commit's own blobs are read instead."""
    _files(
        shop,
        **{
            '.gitattributes': 'definitions.yml export-ignore\nmodels/*.sql export-subst\n',
            'definitions.yml': _definitions(filters=True, name='Customer lifetime value', status='approved'),
        },
    )
    commit(shop, 'Require the filter again', 'Mallory', '2026-04-01T10:00:00+00:00')
    (entry,) = timeline(shop, since='HEAD~1').entries
    assert entry.commit.author == 'Mallory'
    assert [c.headline() for c in entry.changes] == ["metric 'Revenue': filters changed"]


def test_approvals_follow_ancestry_not_the_order_of_the_list(tmp_path: Path) -> None:
    """Sam doubles the formula on one branch while Priya approves the term on another: Priya never
    saw the doubled formula, so her approval is not credited to it, whatever the dates say."""
    root = tmp_path / 'shop'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    term = 'terms:\n  - name: Lifetime value\n    kind: measure\n    status: {status}\n    columns: [main.ltv.total]\n{approved}'
    ltv = 'create table main.ltv as select customer_id, sum(amount{times}) as total from raw.orders group by customer_id\n'
    _files(
        root, **{'ltv.sql': ltv.format(times=''), 'definitions.yml': term.format(status='draft', approved='')}
    )
    commit(root, 'Add lifetime value', 'Ada', '2026-01-01T10:00:00+00:00')
    git(root, 'checkout', '-q', '-b', 'approval')
    _files(root, **{'definitions.yml': term.format(status='approved', approved='    approved: 2026-02-01\n')})
    commit(root, 'Approve lifetime value', 'Priya', '2026-02-01T10:00:00+00:00')
    git(root, 'checkout', '-q', 'main')
    _files(root, **{'ltv.sql': ltv.format(times=' * 2')})
    commit(root, 'Double lifetime value', 'Sam', '2026-03-01T10:00:00+00:00')
    git(root, 'merge', '-q', '--no-edit', 'approval', author='Ada', committed='2026-04-01T10:00:00+00:00')
    result = timeline(root)
    changes = {c.name + ': ' + commit.subject: c for commit, c in result.changes('metric')}
    doubled = changes['Lifetime value: Double lifetime value']
    assert doubled.approval is None
    assert next(n for n in result.release_notes() if 'formula changed' in n).endswith(
        'draft, not approved since'
    )
    added = changes['Lifetime value: Add lifetime value']
    assert added.approval is not None and added.approval.author == 'Priya'


def test_commits_are_taken_in_the_order_of_history(shop: Path) -> None:
    """A rebased commit keeps its author's earlier date; it still comes after its parent."""
    _files(
        shop,
        **{'definitions.yml': _definitions(filters=True, name='Customer lifetime value', status='approved')},
    )
    commit(
        shop,
        'Written first, landed last',
        'Sam',
        '2025-12-01T10:00:00+00:00',
        committed='2026-05-01T10:00:00+00:00',
    )
    result = timeline(shop, since='v0')
    assert result.entries[-1].commit.subject == 'Written first, landed last'
    last = result.entries[-1].commit
    assert last.authored.startswith('2025-12-01') and last.date.startswith('2026-05-01')
    assert '2025-12-01' in str(result) and f'### 2025-12-01 `{last.short}` Sam: ' in result.markdown()


def test_co_authors_are_credited(shop: Path) -> None:
    _files(
        shop,
        **{'definitions.yml': _definitions(filters=True, name='Customer lifetime value', status='approved')},
    )
    commit(
        shop,
        'Pair on the filter\n\nCo-authored-by: Ada Lovelace <ada@example.com>',
        'Sam',
        '2026-04-01T10:00:00+00:00',
    )
    result = timeline(shop, since='HEAD~1')
    (entry,) = result.entries
    assert entry.commit.co_authors == ['Ada Lovelace'] and entry.commit.by == 'Sam with Ada Lovelace'
    assert result.release_notes()[0].endswith('(Sam with Ada Lovelace); agreed, not approved since')
    assert result.to_dict()['entries'][0]['commit']['co_authors'] == ['Ada Lovelace']


def test_moving_definitions_or_adding_a_pass_through_model_changes_nothing(shop: Path) -> None:
    """Where a definition is written, and a staging model that only passes columns on, are not
    the business's meaning."""
    (shop / 'business').mkdir()
    (shop / 'definitions.yml').rename(shop / 'business' / 'definitions.yml')
    commit(shop, 'Move the definitions', 'Ada', '2026-04-01T10:00:00+00:00')
    _files(
        shop,
        **{
            'models/stg_orders.sql': 'select id, customer_id, amount, status from raw.orders\n',
            'models/orders.sql': ORDERS.replace('from raw.orders', "from {{ ref('stg_orders') }}"),
        },
    )
    commit(shop, 'Stage the orders', 'Ada', '2026-04-02T10:00:00+00:00')
    result = timeline(shop, since='HEAD~2')
    assert result.commits == 2
    assert [(e.commit.subject, [c.headline() for c in e.changes]) for e in result.entries] == []


def test_renames_are_told_by_what_stays_the_same(shop: Path) -> None:
    """A property renamed keeps its formula, and the rows a metric is computed over, composed down
    to source, stay the same; a metric deleted and another added on its column, with another
    definition, is not a rename."""
    _files(
        shop,
        **{
            'models/orders.sql': ORDERS.replace('status from', 'status as order_status from'),
            'models/customer_value.sql': VALUE.replace('where status', 'where order_status'),
        },
    )
    commit(shop, 'Call it the order status', 'Ada', '2026-04-01T10:00:00+00:00')
    definitions = _definitions(filters=False, name='Customer lifetime value', status='approved')
    _files(
        shop,
        **{
            'definitions.yml': definitions.replace(
                '  - name: Revenue\n', '  - name: Average order value\n'
            ).replace('What customers paid for their orders.', 'What an order is worth on average.')
        },
    )
    commit(shop, 'Replace revenue', 'Sam', '2026-04-02T10:00:00+00:00')
    renamed, replaced = timeline(shop, since='HEAD~2').entries
    assert [c.headline() for c in renamed.changes] == [
        "property 'order.order_status': renamed from 'order.status'"
    ]
    assert [c.headline() for c in replaced.changes] == [
        "metric 'Average order value': added",
        "metric 'Revenue': removed",
    ]


def test_the_limit_note_only_when_there_are_more_commits(shop: Path) -> None:
    assert timeline(shop, since='v0', limit=6).notes == []
    assert timeline(shop, since='v0', limit=5).notes == [
        'only the last 5 commits were read; raise the limit to read more'
    ]


def test_effects_read_the_whole_history_when_the_range_starts_at_the_first_commit(tmp_path: Path) -> None:
    project = make_sql_shop(tmp_path)
    git(tmp_path, 'tag', 'v2', 'HEAD~1')  # a later tag than the changes, where release notes would start
    result = timeline(tmp_path, graph=project.graph)
    assert result.start is None
    effects(result, project.graph, tmp_path, tmp_path / 'shop.duckdb')
    changed = [c for _, c in result.changes('metric') if {'formula', 'rows'} & set(c.fields)]
    assert len(changed) == 2 and all(c.effect is not None for c in changed)


def test_verify_results_hold_at_every_commit_and_say_so(shop: Path) -> None:
    graph = Graph()
    graph.meta['integrity'] = {'keys': {}, 'relationships': {}}
    result = timeline(shop, since='HEAD~1', graph=graph)
    assert result.notes == [
        'what konstruct verify found of keys and relationships in the data now holds at every commit'
    ]


def test_the_command_line(shop: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from konstruct.interfaces.cli import main

    root = str(shop)
    assert main(['--no-color', '-q', '-C', root, 'timeline', '--since', 'v0']) == 0
    out = capsys.readouterr().out
    assert out.startswith('Timeline since v0 (HEAD)\n11 changes to the business in 5 of 6 commits since v0.')
    assert main(['-q', '-C', root, 'timeline', '--since', 'v0', '--format', 'markdown']) == 0
    assert capsys.readouterr().out.startswith('## Timeline since v0')
    assert main(['-q', '-C', root, 'timeline', '--file', 'models/schema.yml', '-n', '1', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['paths'] == ['models/schema.yml'] and data['commits'] == 1
    assert main(['-q', '-C', root, 'timeline', '--format', 'json', '--until', 'HEAD~4']) == 0
    assert json.loads(capsys.readouterr().out)['until'] == 'HEAD~4'
    assert main(['-q', '-C', root, 'timeline', '--since', 'no-such-ref']) == 1
    assert 'no-such-ref' in capsys.readouterr().err
    # --timeline is how --effect reads the changes, and nothing without it.
    assert main(['-q', '-C', root, 'verify', '--timeline']) == 2
    assert '--timeline picks how --effect reads the changes' in capsys.readouterr().err


def test_verify_effect_with_the_timeline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from konstruct.interfaces.cli import main

    make_sql_shop(tmp_path)
    root = str(tmp_path)
    assert main(['-q', '-C', root, 'verify', '--effect', '--timeline', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['warehouse'] is not None
    assert any("on today's data" in note for note in data['release_notes'])
    assert (
        main(['--no-color', '-q', '-C', root, 'verify', '--effect', '--timeline', '--format', 'markdown'])
        == 0
    )
    assert "  - on today's data:" in capsys.readouterr().out


def test_the_timeline_with_no_formula_change_opens_no_warehouse(shop: Path) -> None:
    from konstruct import Project

    project = Project(shop)
    project.build(run_dbt=False, warehouses=False)
    result = project.timeline_effect('v0', until='HEAD~2', warehouse=shop / 'no-such.duckdb')
    assert result.warehouse is None and len(result.entries) == 4
    with pytest.raises(QueryError, match='at least 1'):
        project.timeline(limit=0)
    with pytest.raises(QueryError, match='no-such-ref'):
        project.timeline('no-such-ref')


def test_the_mcp_tool(shop: Path) -> None:
    import anyio
    from mcp import Client

    from konstruct import Project
    from konstruct.interfaces.mcp_server import UNTRUSTED_CONTENT, build_server

    Project(shop).build(run_dbt=False, warehouses=False)

    async def run() -> dict[str, Any]:
        async with Client(build_server(shop)) as client:
            result = await client.call_tool('timeline', {'since': 'v0', 'paths': ['definitions.yml']})
            assert not result.is_error
            data: dict[str, Any] = result.structured_content or {}
            return data

    data = anyio.run(run)
    assert data['untrusted_content'] == UNTRUSTED_CONTENT
    assert data['commits'] == 3 and data['paths'] == ['definitions.yml']


def test_metrics_that_share_a_name_get_keys_whatever_the_order() -> None:
    from konstruct.semantics.world_facts import unique_keys

    first = {'source': 'definitions', 'entity': 'order', 'columns': ['a.x']}
    second = {'source': 'definitions', 'entity': 'customer', 'columns': ['b.y']}
    third: dict[str, Any] = {'source': 'looker', 'entity': None, 'columns': []}
    for metrics in ([first, second], [second, first]):
        keys = dict(zip(unique_keys('Revenue', metrics), map(id, metrics), strict=True))
        assert keys == {
            'Revenue (definitions: order; a.x)': id(first),
            'Revenue (definitions: customer; b.y)': id(second),
        }
    assert unique_keys('Revenue', [first]) == ['Revenue']
    assert unique_keys('Revenue', [first, third]) == ['Revenue (definitions)', 'Revenue (looker)']
    assert unique_keys('Revenue', [first, first])[0].endswith('#0')


def test_a_model_unchanged_with_todays_macros_keeps_its_compiled_sql(tmp_path: Path) -> None:
    from konstruct.inputs.dbt_commit import compile_inputs

    project = _project(
        tmp_path,
        **{
            'models/same.sql': 'select 1',
            'models/edited.sql': 'select 2',
            'macros/cents.sql': '{% macro cents(x) %}{{ x }} / 100{% endmacro %}',
        },
    )
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {'model.shop.same': _model('same'), 'model.shop.edited': _model('edited')},
    }
    today = compile_inputs(project)
    assert today['macros/cents.sql'] == '{% macro cents(x) %}{{ x }} / 100{% endmacro %}'
    assert today['dbt_project.yml'] == PROJECT
    nodes = manifest_at(manifest, None, project, compiled_from=today).manifest['nodes']
    assert nodes['model.shop.same']['compiled_code'] == 'select 1'
    assert 'compiled_code' not in nodes['model.shop.edited']
    # Macros that differ from today's render every model again, with the commit's macros.
    other = manifest_at(manifest, None, project, compiled_from={**today, 'macros/cents.sql': 'changed'})
    assert not any('compiled_code' in n for n in other.manifest['nodes'].values())
    other = manifest_at(manifest, None, project, compiled_from={**today, 'packages.yml': 'packages: []'})
    assert not any('compiled_code' in n for n in other.manifest['nodes'].values())


def test_a_build_leaves_the_users_cache_alone_and_the_timeline_reads_no_project_cache(
    shop: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository can commit konstruct-out/cache.json, which a build with --update reads: what
    it holds never reaches the user's private cache, and building a commit's world reads no
    cache in the project at all, only the timeline's own."""
    from konstruct import Project
    from konstruct.engine import build as building

    cache_home = Path(os.environ['XDG_CACHE_HOME'])
    Project(shop).build(update=True, run_dbt=False, warehouses=False)
    assert not (cache_home / 'konstruct' / 'analysis').exists()
    read: list[Path] = []
    real = building.Cache.__init__

    def recorded(self: Any, path: Path, enabled: bool) -> None:
        if enabled:
            read.append(Path(path))
        real(self, path, enabled)

    monkeypatch.setattr(building.Cache, '__init__', recorded)
    timeline(shop, since='HEAD~1')
    assert read and all(not p.resolve().is_relative_to(shop.resolve()) for p in read)
    assert all(p.resolve().is_relative_to((cache_home / 'konstruct' / 'timeline').resolve()) for p in read)


def test_a_formula_digest_depends_on_the_dialect() -> None:
    """The same expression read in another dialect can mean another thing, so it is another
    formula."""
    from konstruct.core.graph import Node
    from konstruct.semantics.world_facts import Formulas

    def column(dialect: str) -> tuple[Formulas, Node]:
        graph = Graph()
        graph.meta['dialect'] = dialect
        table = graph.add_node(Node(id='table.t', kind='table', name='t', relation='t'))
        node = graph.add_column(table, 'x', origin='sql', attrs={'expression': "a || 'b'"})
        return Formulas(graph), node

    duck, mysql = column('duckdb'), column('mysql')
    assert duck[0].digest(duck[1]) != mysql[0].digest(mysql[1])
    assert duck[0].digest(duck[1]) == column('duckdb')[0].digest(column('duckdb')[1])


def test_the_cache_is_bounded_by_bytes_and_worlds_follow_their_analyses(
    shop: Path, private_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest worlds go once the cache passes its size; a world built from analyses since
    removed is built again rather than read."""
    from konstruct.analysis import timeline as module

    timeline(shop, since='v0')
    worlds = sorted(private_cache.glob('world-*.json'), key=lambda p: p.stat().st_mtime)
    assert len(worlds) == 7
    sizes = sum(p.stat().st_size for p in private_cache.iterdir())
    monkeypatch.setattr(module, 'CACHE_BYTES', sizes - worlds[0].stat().st_size - worlds[1].stat().st_size)
    (private_cache / 'world-0.json').write_text('{}')  # one more, so the run prunes
    timeline(shop, since='HEAD~1')
    left = list(private_cache.glob('world-*.json'))
    assert sum(p.stat().st_size for p in private_cache.iterdir()) <= module.CACHE_BYTES
    assert worlds[0] not in left and worlds[-1] in left
    monkeypatch.setattr(module, 'CACHE_BYTES', 1 << 30)
    # The analyses are another generation once their file is gone: no world is read back.
    for analyses in private_cache.glob('analysis-*'):
        analyses.unlink()
    built: list[str] = []
    real = module._Worlds._build

    def counted(self: Any, sha: str) -> Any:
        built.append(sha)
        return real(self, sha)

    monkeypatch.setattr(module._Worlds, '_build', counted)
    timeline(shop, since='HEAD~1')
    assert len(built) == 2


def test_a_package_model_downstream_of_a_changed_ephemeral_model_is_named(tmp_path: Path) -> None:
    """A package's model is not in git, so it keeps the SQL dbt compiled today; where that inlines
    a project's ephemeral model the commit changed, the model is named."""
    from konstruct.inputs.dbt_commit import compile_inputs

    project = _project(tmp_path, **{'models/e.sql': 'select 1 as id'})
    today = compile_inputs(project)
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {
            'model.shop.e': _model('e', raw_code='select 1 as id', config={'materialized': 'ephemeral'}),
            'model.pkg.reads': _model(
                'reads', package_name='pkg', depends_on={'nodes': ['model.shop.e'], 'macros': []}
            ),
            'model.pkg.other': _model('other', package_name='pkg', depends_on={'nodes': [], 'macros': []}),
        },
    }
    assert manifest_at(manifest, None, project, compiled_from=today).package_compiled == []
    _files(tmp_path, **{'models/e.sql': 'select 2 as id'})
    at = manifest_at(manifest, None, project, compiled_from=today)
    assert at.package_compiled == ['reads']
    assert at.manifest['nodes']['model.pkg.reads']['compiled_code'] == 'select 1'


def test_a_seed_whose_header_changed_has_the_commits_columns(tmp_path: Path) -> None:
    """A seed's columns are its CSV header as a CSV reader reads it: quoted, after a byte order
    mark, with a comma inside a quoted name."""
    project = _project(
        tmp_path,
        **{
            'seeds/codes.csv': 'code,name\na,b\n',
            'seeds/same.csv': 'code\na\n',
            'seeds/quoted.csv': '\ufeff"id","amount","city, state"\n1,2,"x, y"\n',
        },
    )
    seeds = ('codes', 'same', 'quoted')
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {f'seed.shop.{n}': _model(n, 'seed') for n in seeds},
    }
    catalog = {
        'nodes': {
            'seed.shop.codes': {'columns': {'code': {'name': 'code'}, 'label': {'name': 'label'}}},
            'seed.shop.same': {'columns': {'CODE': {'name': 'CODE', 'type': 'text'}}},
            'seed.shop.quoted': {
                'columns': {c: {'name': c, 'type': 'text'} for c in ('id', 'amount', 'city, state')}
            },
        }
    }
    at = manifest_at(manifest, catalog, project)
    assert at.manifest['nodes']['seed.shop.codes']['columns'] == {
        'code': {'name': 'code'},
        'name': {'name': 'name'},
    }
    assert list(at.manifest['nodes']['seed.shop.quoted']['columns']) == ['id', 'amount', 'city, state']
    assert at.catalog is not None and sorted(at.catalog['nodes']) == ['seed.shop.quoted', 'seed.shop.same']
    assert at.uncatalogued == ['codes']


def test_a_model_moved_to_another_folder_keeps_its_configuration_and_is_named(tmp_path: Path) -> None:
    """A model the commit holds in another folder than today is the same model: it keeps its
    configuration and relation, and is named, as the folder's configuration in dbt_project.yml is
    not applied to it."""
    project = _project(tmp_path, **{'models/marts/orders.sql': 'select 1 as id'})
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {
            'model.shop.orders': _model(
                'orders',
                original_file_path='models/staging/orders.sql',
                schema='staging',
                relation_name='"wh"."staging"."orders"',
                config={'materialized': 'incremental'},
            ),
            'model.shop.other': _model('other'),
        },
    }
    _files(tmp_path, **{'models/other.sql': 'select 1'})
    at = manifest_at(manifest, None, project)
    orders = at.manifest['nodes']['model.shop.orders']
    assert orders['original_file_path'] == 'models/marts/orders.sql'
    assert (
        orders['relation_name'] == '"wh"."staging"."orders"'
        and orders['config']['materialized'] == 'incremental'
    )
    assert at.moved == ['orders']


def test_a_model_whose_ephemeral_upstream_changed_is_rendered_again(tmp_path: Path) -> None:
    """dbt inlines an ephemeral model into the SQL it compiles for the models that read it, so a
    model whose own file is today's still compiles differently when an upstream model changed."""
    from konstruct.inputs.dbt_commit import compile_inputs

    reads = "select amount from {{ ref('e') }}"
    project = _project(
        tmp_path,
        **{
            'models/e.sql': 'select id, amount from raw.orders',
            'models/m.sql': reads,
            'models/after.sql': "select amount from {{ ref('m') }}",
            'models/alone.sql': 'select 1',
        },
    )
    today = compile_inputs(project)
    ephemeral = {'materialized': 'ephemeral'}
    manifest = {
        'metadata': {'project_name': 'shop'},
        'nodes': {
            'model.shop.e': _model('e', raw_code='select id, amount from raw.orders', config=ephemeral),
            'model.shop.m': _model(
                'm',
                raw_code=reads,
                compiled_code='with __dbt__cte__e as (select id, amount from raw.orders) '
                'select amount from __dbt__cte__e',
                depends_on={'nodes': ['model.shop.e'], 'macros': []},
            ),
            'model.shop.after': _model(
                'after',
                raw_code="select amount from {{ ref('m') }}",
                compiled_code='select amount from "wh"."main"."m"',
                depends_on={'nodes': ['model.shop.m'], 'macros': []},
            ),
            'model.shop.alone': _model('alone'),
        },
    }
    _files(tmp_path, **{'models/e.sql': 'select id, amount * 2 as amount from raw.orders'})
    nodes = manifest_at(manifest, None, project, compiled_from=today).manifest['nodes']
    assert [k for k, n in nodes.items() if 'compiled_code' in n] == ['model.shop.alone']
    # So too where the upstream model is not in the commit at all.
    (tmp_path / 'models' / 'e.sql').unlink()
    nodes = manifest_at(manifest, None, project, compiled_from=today).manifest['nodes']
    assert [k for k, n in nodes.items() if 'compiled_code' in n] == ['model.shop.alone']


VARIED = "select id as order_id, amount * {{ var('rate', 3) }} as amount from raw.orders\n"
KEYED = """version: 2
models:
  - name: orders
    columns:
      - name: order_id
        description: {described}
        data_tests: [unique]
"""


def _varied_shop(root: Path, commits: list[tuple[str, dict[str, str]]], compiled: str) -> Path:
    """A dbt project with one model whose SQL depends on a var, committed as ``commits`` give,
    and today's manifest with ``compiled`` as the SQL dbt compiled for the model."""
    root.mkdir(parents=True)
    git(root, 'init', '-q', '-b', 'main')
    for index, (subject, files) in enumerate(commits):
        _files(root, **files)
        commit(root, subject, 'Ada', f'2026-01-0{index + 1}T10:00:00+00:00')
    model = _model('orders', raw_code=VARIED.strip(), compiled_code=compiled, depends_on={'nodes': []})
    sources: dict[str, Any] = {}
    if (root / 'models' / 'sources.yml').exists():
        sources = {
            'source.shop.raw.orders': {
                'unique_id': 'source.shop.raw.orders',
                'resource_type': 'source',
                'source_name': 'raw',
                'name': 'orders',
                'identifier': 'orders_v2',
                'package_name': 'shop',
                'database': 'wh',
                'schema': 'raw',
                'relation_name': '"wh"."raw"."orders_v2"',
                'original_file_path': 'models/sources.yml',
            }
        }
        model['raw_code'] = (root / 'models' / 'orders.sql').read_text().strip()
    _files(root, **{'.gitignore': 'target/\n'})
    (root / 'target').mkdir()
    (root / 'target' / 'manifest.json').write_text(
        json.dumps(
            {
                'metadata': {'adapter_type': 'duckdb', 'project_name': 'shop'},
                'nodes': {'model.shop.orders': model},
                'sources': sources,
            }
        )
    )
    return root


def _amount(result: Timeline) -> list[tuple[str, list[str]]]:
    return [
        (e.commit.subject, c.english)
        for e in result.entries
        for c in e.changes
        if c.name == 'order.amount' and c.status == 'changed'
    ]


def test_a_var_changed_in_dbt_project_yml_is_a_change(tmp_path: Path) -> None:
    """The commit's dbt_project.yml gives var() its value, and its SQL compiled by dbt is kept
    only where the commit's dbt_project.yml is today's."""
    project = 'name: shop\nprofile: shop\nvars:\n  rate: {rate}\n'
    root = _varied_shop(
        tmp_path / 'shop',
        [
            (
                'Add orders',
                {
                    'dbt_project.yml': project.format(rate=2),
                    'models/orders.sql': VARIED,
                    'models/schema.yml': KEYED.format(described='The order.'),
                },
            ),
            ('Raise the rate', {'dbt_project.yml': project.format(rate=5)}),
        ],
        'select id as order_id, amount * 5 as amount from raw.orders',
    )
    assert _amount(timeline(root)) == [
        ('Raise the rate', ['in orders.amount, it was [amount] times 2; it is now [amount] times 5'])
    ]


def test_a_commit_that_only_documents_invents_no_formula_change(tmp_path: Path) -> None:
    """Where the commit's YAML differs from today's the model is rendered, and where it is the
    same dbt's compiled SQL is kept: the two must agree, or the commit that changed only a
    description would be credited with a formula change."""
    project = 'name: shop\nprofile: shop\nvars:\n  rate: 2\n'
    root = _varied_shop(
        tmp_path / 'shop',
        [
            (
                'Add orders',
                {
                    'dbt_project.yml': project,
                    'models/orders.sql': VARIED,
                    'models/schema.yml': KEYED.format(described='An order.'),
                },
            ),
            ('Describe the order', {'models/schema.yml': KEYED.format(described='The order.')}),
        ],
        'select id as order_id, amount * 2 as amount from raw.orders',
    )
    result = timeline(root)
    assert _amount(result) == []
    assert not [c for _, c in result.changes() if 'formulas' in c.fields]


def test_a_source_identifier_follows_the_commits_yaml(tmp_path: Path) -> None:
    """A source's table comes from the commit's YAML, not today's manifest."""
    sql = "select id as order_id, amount from {{ source('raw', 'orders') }}\n"
    sources = 'version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n        identifier: {table}\n'
    root = _varied_shop(
        tmp_path / 'shop',
        [
            (
                'Add orders',
                {
                    'dbt_project.yml': PROJECT,
                    'models/orders.sql': sql,
                    'models/sources.yml': sources.format(table='orders_v1'),
                    'models/schema.yml': KEYED.format(described='The order.'),
                },
            ),
            ('Read the new table', {'models/sources.yml': sources.format(table='orders_v2')}),
        ],
        'select id as order_id, amount from "wh"."raw"."orders_v2"',
    )
    changed = _amount(timeline(root))
    assert [subject for subject, _ in changed] == ['Read the new table']
    assert 'orders_v1' in changed[0][1][0] and 'orders_v2' in changed[0][1][0]


def _facts(world: World) -> dict[str, Any]:
    """What a world holds, without the formula texts, which are kept only to word a change."""
    return {k: v for k, v in world.to_dict().items() if k != 'texts'}


@pytest.mark.parametrize('ci', [False, True])
def test_the_world_after_a_commit_that_does_not_build_is_that_commits_own(
    shop: Path, ci: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.analysis import timeline as module

    if ci:
        monkeypatch.setenv('CI', 'true')
    (shop / 'konstruct.toml').write_text('this is not toml = = =\n')
    commit(shop, 'Break the configuration', 'Linus', '2026-04-01T10:00:00+00:00')
    (shop / 'konstruct.toml').unlink()
    commit(shop, 'Mend it', 'Linus', '2026-04-02T10:00:00+00:00')
    broken, mended = git(shop, 'rev-parse', 'HEAD~1', 'HEAD').split()

    def worlds(cache: bool) -> Any:
        return module._Worlds(shop, shop, Path(), None, cache, [])

    after = worlds(True)
    assert after.at(broken) is None
    world = after.at(mended)
    after.close()
    alone = worlds(False)
    fresh = alone.at(mended)
    alone.close()
    assert world is not None and fresh is not None and fresh.metrics and fresh.properties
    assert _facts(world) == _facts(fresh)
    # Nor is the wrong world kept: a later run reads back the commit's own.
    again = worlds(True)
    read = again.at(mended)
    again.close()
    assert read is not None and _facts(read) == _facts(fresh)


def test_the_readings_of_a_relation_read_twice_are_worded(tmp_path: Path) -> None:
    result = timeline(make_pairs(tmp_path / 'pairs'))
    first, second = (c.english for e in result.entries for c in e.changes)
    assert first == [
        'a new metric, trusted as declared: it is [amount (the first reading of orders)] plus '
        '[amount (the second reading of orders)]'
    ]
    joined = 'keeping every row of the left side, with no values from the right side where it has no match'
    assert second == [
        'it was [amount (the first reading of orders)] plus [amount (the second reading of orders)]; '
        'it is now [amount (the second reading of orders)] plus [amount (the first reading of orders)]',
        'no longer: rows are joined where [parent id (the first reading of raw.orders)] and '
        f'[id (the second reading of raw.orders)] both have values and are equal, {joined}',
        'no longer: the second reading of raw.orders is on the side of a left join that may find no '
        'match, so a row with no match in it is kept, with no values from it',
        f'now: the rows of the first reading of orders are joined where true, {joined}',
        'now: the first reading of raw.orders is on the side of a left join that may find no match, so '
        'a row with no match in it is kept, with no values from it',
        'now: rows are kept only where [status (the first reading of raw.orders)] has a value other than '
        "'void'",
    ]


def test_a_timeline_that_fails_leaves_no_tree_and_keeps_its_analyses(
    shop: Path, private_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    from konstruct.analysis import timeline as module

    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch))

    def fails(*args: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(module, 'world_diff', fails)
    with pytest.raises(KeyboardInterrupt):
        timeline(shop, since='HEAD~1')
    assert list(scratch.iterdir()) == []
    assert list(private_cache.glob('analysis-*.json'))


def test_a_dbt_run_that_changes_only_when_the_manifest_was_made_keeps_the_cache(
    shop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.analysis import timeline as module

    path = shop / 'target' / 'manifest.json'
    manifest = json.loads(path.read_text())
    disabled = {**manifest['nodes']['model.shop.orders'], 'unique_id': 'model.shop.old', 'created_at': 1.5}
    manifest['disabled'] = {'model.shop.old': [disabled]}
    path.write_text(json.dumps(manifest))
    catalog = shop / 'target' / 'catalog.json'

    def stats(rows: int) -> dict[str, Any]:
        return {
            'row_count': {'id': 'row_count', 'value': rows},
            'bytes': {'id': 'bytes', 'value': rows * 8},
            'last_modified': {'id': 'last_modified', 'value': f'2026-05-0{rows % 9 + 1}'},
        }

    def write_catalog(rows: int) -> None:
        catalog.write_text(
            json.dumps(
                {
                    'metadata': {'generated_at': f'2026-05-0{rows % 9 + 1}T00:00:00Z'},
                    'nodes': {
                        'model.shop.orders': {
                            'columns': {'order_id': {'name': 'order_id', 'type': 'INTEGER', 'index': 1}},
                            'stats': stats(rows),
                        }
                    },
                    'sources': {},
                }
            )
        )

    write_catalog(10)
    timeline(shop, since='HEAD~2')
    # A dbt run and a data load: when each artefact was made, and how much data there is.
    manifest['metadata'].update(generated_at='2026-05-01T00:00:00Z', invocation_id='another run')
    for node in manifest['nodes'].values():
        node['created_at'] = 1789000000.5
    disabled['created_at'] = 2.5
    path.write_text(json.dumps(manifest))
    write_catalog(20)
    built: list[str] = []
    real = module._Worlds._build

    def counted(self: Any, sha: str) -> Any:
        built.append(sha)
        return real(self, sha)

    monkeypatch.setattr(module._Worlds, '_build', counted)
    timeline(shop, since='HEAD~2')
    assert built == []
    manifest['nodes']['model.shop.orders']['compiled_code'] = 'select 1 as order_id'
    path.write_text(json.dumps(manifest))
    timeline(shop, since='HEAD~2')
    assert built  # what the manifest says is part of every world
