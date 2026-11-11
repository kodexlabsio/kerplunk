"""Sensitive data through the lineage, the gate's findings on it, the erasure map and masking."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis import sensitivity as analysis
from konstruct.analysis.diff import DiffError
from konstruct.analysis.erasure import erasure
from konstruct.analysis.query import QueryError
from konstruct.analysis.sensitivity import (
    Settings,
    _tag,
    column_marks,
    entity_keys,
    mask,
    sensitive_columns,
    sensitivity,
    settings_at,
)
from konstruct.core.graph import Graph, Node
from konstruct.engine.config import ConfigError
from konstruct.interfaces.cli import main

SQL = """\
create table crm.contacts as select p.id, p.email, upper(p.email) as shout, concat(p.first, ' ', p.last) as full_name from raw.people p;
create table mart.hashed as select p.id, md5(p.email) as email_hash, sha256(p.email) as sha, hash(p.email) as h, md5(lower(trim(p.email))) as normal_hash, md5(p.email) || p.email as leaky from raw.people p;
create table mart.counts as select p.region, count(p.email) as emails, count(distinct p.email) as distinct_emails, min(p.email) as first_email, max(p.age) as oldest from raw.people p group by p.region;
create table mart.stats as select p.region, avg(p.age) as mean_age, sum(p.age) as total_age from raw.people p group by p.region having count(distinct p.household_id) >= 10;
create table mart.small as select p.region, avg(p.age) as mean_age from raw.people p group by p.region having count(distinct p.household_id) >= 3;
create table mart.homes as select h.id as home, h.street from raw.households h join raw.people p on p.household_id = h.id;
create table mart.ungrouped as select avg(p.age) as mean_age from raw.people p;
create table mart.active as select p.id from raw.people p where p.ssn is not null;
create table mart.cased as select case when p.vip then p.email else 'hidden' end as shown, coalesce(p.email, p.phone) as contact, case when p.email like '%@x.com' then 1 else 0 end as is_x from raw.people p;
create table mart.merged as select p.email as contact from raw.people p union all select l.phone from raw.leads l;
create table mart.windowed as select p.id, first_value(p.email) over (order by p.id) as first_email, row_number() over (partition by p.email order by p.id) as rn, avg(p.age) over (partition by p.region) as region_age, count(p.email) over () as n from raw.people p;
create table mart.joined as select o.id, p.email, o.amount from raw.orders o join raw.people p on o.person_id = p.id;
create table mart.opaque as select * from raw.people;
create table mart.tokens as select tokenise(p.ssn) as token from raw.people p;
create table mart.nested as with c as (select p.id, lower(p.email) as e from raw.people p) select c.id, c.e as email from c;
"""

DEFINITIONS = """\
version: 1
terms:
  - name: Contact details
    definition: How to reach a person.
    sensitivity: Personal
    columns: [raw.people.email, raw.people.first, raw.people.ssn, raw.people.age, raw.people.vip, raw.households.street]
  - name: Lead phone
    definition: A lead's phone.
    sensitivity: personal
    columns: [raw.leads.phone]
  - name: Reporting
    definition: The reports the finance team reads.
    sensitivity: personal
    columns: [mart.joined]
  - name: Household
    kind: entity
    definition: The people who live at one address.
    columns: [raw.households.id]
"""

CONFIG = """\
[sensitivity]
holders = ["crm.*"]
sanitisers = ["tokenise"]
tags = ["restricted"]
k = 5
"""

SENSITIVE_PROJECT: dict[str, str] = {
    'models.sql': SQL,
    'definitions.yml': DEFINITIONS,
    'konstruct.toml': CONFIG.replace('holders = ["crm.*"]', 'holders = ["crm.*", "contact_list"]'),
    'views.lkml': (
        'view: contact_list {\n  sql_table_name: crm.contacts ;;\n'
        '  dimension: email {\n    sql: ${TABLE}.email ;;\n  }\n}\n'
        'view: report {\n  sql_table_name: mart.cased ;;\n'
        '  dimension: shown {\n    sql: ${TABLE}.shown ;;\n  }\n}\n'
    ),
}
"""A project of SQL files with every kind of read of a sensitive column, for the interface test too."""


def write_project(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding='utf-8')
    return root


def build(root: Path, files: dict[str, str]) -> Project:
    project = Project(write_project(root, files))
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    return project


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return build(tmp_path_factory.mktemp('sensitive') / 'shop', SENSITIVE_PROJECT)


def labels(project: Project) -> dict[str, list[str]]:
    return {c.label: c.sensitivity for c in project.sensitivity().columns}


# Propagation


def test_marks_come_from_terms(shop: Project) -> None:
    result = shop.sensitivity()
    marked = {c.label: [m.by for m in c.marks] for c in result.marked}
    assert marked['people.email'] == ["term 'Contact details'"]
    assert marked['leads.phone'] == ["term 'Lead phone'"]
    # The kind is a word, lower case, as definitions.yml normalises it.
    assert result.column('people.email') is not None
    assert result.column('people.email').sensitivity == ['personal']  # type: ignore[union-attr]


@pytest.mark.parametrize(
    'column',
    [
        'contacts.email',  # a copy
        'contacts.shout',  # transformed
        'contacts.full_name',  # one of a concatenation's inputs
        'cased.shown',  # a CASE branch
        'cased.is_x',  # a CASE condition: one bit of the value is still the value's
        'cased.contact',  # COALESCE
        'merged.contact',  # either side of a UNION
        'windowed.first_email',  # a window function's value
        'windowed.rn',  # a window partitioned by the value
        'windowed.region_age',  # an aggregate as a window function: no group size
        'joined.email',  # brought in by a join
        'nested.email',  # through a CTE
        'counts.first_email',  # MIN returns one person's value
        'counts.oldest',  # and so does MAX
        'counts.emails',  # a count over groups of unknown size
        'counts.distinct_emails',
        'windowed.n',  # a count as a window function
        'small.mean_age',  # a group of three is not large enough
        'ungrouped.mean_age',  # no group size at all
        'hashed.leaky',  # one read hashed and one not
        'hashed.email_hash',  # a hash is found again by hashing a guess
        'hashed.sha',
        'hashed.h',
        'hashed.normal_hash',
    ],
)
def test_sensitivity_flows_to(shop: Project, column: str) -> None:
    assert labels(shop)[column] == ['personal']


@pytest.mark.parametrize('column', ['hashed.email_hash', 'hashed.sha', 'hashed.h', 'hashed.normal_hash'])
def test_a_hash_pseudonymises(shop: Project, column: str) -> None:
    found = shop.sensitivity().column(column)
    assert found is not None and found.transform == 'pseudonymised'
    # One read in clear is the value in clear.
    leaky = shop.sensitivity().column('hashed.leaky')
    assert leaky is not None and leaky.transform == 'transformed'


@pytest.mark.parametrize(
    ('column', 'by'),
    [
        ('stats.mean_age', 'AVG over groups of at least 10 distinct keys'),
        ('stats.total_age', 'SUM over groups of at least 10 distinct keys'),
        ('tokens.token', 'TOKENISE'),
    ],
)
def test_a_sanitiser_stops_it(shop: Project, column: str, by: str) -> None:
    result = shop.sensitivity()
    assert column not in labels(shop)
    assert [s.by for s in result.sanitised if s.column == column] == [by]


PAY = """\
version: 1
terms:
  - name: Pay
    definition: pay.
    sensitivity: personal
    columns: [raw.pay.salary, raw.pay.pin, raw.pay.email, raw.users.name]
  - name: User
    kind: entity
    definition: A person with an account.
    columns: [raw.users.id]
"""
# A join that makes raw.pay.user_id a foreign key to the users, so its distinct values count people.
LINK = 'create table m.link as select u.id, u.name from raw.pay p join raw.users u on p.user_id = u.id;\n'


@pytest.mark.parametrize(
    'sql',
    [
        # Grouped on an identifier: each group is one person's rows.
        'select p.user_id, sum(p.salary) as s from raw.pay p group by p.user_id having count(distinct p.user_id) >= 10',
        # A CASE, an IF, a comparison or a FILTER picks one person's rows inside the aggregate.
        'select p.dept, sum(case when p.user_id = 42 then p.salary else 0 end) as s from raw.pay p '
        'group by p.dept having count(distinct p.user_id) >= 10',
        'select p.dept, sum(if(p.user_id = 42, p.salary, 0)) as s from raw.pay p group by p.dept '
        'having count(distinct p.user_id) >= 10',
        'select p.dept, sum(p.salary * (p.user_id = 42)::int) as s from raw.pay p group by p.dept '
        'having count(distinct p.user_id) >= 10',
        'select p.dept, sum(p.salary) filter (where p.user_id = 42) as s from raw.pay p group by p.dept '
        'having count(distinct p.user_id) >= 10',
        # Differencing: two aggregates over different rows.
        'select p.dept, sum(p.salary) - sum(case when p.user_id <> 42 then p.salary end) as s '
        'from raw.pay p group by p.dept having count(distinct p.user_id) >= 10',
        # A filter pins the rows before they are grouped.
        'select p.dept, sum(p.salary) as s from raw.pay p where p.user_id = 42 group by p.dept '
        'having count(distinct p.user_id) >= 10',
        # An implicit cross join with a large group elsewhere.
        'with one as (select sum(p.salary) as s from raw.pay p where p.user_id = 42), g as (select q.dept '
        'from raw.pay q group by q.dept having count(distinct q.user_id) >= 10) select one.s from one, g',
        'select o.dept, sum(p.salary) as s from raw.pay p, raw.other o group by o.dept having count(distinct p.user_id) >= 10',
        # Rows are not people: one person can have ten rows, and a login is one person's.
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(*) >= 10',
        'select p.login, avg(p.salary) as s from raw.pay p group by p.login having count(*) >= 10',
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(p.salary) >= 10',
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.login) >= 10',
        # A key unique per row counts rows: one person with ten payments has ten payment ids.
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.payment_id) >= 10',
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.id) >= 10',
        # A key nothing says refers to anyone.
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.account_id) >= 10',
        # Counting distinct values that are not a key, or rows a condition picks.
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.dept) >= 10',
        'select p.dept, avg(p.salary) as s from raw.pay p group by p.dept having approx_count_distinct(p.uid) >= 10',
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.user_id) >= 10 or true',
        'select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.user_id) >= 10 and max(p.pin) = 1',
        # ROLLUP and GROUPING SETS make groups the HAVING never sees whole.
        'select p.dept, sum(p.salary) as s from raw.pay p group by rollup (p.dept, p.region) having count(distinct p.user_id) >= 10',
        'select p.dept, sum(p.salary) as s from raw.pay p group by grouping sets ((p.dept), (p.region)) '
        'having count(distinct p.user_id) >= 10',
        # A conditional count, or a count with no group at all.
        'select p.user_id, count(case when p.salary > 100000 then 1 end) as s from raw.pay p group by p.user_id',
        'select p.user_id, count_if(p.salary > 100000) as s from raw.pay p group by p.user_id',
        'select p.dept, count(p.salary) * 0 + max(p.salary) as s from raw.pay p group by p.dept',
        # A hash of a low-entropy value, of part of a value, or compared with a guess.
        'select md5(p.pin) as s from raw.pay p',
        'select md5(substr(p.email, 1, 3)) as s from raw.pay p',
        "select md5(p.email) = 'abc' as s from raw.pay p",
        "select case when md5(p.email) = 'abc' then 1 end as s from raw.pay p",
        "select p.email['x'] as s, p.email from raw.pay p",
        'select p.email.domain as s, p.email from raw.pay p',
    ],
)
def test_no_aggregate_or_hash_launders_a_value(tmp_path: Path, sql: str) -> None:
    project = build(
        tmp_path, {'models.sql': f'create table m.out as {sql};\n' + LINK, 'definitions.yml': PAY}
    )
    assert labels(project).get('out.s') == ['personal'], project.graph.column('table.m.out', 's')


def test_a_large_group_stops_it(tmp_path: Path) -> None:
    sql = 'create table m.out as select p.dept, sum(p.salary) as s from raw.pay p group by p.dept having count(distinct p.user_id) >= 10;\n'
    project = build(tmp_path, {'models.sql': sql + LINK, 'definitions.yml': PAY})
    assert 'out.s' not in labels(project)
    assert [s.by for s in project.sensitivity().sanitised] == ['SUM over groups of at least 10 distinct keys']


@pytest.mark.parametrize('where', [None, 'is_current'])
def test_a_unique_test_on_some_rows_keys_no_entity_whose_distinct_values_count_people(
    tmp_path: Path, where: str | None
) -> None:
    """users keeps a row per version, its id unique only where is_current: the id keys no entity,
    so the payments' user_id is no foreign key to people, and its distinct values prove no group
    of k people."""
    sql = (
        'create table m.out as select p.dept, sum(p.salary) as s from raw.pay p group by p.dept '
        'having count(distinct p.user_id) >= 10;\n'
    )
    pay = PAY.split('  - name: User')[0]
    graph = build(tmp_path, {'models.sql': sql + LINK, 'definitions.yml': pay}).graph
    test = graph.add_node(Node('test.shop.unique_users_id', 'test', 'unique', attrs={'test': 'unique'}))
    if where:
        test.attrs['where'] = where
    users = graph.node_for_relation('raw.users')
    assert users is not None
    graph.add_edge(test.id, f'{users.id}#id', 'tests')
    assert (f'{users.id}#id' in entity_keys(graph)) is (where is None)
    sensitive = {c.label for c in sensitivity(graph, Settings()).columns}
    assert ('out.s' in sensitive) is (where is not None), sensitive


def test_a_larger_k_keeps_an_aggregate_sensitive(shop: Project) -> None:
    result = sensitivity(shop.graph, Settings(k=20))
    assert result.column('stats.mean_age') is not None
    assert result.column('tokens.token') is not None  # tokenise is not a sanitiser without the setting


def test_a_join_or_union_proves_no_group_size(tmp_path: Path) -> None:
    sql = (
        'create table mart.mixed as with a as (select p.region, sum(p.age) as total from raw.people p '
        'group by p.region), b as (select q.region, count(*) as n from raw.people q group by q.region '
        'having count(distinct q.id) >= 50) select a.region, a.total, b.n from a join b on a.region = b.region'
    )
    union = (
        'create table mart.both as select p.region, sum(p.age) as total from raw.people p group by p.region '
        'union all select q.region, sum(q.age) from raw.people q group by q.region having count(distinct q.id) >= 50'
    )
    project = build(tmp_path, {'models.sql': f'{sql};\n{union};\n', 'definitions.yml': DEFINITIONS})
    assert labels(project)['mixed.total'] == ['personal']
    assert labels(project)['both.total'] == ['personal']


def test_a_join_or_filter_only_shapes_rows(shop: Project) -> None:
    result = shop.sensitivity()
    assert ('active', 'people.ssn') in {(r.relation, r.column) for r in result.references}
    assert 'active.id' not in labels(shop)
    # A join on a column that is not sensitive brings the sensitive column in, not the key.
    assert labels(shop)['joined.email'] == ['personal'] and 'joined.amount' not in labels(shop)


def test_unknown_lineage_is_reported(shop: Project) -> None:
    assert [(u.relation, u.source) for u in shop.sensitivity().uncertain] == [('opaque', 'people')]


def test_holders(shop: Project) -> None:
    result = shop.sensitivity()
    held = {c.label: c.held for c in result.columns}
    assert held['contacts.email'] == "[sensitivity] holders 'crm.*'"
    assert held['people.email'] == 'marked'
    # A term bound to a relation marks it to hold the data.
    assert held['joined.email'] == "term 'Reporting'"
    assert held['cased.shown'] is None
    unheld = {c.label for c in result.unheld}
    assert 'cased.shown' in unheld and 'contacts.email' not in unheld and 'people.email' not in unheld


def test_exposures(shop: Project) -> None:
    exposures = {e.exposure: (e.columns, e.held) for e in shop.sensitivity().exposures}
    assert exposures == {
        'contact_list': (['contacts.email'], "[sensitivity] holders 'contact_list'"),
        'report': (['cased.shown'], None),
    }
    assert [e.exposure for e in shop.sensitivity().unheld_exposures] == ['report']


def test_one_column(shop: Project) -> None:
    result = shop.sensitivity('contacts.shout')
    [column] = result.columns
    assert column.path == ['people.email', 'contacts.shout']
    assert column.transform == 'transformed' and column.sources == ['people.email']
    assert 'path people.email -> contacts.shout' in str(result)
    stopped = shop.sensitivity('tokens.token')
    assert stopped.columns == [] and [s.by for s in stopped.sanitised] == ['TOKENISE']
    assert 'No sensitive columns in tokens.token' in stopped.summary()
    relation = shop.sensitivity('mart.hashed')
    assert {c.label for c in relation.columns} == {
        'hashed.leaky',
        'hashed.email_hash',
        'hashed.sha',
        'hashed.h',
        'hashed.normal_hash',
    }


def test_a_direct_path_stays_direct(shop: Project) -> None:
    assert shop.sensitivity('contacts.email').columns[0].transform == 'direct'


def test_the_text_and_json(shop: Project) -> None:
    result = shop.sensitivity()
    text = str(result)
    assert text.startswith('Sensitive data in the graph')
    for heading in (
        'Columns',
        'Stopped by a sanitiser',
        'Only joining, filtering or grouping',
        'Lineage not known',
    ):
        assert heading in text
    data = result.to_dict()
    assert data['settings'] == {
        'holders': ['crm.*', 'contact_list'],
        'sanitisers': ['tokenise'],
        'tags': ['restricted'],
        'k': 5,
    }
    assert 'cased.shown' in data['unheld']
    json.dumps(data)


def test_no_marks(tmp_path: Path) -> None:
    project = build(tmp_path, {'models.sql': 'create table a.b as select x.y from raw.x x'})
    result = project.sensitivity()
    assert result.columns == [] and result.summary().startswith('No sensitive columns')


# Propagation against a brute-force oracle

# Each expression with whether it carries the value of each column it reads: a is s.v, b is s.w.
OPERATIONS: list[tuple[str, tuple[bool, bool]]] = [
    ('{a}', (True, False)),
    ('upper({a})', (True, False)),
    ('md5({a})', (True, False)),
    ('sha256({a})', (True, False)),
    ('concat({a}, {b})', (True, True)),
    ('coalesce({a}, {b})', (True, True)),
    ('count({a}) over ()', (True, False)),
    ('md5({a}) || {b}', (True, True)),
    ('tokenise({a})', (False, False)),
    ('tokenise({a}) || {b}', (False, True)),
    ('case when {a} is null then {b} end', (True, True)),
    ('length({b})', (False, True)),
]


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.tuples(
            st.integers(0, 50),
            st.sampled_from(range(len(OPERATIONS))),
            st.sampled_from(range(len(OPERATIONS))),
            st.booleans(),
        ),
        min_size=1,
        max_size=6,
    )
)
def test_propagation_matches_an_oracle(
    tmp_path_factory: pytest.TempPathFactory, steps: list[tuple[int, int, int, bool]]
) -> None:
    # raw.t0 has v (marked) and w (not); each model reads an earlier relation, its v and w each
    # computed by one operation, either over (v, w) or over (w, v).
    # m.t0 reads both, so that raw.t0 has both columns whatever the models read.
    oracle: dict[str, dict[str, bool]] = {'raw.t0': {'v': True, 'w': False}, 'm.t0': {'v': True, 'w': False}}
    statements = ['create table m.t0 as select s.v, s.w from raw.t0 s']
    names = ['raw.t0']
    for index, (pick, left, right, swap) in enumerate(steps, start=1):
        source = names[pick % len(names)]
        a, b = ('s.w', 's.v') if swap else ('s.v', 's.w')
        sensitive = oracle[source]
        reads = (sensitive['w'], sensitive['v']) if swap else (sensitive['v'], sensitive['w'])
        row: dict[str, bool] = {}
        parts = []
        for column, operation in (('v', left), ('w', right)):
            template, carries = OPERATIONS[operation]
            parts.append(f'{template.format(a=a, b=b)} as {column}')
            row[column] = any(c and r for c, r in zip(carries, reads, strict=True))
        name = f'm.t{index}'
        statements.append(f'create table {name} as select {", ".join(parts)} from {source} s')
        oracle[name] = row
        names.append(name)
    definitions = 'version: 1\nterms:\n  - name: V\n    definition: v.\n    sensitivity: personal\n    columns: [raw.t0.v]\n'
    root = tmp_path_factory.mktemp('oracle')
    files = {
        'models.sql': ';\n'.join(statements) + ';\n',
        'definitions.yml': definitions,
        'konstruct.toml': '[sensitivity]\nsanitisers = ["tokenise"]\n',
    }
    project = build(root, files)
    found = {c.label for c in project.sensitivity().columns}
    # raw.t0 and m.t0 share a short name, so each is shown by its warehouse name.
    expected = {
        f'{n if n.endswith(".t0") else n.rsplit(".", 1)[-1]}.{c}'
        for n, row in oracle.items()
        for c, s in row.items()
        if s
    }
    assert found == expected


# Warehouse tags and masking policies


@pytest.mark.parametrize(
    ('name', 'value', 'label'),
    [
        ('PII', 'true', 'pii'),
        ('analytics.governance.PII', 'email', 'pii'),
        ('SNOWFLAKE.CORE.PRIVACY_CATEGORY', 'IDENTIFIER', 'identifier'),
        ('snowflake.core.semantic_category', 'EMAIL', 'email'),
        ('class.email_address', '', 'email_address'),
        ('sensitivity', 'confidential', 'confidential'),
        ('PII', 'none', None),
        ('privacy_category', 'insensitive', None),
        ('cost_centre', 'finance', None),
        ('restricted', '', 'restricted'),
    ],
)
def test_warehouse_tags(name: str, value: str, label: str | None) -> None:
    assert _tag(name, value, frozenset({'restricted'})) == label


def _built(tmp_path: Path, sql: str, attrs: dict[str, Any]) -> Graph:
    project = build(tmp_path, {'models.sql': sql})
    graph = project.graph
    column = graph.column('table.raw.t', 'email')
    assert column is not None
    column.attrs.update(attrs)
    return graph


def test_a_warehouse_tag_marks_a_column(tmp_path: Path) -> None:
    graph = _built(
        tmp_path,
        'create table mart.copy as select t.email from raw.t t',
        {'warehouse_tags': {'PII': 'email'}},
    )
    marks = column_marks(graph)
    assert [(m.sensitivity, m.by) for m in marks['table.raw.t#email']] == [
        ('pii', 'warehouse tag PII = email')
    ]
    assert sensitive_columns(graph) == {'table.raw.t#email': ['pii'], 'table.mart.copy#email': ['pii']}


def test_an_unmasked_copy(tmp_path: Path) -> None:
    sql = (
        'create table mart.copy as select t.email, md5(t.email) as hashed from raw.t t;\n'
        'create view mart.reader as select t.email from raw.t t;\n'
    )
    graph = _built(tmp_path, sql, {'masking_policy': 'governance.email_mask', 'warehouse_tags': {'PII': ''}})
    result = sensitivity(graph)
    assert [(u.column, u.source, u.policy) for u in result.unmasked] == [
        ('copy.email', 't.email', 'governance.email_mask')
    ]
    source = result.column('t.email')
    assert source is not None
    assert [m.by for m in source.marks] == ['warehouse tag PII', 'masking policy governance.email_mask']
    assert 'Unmasked copies of masked columns' in str(result)
    # A copy with a policy of its own is masked too.
    copy = graph.column('table.mart.copy', 'email')
    assert copy is not None
    copy.attrs['masking_policy'] = 'governance.email_mask'
    assert sensitivity(graph).unmasked == []


def test_a_masking_policy_alone_marks_a_column(tmp_path: Path) -> None:
    graph = _built(tmp_path, 'create table mart.copy as select t.email from raw.t t', {'masking_policy': 'p'})
    assert sensitive_columns(graph)['table.mart.copy#email'] == ['masked']


# Settings


def test_settings() -> None:
    assert Settings.read(None) == Settings()
    read = Settings.read({'holders': ['a'], 'sanitisers': ['Tokenise'], 'tags': ['x'], 'k': 3})
    assert read == Settings(('a',), ('Tokenise',), ('x',), 3)
    base = Settings(('a', 'b'), ('tokenise',), ('y',), 7)
    assert read.strictest(base) == Settings(('a',), ('Tokenise',), ('x', 'y'), 7)
    assert read.strictest(None) is read
    for bad, message in (
        ([], 'must be a table'),
        ({'holder': []}, "no setting 'holder'"),
        ({'holders': 'a'}, 'list of names'),
        ({'tags': ['']}, 'list of names'),
        ({'k': 1}, 'at least 2'),
        ({'k': True}, 'at least 2'),
    ):
        with pytest.raises(ConfigError, match=message):
            Settings.read(bad)


def test_an_invalid_section_is_a_configuration_error(tmp_path: Path) -> None:
    project = build(tmp_path, {'models.sql': SQL, 'definitions.yml': DEFINITIONS})
    (tmp_path / 'konstruct.toml').write_text('[sensitivity]\nk = 0\n')
    with pytest.raises(ConfigError):
        project.sensitivity()
    assert main(['-C', str(tmp_path), 'sensitivity']) == 1


def _git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def _repository(root: Path) -> None:
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'config', 'user.email', 'test@example.com')
    _git(root, 'config', 'user.name', 'Test')
    _git(root, 'config', 'commit.gpgsign', 'false')
    (root / '.gitignore').write_text('konstruct-out/\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Start')


def test_settings_at_a_commit(tmp_path: Path) -> None:
    write_project(tmp_path, {'konstruct.toml': CONFIG})
    _repository(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[sensitivity]\nholders = ["everything"]\n')
    assert settings_at(tmp_path, 'HEAD') == Settings(('crm.*',), ('tokenise',), ('restricted',), 5)
    assert settings_at(tmp_path, None) == Settings(('everything',))
    assert settings_at(tmp_path, 'no-such-ref') is None
    (tmp_path / 'konstruct.toml').write_text('[sensitivity\n')
    assert settings_at(tmp_path, None) is None
    (tmp_path / 'konstruct.toml').write_text('[sensitivity]\nk = 0\n')
    assert settings_at(tmp_path, None) is None
    (tmp_path / 'konstruct.toml').unlink()
    (tmp_path / 'pyproject.toml').write_text('[tool.konstruct.sensitivity]\nk = 4\n')
    assert settings_at(tmp_path, None) == Settings(k=4)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    assert settings_at(tmp_path, None) is None


def test_settings_outside_git(tmp_path: Path) -> None:
    assert settings_at(tmp_path, None) is None


# Definitions


# The gate


GATED = {
    'models.sql': 'create table crm.contacts as select p.id, p.email from raw.people p;\n',
    'definitions.yml': DEFINITIONS,
    'konstruct.toml': '[sensitivity]\nholders = ["crm.*"]\n',
}


@pytest.fixture
def gated(tmp_path: Path) -> Project:
    project = build(tmp_path, GATED)
    _repository(tmp_path)
    return project


def _gate(project: Project) -> list[tuple[str, str]]:
    result = project.gate(base='HEAD', run_dbt=False, warehouses=False)
    return [(f.rule, f.message) for f in result.findings]


def test_the_gate_passes_held_data(gated: Project) -> None:
    (gated.root / 'models.sql').write_text(
        GATED['models.sql']
        + 'create table crm.copied as select c.email, md5(c.email) as h from crm.contacts c;\n'
    )
    assert _gate(gated) == []


def test_the_gate_stops_sensitive_data_in_a_model_not_marked_to_hold_it(gated: Project) -> None:
    (gated.root / 'models.sql').write_text(
        GATED['models.sql']
        + 'create table mart.report as select c.id, c.email, upper(c.email) as e2, md5(c.email) as h from crm.contacts c;\n'
    )
    [(rule, message)] = _gate(gated)
    assert rule == 'sensitive-flow'
    assert message == (
        'report receives personal data from people.email in report.e2, report.email, report.h, and is not marked to hold it'
    )
    result = gated.gate(base='HEAD', update=False)
    assert result.findings[0].path == 'models.sql'
    assert 'holders' in result.findings[0].fix


def test_a_change_cannot_mark_its_own_model(gated: Project) -> None:
    sql = GATED['models.sql'] + 'create table mart.report as select c.email from crm.contacts c;\n'
    (gated.root / 'models.sql').write_text(sql)
    (gated.root / 'konstruct.toml').write_text('[sensitivity]\nholders = ["crm.*", "mart.*"]\n')
    assert [r for r, _ in _gate(gated)] == ['sensitive-flow']
    # Nor by marking the column in definitions.yml itself.
    (gated.root / 'konstruct.toml').write_text(GATED['konstruct.toml'])
    (gated.root / 'definitions.yml').write_text(
        DEFINITIONS
        + '  - name: Report email\n    definition: e.\n    sensitivity: personal\n    columns: [mart.report.email]\n'
    )
    assert [r for r, _ in _gate(gated)] == ['sensitive-flow']
    # Once the base holds the mark, it counts.
    _git(gated.root, 'add', '-A')
    _git(gated.root, 'commit', '-qm', 'Report')
    assert _gate(gated) == []


def test_the_gate_stops_a_removed_mark(gated: Project) -> None:
    (gated.root / 'definitions.yml').write_text(
        DEFINITIONS.replace(
            '    sensitivity: personal\n    columns: [raw.leads.phone]', '    columns: [raw.leads.phone]'
        )
    )
    findings = gated.gate(base='HEAD', run_dbt=False, warehouses=False).findings
    assert [(f.rule, f.message, f.path) for f in findings] == [
        ('sensitivity-removed', 'Lead phone: sensitivity personal -> none', 'definitions.yml')
    ]


LEAK = GATED['models.sql'] + 'create table mart.report as select c.email from crm.contacts c;\n'
STOPPED = [
    (
        'sensitive-flow',
        'report receives personal data from people.email in report.email, and is not marked to hold it',
    )
]


def _gated(root: Path, files: dict[str, str]) -> Project:
    project = build(root, files)
    _repository(root)
    return project


@pytest.mark.parametrize(
    ('base', 'head'),
    [
        # No configuration at the base: a holder the change adds holds nothing.
        ({}, {'konstruct.toml': '[sensitivity]\nholders = ["*"]\n'}),
        ({}, {'pyproject.toml': '[tool.konstruct.sensitivity]\nholders = ["*"]\n'}),
        # A base whose settings are not valid is one with none.
        (
            {'konstruct.toml': '[sensitivity]\nholders = ["crm.*"]\nbogus = 1\n'},
            {'konstruct.toml': '[sensitivity]\nholders = ["*"]\n'},
        ),
    ],
)
def test_the_gate_fails_closed_on_the_base_settings(
    tmp_path: Path, base: dict[str, str], head: dict[str, str]
) -> None:
    files = {'models.sql': GATED['models.sql'], 'definitions.yml': DEFINITIONS, **base}
    project = _gated(tmp_path, files)
    for name, text in {'models.sql': LEAK, **head}.items():
        (tmp_path / name).write_text(text)
    assert _gate(project) == STOPPED


def test_the_gate_fails_closed_without_the_base_commit(gated: Project) -> None:
    """A shallow clone that lacks the base commit reads no settings there."""
    from konstruct.analysis.gate import gate

    (gated.root / 'models.sql').write_text(LEAK)
    gated.build(update=True, run_dbt=False, warehouses=False)
    diff = gated.diff(base='HEAD')
    diff.base, diff.base_commit = 'f' * 40, None
    findings = gate(gated.graph, diff, gated.conflicts()).findings
    assert [(f.rule, f.message) for f in findings if f.rule == 'sensitive-flow'] == STOPPED


def test_a_term_the_change_binds_to_its_model_holds_nothing(gated: Project) -> None:
    (gated.root / 'models.sql').write_text(LEAK)
    (gated.root / 'definitions.yml').write_text(
        DEFINITIONS
        + '  - name: Report\n    definition: r.\n    sensitivity: personal\n    columns: [mart.report]\n'
    )
    assert _gate(gated) == STOPPED


def test_a_pack_mark_on_a_changed_column_holds_nothing(gated: Project) -> None:
    """A schema the change makes look like Stripe's binds the pack's personal term to the change's
    own column: that mark does not make the column a holder."""
    (gated.root / 'models.sql').write_text(
        GATED['models.sql']
        + 'create table stripe_out.customer as select c.id, c.email, false as delinquent, false as livemode '
        'from crm.contacts c;\n'
        'create table stripe_out.charge as select 1 as id, 1 as amount, 0 as amount_refunded, true as captured, '
        "true as paid, 'usd' as currency, false as livemode;\n"
    )
    findings = _gate(gated)
    assert (
        'sensitive-flow',
        'customer receives personal data from people.email in customer.email, and is not marked to hold it',
    ) in findings
    assert gated.sensitivity('stripe_out.customer.email').columns[0].marks


def test_a_new_exposure_on_an_existing_model_is_judged(tmp_path: Path) -> None:
    sql = GATED['models.sql'] + 'create table mart.shown as select p.id, p.email from raw.people p;\n'
    project = _gated(tmp_path, {**GATED, 'models.sql': sql})
    (tmp_path / 'views.lkml').write_text(
        'view: shown_view {\n  sql_table_name: mart.shown ;;\n  dimension: email {\n    sql: ${TABLE}.email ;;\n  }\n}\n'
    )
    findings = project.gate(base='HEAD', run_dbt=False, warehouses=False).findings
    assert [(f.rule, f.message, f.path) for f in findings] == [
        (
            'sensitive-flow',
            'shown_view shows personal data from shown.email, and is not marked to hold it',
            'views.lkml',
        )
    ]


@pytest.mark.parametrize('read', ['c.profile.email', "c.profile['email']"])
def test_a_struct_field_carries_its_struct(tmp_path: Path, read: str) -> None:
    defs = (
        DEFINITIONS
        + '  - name: Profile\n    definition: p.\n    sensitivity: personal\n    columns: [raw.people.profile]\n'
    )
    base = 'create table crm.contacts as select p.id, p.email, p.profile from raw.people p;\n'
    project = _gated(tmp_path, {**GATED, 'models.sql': base, 'definitions.yml': defs})
    (tmp_path / 'models.sql').write_text(
        base + f'create table mart.report as select {read} as e from crm.contacts c;\n'
    )
    assert _gate(project) == [
        (
            'sensitive-flow',
            'report receives personal data from people.profile in report.e, and is not marked to hold it',
        )
    ]


def test_a_sensitive_field_makes_its_struct_sensitive(tmp_path: Path) -> None:
    defs = 'version: 1\nterms:\n  - name: E\n    definition: e.\n    sensitivity: personal\n    columns: [raw.people.profile.email]\n'
    sql = 'create table m.a as select p.profile, p.profile.email as e, p.profile.age as age from raw.people p;\n'
    project = build(tmp_path, {'models.sql': sql, 'definitions.yml': defs})
    found = labels(project)
    assert found['people.profile'] == ['personal'] and found['a.profile'] == ['personal']
    assert found['a.e'] == ['personal'] and 'a.age' not in found


def test_unknown_lineage_into_a_changed_model_fails_the_gate(gated: Project) -> None:
    (gated.root / 'models.sql').write_text(
        GATED['models.sql'] + 'create table mart.report as select * from raw.people;\n'
    )
    assert _gate(gated) == [
        (
            'sensitive-flow',
            'report reads people, which holds personal data, through SQL whose columns Konstruct does not '
            'know, so it may hold that data, and is not marked to hold it',
        )
    ]


def test_turning_a_pack_off_removes_its_marks(gated: Project) -> None:
    (gated.root / 'konstruct.toml').write_text(
        GATED['konstruct.toml'] + '[ontologies]\npacks = ["hubspot"]\n'
    )
    findings = gated.gate(base='HEAD', run_dbt=False, warehouses=False).findings
    assert [(f.rule, f.message, f.path) for f in findings] == [
        (
            'sensitivity-removed',
            f'the change turns off the ontology pack {name}, whose terms mark columns sensitive',
            'konstruct.toml',
        )
        for name in ('shopify', 'stripe')
    ]


def test_the_gate_keeps_the_warehouse_marks_of_the_last_build(gated: Project) -> None:
    """The gate from files alone reads no warehouse: the tags the last full build read still count."""
    graph = gated.graph
    column = graph.column('table.raw.people', 'id')
    assert column is not None
    column.attrs['warehouse_tags'] = {'PII': ''}
    graph.write(gated.graph_path)
    (gated.root / 'models.sql').write_text(
        GATED['models.sql'] + 'create table mart.ids as select p.id from raw.people p;\n'
    )
    # Each gate rebuilds graph.json from files alone, and keeps the tags for the next one.
    for _ in range(3):
        assert _gate(Project(gated.root)) == [
            ('sensitive-flow', 'ids receives pii data from people.id in ids.id, and is not marked to hold it')
        ]


@pytest.mark.parametrize(
    ('create', 'table', 'full'),
    [
        # The warehouse names the table with its catalogue, a build from files alone without it.
        (
            'create schema raw; create table raw.people (id int, email text)',
            'raw.people',
            'table.w.raw.people',
        ),
        # SQL that names the table alone: the warehouse adds its catalogue and schema.
        ('create table people (id int, email text)', 'people', 'table.w.main.people'),
    ],
)
def test_the_gate_keeps_the_warehouse_marks_of_a_build_that_named_more_of_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_config: None, create: str, table: str, full: str
) -> None:
    """The tags a full build read are kept by a build from files alone that names the table with
    less of its warehouse key."""
    import duckdb

    from konstruct.inputs import warehouse

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute(create)
    con.close()
    real = warehouse.DuckDBConnector.fetch

    def fetch(self: Any) -> Any:
        relations = real(self)
        for relation in relations:
            for column in relation.columns:
                if column.name == 'email':
                    column.attrs['warehouse_tags'] = {'PII': 'email'}
        return relations

    monkeypatch.setattr(warehouse.DuckDBConnector, 'fetch', fetch)
    write_project(
        tmp_path,
        {
            'konstruct.toml': '[[warehouse]]\ntype = "duckdb"\npath = "w.duckdb"\n\n[sensitivity]\nholders = ["crm.*"]\n',
            'models.sql': GATED['models.sql'].replace('raw.people', table),
        },
    )
    project = Project(tmp_path)
    graph = project.build(run_dbt=False, dialect='duckdb').graph
    assert graph.column(full, 'email').attrs['warehouse_tags'] == {'PII': 'email'}  # type: ignore[union-attr]
    _repository(tmp_path)
    (tmp_path / 'models.sql').write_text(LEAK.replace('raw.people', table))
    for _ in range(2):
        assert _gate(Project(tmp_path)) == [
            (
                'sensitive-flow',
                'report receives pii data from people.email in report.email, and is not marked to hold it',
            )
        ]


AGG_PAYMENTS = (
    'create table m.agg as select p.dept, avg(p.salary) as a from raw.pay p group by p.dept '
    'having count(distinct p.payment_id) >= 10;\n'
)
AGG_USERS = AGG_PAYMENTS.replace('m.agg', 'm.aggu').replace('payment_id', 'user_id')


def test_a_change_cannot_forge_the_key_that_proves_a_group(tmp_path: Path) -> None:
    """A join the change adds, equating payment_id with the users' key, is no evidence that
    payment_id counts people."""
    project = _gated(tmp_path, {'models.sql': LINK, 'definitions.yml': PAY})
    forged = 'create table m.f as select u.name from raw.pay p join raw.users u on p.payment_id = u.id;\n'
    (tmp_path / 'models.sql').write_text(LINK + forged + AGG_PAYMENTS)
    assert (
        'sensitive-flow',
        'agg receives personal data from pay.salary in agg.a, and is not marked to hold it',
    ) in _gate(project)
    # Outside the gate, the join is the project's own and counts.
    assert 'agg.a' not in labels(project)


def test_a_foreign_key_on_the_base_proves_a_group(tmp_path: Path) -> None:
    project = _gated(tmp_path, {'models.sql': LINK, 'definitions.yml': PAY})
    # A join in a file the change rewrites is the change's own: it proves nothing.
    (tmp_path / 'models.sql').write_text(AGG_USERS)
    (tmp_path / 'link.sql').write_text(LINK)
    assert (
        'sensitive-flow',
        'aggu receives personal data from pay.salary in aggu.a, and is not marked to hold it',
    ) in _gate(project)
    # The join where the base has it, in a file the change leaves alone, proves the group.
    (tmp_path / 'link.sql').unlink()
    (tmp_path / 'models.sql').write_text(LINK)
    (tmp_path / 'agg.sql').write_text(AGG_USERS)
    assert _gate(project) == []


def test_a_change_touching_the_files_that_declare_a_key_proves_nothing_with_it(tmp_path: Path) -> None:
    """A primary key does not record the YAML file that declares it, and an Ossie key names its
    model: while the change touches such a file, the gate takes none of them as an entity key."""
    graph = build(
        tmp_path, {'models.sql': 'create table m.x as select u.id, u.code from raw.users u;\n'}
    ).graph
    users = graph.nodes['table.raw.users']
    users.attrs['primary_key'] = ['id']
    code = graph.column('table.raw.users', 'code')
    assert code is not None
    code.attrs['ossie_key'] = 'model.ossie.yaml'
    both = {'table.raw.users#id', 'table.raw.users#code'}
    assert entity_keys(graph) == both
    assert entity_keys(graph, frozenset({'models.sql'})) == both
    assert entity_keys(graph, frozenset({'models/schema.yml'})) == {'table.raw.users#code'}
    assert entity_keys(graph, frozenset({'model.ossie.yaml'})) == set()
    users.path = 'users.sql'
    assert entity_keys(graph, frozenset({'users.sql'})) == {'table.raw.users#code'}


PAID = 'create table crm.copy as select p.dept, p.salary from raw.pay p;\n'
SALARY = 'version: 1\nterms:\n  - name: Pay\n    definition: p.\n    sensitivity: personal\n    columns: [raw.pay.salary]\n'
VANISHED = [
    (
        'sensitivity-removed',
        "pay.salary is no longer marked personal: the term 'Pay' binds it at the base and not after the change",
    ),
    (
        'sensitive-flow',
        'leak reads pay, which holds personal data at the base, through SQL whose columns Konstruct does not '
        'know, so it may hold that data, and is not marked to hold it',
    ),
]


HIDE = 'create table raw.pay as select 1 as dept'


def _vanishing(root: Path, first: str, base: dict[str, str] | None = None) -> Project:
    """A change whose new file builds raw.pay with no salary column: the copy of salary it adds
    then has no column lineage, and the term marking salary binds nothing."""
    build(
        root,
        {
            'models.sql': PAID,
            'definitions.yml': SALARY,
            'konstruct.toml': '[sensitivity]\nholders = ["crm.*"]\n',
            **(base or {}),
        },
    )
    _repository(root)
    _git(root, 'checkout', '-qb', 'change')
    (root / 'models.sql').write_text(PAID + 'create table m.leak as select p.salary from raw.pay p;\n')
    (root / 'a_first.sql').write_text(f'{first};\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Change')
    shutil.rmtree(root / 'konstruct-out')  # a fresh checkout, as in CI
    return Project(root)


@pytest.mark.parametrize(
    ('first', 'base'),
    [
        (HIDE, None),
        ('create table raw.pay as select u.dept from raw.users u join raw.teams t on u.team = t.id', None),
        # What git archive leaves out or rewrites is still the base: the gate reads its blobs.
        (HIDE, {'.gitattributes': 'definitions.yml export-ignore\n'}),
        (HIDE, {'.gitattributes': '*.sql export-ignore\n'}),
    ],
)
def test_a_mark_the_change_makes_disappear_fails_the_gate(
    tmp_path: Path, first: str, base: dict[str, str] | None
) -> None:
    project = _vanishing(tmp_path, first, base)
    result = project.gate(base='main', run_dbt=False, warehouses=False)
    assert any('Unknown column: salary' in w for w in project.graph.warnings)
    assert [(f.rule, f.message) for f in result.findings] == VANISHED
    assert [f.path for f in result.findings] == ['definitions.yml', 'models.sql']


def test_a_base_that_cannot_be_built_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _vanishing(tmp_path, HIDE)
    monkeypatch.setattr(analysis, 'base_graph', lambda root, ref, cache: None)
    assert [
        (f.rule, f.message) for f in project.gate(base='main', run_dbt=False, warehouses=False).findings
    ] == [
        (
            'sensitivity-removed',
            "the personal term 'Pay' names raw.pay.salary, which is not a column, and Konstruct cannot "
            'build the base to tell whether the change made its mark disappear',
        ),
        (
            'sensitive-flow',
            'leak reads pay, which may hold sensitive data at the base (Konstruct cannot build the base to '
            'tell), through SQL whose columns Konstruct does not know, so it may hold that data, and is not '
            'marked to hold it',
        ),
    ]


def test_the_base_is_built_only_when_a_mark_may_have_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every term binds and every read has its column lineage: nothing can have vanished."""

    def unexpected(root: Path, ref: str, cache: Path | None) -> Graph:
        raise AssertionError('the base was built')

    project = _gated(
        tmp_path,
        {
            'models.sql': PAID,
            'definitions.yml': SALARY,
            'konstruct.toml': '[sensitivity]\nholders = ["crm.*"]\n',
        },
    )
    monkeypatch.setattr(analysis, 'base_graph', unexpected)
    (tmp_path / 'models.sql').write_text(PAID + 'create table m.r as select c.salary from crm.copy c;\n')
    assert _gate(project) == [
        (
            'sensitive-flow',
            'r receives personal data from pay.salary in r.salary, and is not marked to hold it',
        )
    ]


def test_a_term_the_base_does_not_have_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A term the change leaves alone is on the base: when the graph of the base lacks it, the
    base was not read in full, and the term's unbound column is a finding."""
    project = _vanishing(tmp_path, HIDE)
    monkeypatch.setattr(analysis, 'base_graph', lambda root, ref, cache: Graph())
    assert [
        (f.rule, f.message) for f in project.gate(base='main', run_dbt=False, warehouses=False).findings
    ] == [
        (
            'sensitivity-removed',
            "the personal term 'Pay' names raw.pay.salary, which is not a column, and the base, built "
            'from its files, has no such term to tell whether the change made its mark disappear',
        )
    ]


def test_the_base_tree_is_the_commit_as_it_holds_it(tmp_path: Path) -> None:
    """Every regular file of the commit, whatever .gitattributes says, and no link: a link could
    lead outside the tree."""
    (tmp_path / 'dir').mkdir()
    (tmp_path / 'dir' / 'kept.sql').write_text('select 1')
    (tmp_path / 'stamp.txt').write_text('$Format:%H$')
    (tmp_path / '.gitattributes').write_text('dir/** export-ignore\nstamp.txt export-subst\n')
    (tmp_path / 'link').symlink_to('/etc/hostname')
    _repository(tmp_path)
    tree = tmp_path / 'konstruct-out' / 'tree'
    analysis._write_tree(tmp_path, 'HEAD', Path(), tree)
    assert (tree / 'dir' / 'kept.sql').read_text() == 'select 1'
    assert (tree / 'stamp.txt').read_text() == '$Format:%H$'
    assert not (tree / 'link').exists() and not (tree / 'link').is_symlink()
    with pytest.raises(DiffError):
        analysis._write_tree(tmp_path, 'no-such-ref', Path(), tmp_path / 'konstruct-out' / 'other')


def test_the_base_graph_is_kept_by_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _vanishing(tmp_path, HIDE)
    cache = tmp_path / 'cache'
    built = analysis.base_graph(project.root, 'main', cache)
    assert built is not None
    [kept] = cache.iterdir()

    def unexpected(config: Any, **_: Any) -> Any:
        raise AssertionError('the base was built again')

    monkeypatch.setattr('konstruct.engine.build.build', unexpected)
    again = analysis.base_graph(project.root, 'main', cache)
    assert again is not None and again.column('table.raw.pay', 'salary') is not None
    # Another commit, or the same commit in another clone, is another graph.
    clone = tmp_path / 'clone'
    _git(tmp_path, 'clone', '-q', str(tmp_path), str(clone))
    monkeypatch.undo()
    assert analysis.base_graph(clone, 'origin/main', cache) is not None
    assert len(list(cache.iterdir())) == 2
    # A file that does not read is built again, and only the latest few graphs are kept.
    kept.write_text('{')
    assert analysis.base_graph(project.root, 'main', cache) is not None
    for index in range(analysis.BASE_GRAPHS + 2):
        (cache / f'{index:064}.json').write_text('{}')
    analysis.base_graph(project.root, 'change', cache)
    assert len(list(cache.iterdir())) == analysis.BASE_GRAPHS


def test_a_cache_others_can_write_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph is read back only from a file and a directory that are the user's own, not links,
    and writable by no one else."""
    from konstruct.engine import build as building

    project = _vanishing(tmp_path, HIDE)
    cache = tmp_path / 'cache'
    assert analysis.base_graph(project.root, 'main', cache) is not None
    [kept] = cache.iterdir()
    assert kept.stat().st_mode & 0o777 == 0o600
    builds: list[object] = []
    real = building.build

    def counted(config: Any, **options: Any) -> Any:
        builds.append(config)
        return real(config, **options)

    monkeypatch.setattr(building, 'build', counted)
    analysis.base_graph(project.root, 'main', cache)
    assert builds == []
    kept.chmod(0o620)  # group writable
    analysis.base_graph(project.root, 'main', cache)
    assert len(builds) == 1 and kept.stat().st_mode & 0o777 == 0o600
    cache.chmod(0o777)  # a directory anyone can write: neither read nor written
    analysis.base_graph(project.root, 'main', cache)
    assert len(builds) == 2
    cache.chmod(0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(cache, target_is_directory=True)
    analysis.base_graph(project.root, 'main', linked)
    assert len(builds) == 3
    if hasattr(os, 'getuid') and os.getuid() == 0:
        os.chown(kept, 4242, -1)  # another user's file
        analysis.base_graph(project.root, 'main', cache)
        assert len(builds) == 4
    assert not analysis._private(tmp_path / 'missing')


def test_the_base_cache_is_the_users_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ('CI', 'GITHUB_ACTIONS'):
        monkeypatch.setenv(variable, 'true')
        assert analysis.base_cache() is None
        monkeypatch.delenv(variable)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert analysis.base_cache() == tmp_path / 'konstruct' / 'base'
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative')
    monkeypatch.setattr('sys.platform', 'linux')
    assert analysis.base_cache() == Path.home() / '.cache' / 'konstruct' / 'base'
    monkeypatch.setattr('sys.platform', 'darwin')
    assert analysis.base_cache() == Path.home() / 'Library' / 'Caches' / 'konstruct' / 'base'
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    assert analysis.base_cache() == tmp_path / 'local' / 'konstruct' / 'base'


@pytest.mark.parametrize('out', ['konstruct-out', 'poison'])
def test_a_graph_of_the_base_the_change_commits_is_not_read(
    tmp_path: Path, out: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change that commits a forged graph of its base, at the name the gate keeps it under, in
    its output directory or one its configuration names, does not hide the mark it makes
    disappear: the gate keeps its graphs only in the user's own cache."""
    for variable in ('CI', 'GITHUB_ACTIONS'):
        monkeypatch.delenv(variable, raising=False)
    project = _vanishing(tmp_path, HIDE)
    forged = analysis.base_graph(tmp_path, 'main')
    assert forged is not None
    term = next(n for n in forged.nodes.values() if n.kind == 'term' and n.name == 'Pay')
    del term.attrs['sensitivity']
    located = analysis._located(tmp_path, 'main')
    assert located is not None
    name = located[3]
    planted = tmp_path / out / 'base' / name
    forged.write(planted)
    planted.parent.chmod(0o700)  # as the user's own cache would be, so only its place is wrong
    planted.chmod(0o600)
    if out == 'konstruct-out':
        (tmp_path / '.gitignore').write_text('')
    else:
        (tmp_path / 'konstruct.toml').write_text('out_dir = "poison"\n\n[sensitivity]\nholders = ["crm.*"]\n')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'Forge the base')
    result = Project(tmp_path).gate(base='main', run_dbt=False, warehouses=False)
    assert [(f.rule, f.message) for f in result.findings] == VANISHED
    cache = analysis.base_cache()
    assert cache is not None and not cache.resolve().is_relative_to(tmp_path.resolve())
    assert (cache / name).is_file()  # the gate kept its own graph there
    assert project.root == tmp_path


def test_the_base_graph_is_built_from_the_commit(tmp_path: Path) -> None:
    project = _vanishing(tmp_path, HIDE)
    base = analysis.base_graph(project.root, 'main')
    assert base is not None
    assert base.column('table.raw.pay', 'salary') is not None
    assert 'table.m.leak' not in base.nodes
    assert analysis.base_graph(project.root, 'no-such-ref') is None
    assert analysis.base_graph(tmp_path / 'elsewhere', 'main') is None


def test_a_key_of_an_entity_without_personal_data_counts_no_people(tmp_path: Path) -> None:
    """Counting distinct order ids counts orders: only a key of an entity whose own relation holds
    personal data, users with a name, proves a group of people."""
    defs = PAY.replace(', raw.users.name]', ']')
    project = build(tmp_path, {'models.sql': LINK + AGG_USERS, 'definitions.yml': defs})
    assert labels(project)['aggu.a'] == ['personal']


def test_the_gate_on_a_copy_keeps_the_marks_of_the_project_graph(gated: Project) -> None:
    """The MCP server's gate builds a copy of the graph from files alone: it keeps the warehouse
    tags of the project's own graph each time, and drops one the project's graph no longer has."""
    from konstruct.interfaces.mcp_server import gate_copy

    graph = gated.graph
    column = graph.column('table.raw.people', 'id')
    assert column is not None
    column.attrs['warehouse_tags'] = {'PII': ''}
    graph.write(gated.graph_path)
    (gated.root / 'models.sql').write_text(
        GATED['models.sql'] + 'create table mart.ids as select p.id from raw.people p;\n'
    )
    expected = (
        'sensitive-flow',
        'ids receives pii data from people.id in ids.id, and is not marked to hold it',
    )
    for _ in range(2):
        assert expected in [(f.rule, f.message) for f in gate_copy(Project(gated.root), 'HEAD').findings]
    del column.attrs['warehouse_tags']
    graph.write(gated.graph_path)
    assert expected not in [(f.rule, f.message) for f in gate_copy(Project(gated.root), 'HEAD').findings]


def test_a_rebuilt_table_keeps_no_masking_policy_of_the_last_build(gated: Project) -> None:
    """Rebuilding a table drops a masking policy set on its column, so a files-only gate does
    not take one the last build saw on a built table as still there."""
    graph = gated.graph
    for relation, policy in (('table.raw.people', 'mask'), ('table.crm.contacts', 'mask')):
        column = graph.column(relation, 'email')
        assert column is not None
        column.attrs['masking_policy'] = policy
    graph.write(gated.graph_path)
    (gated.root / 'models.sql').write_text(
        'create table crm.contacts as select p.id, p.email, upper(p.email) as shout from raw.people p;\n'
    )
    findings = _gate(Project(gated.root))
    assert (
        'unmasked-copy',
        'contacts.shout copies people.email, which mask masks, into a table without a masking policy',
    ) in findings
    kept = Graph.read(gated.graph_path)
    assert kept.column('table.raw.people', 'email').attrs['masking_policy'] == 'mask'  # type: ignore[union-attr]
    assert 'masking_policy' not in kept.column('table.crm.contacts', 'email').attrs  # type: ignore[union-attr]
    # The rebuilt copy is in clear until the warehouse says otherwise.
    assert 'contacts.email' in [u.column for u in sensitivity(kept).unmasked]


def test_a_materialised_view_holds_its_rows(tmp_path: Path) -> None:
    sql = 'create materialized view mart.mv as select p.id, p.email from raw.people p;\ncreate view mart.v as select p.id from raw.people p;\n'
    project = build(tmp_path, {'models.sql': sql})
    mv = project.graph.node_for_relation('mart.mv')
    assert mv is not None and mv.attrs['materialized_view'] is True
    actions = {h.relation: h.action for h in project.erasure('raw.people.id').holdings}
    assert actions['mv'] == 'rebuild' and actions['v'] == 'none'
    column = project.graph.column('table.raw.people', 'email')
    assert column is not None
    column.attrs['masking_policy'] = 'mask'
    assert [u.column for u in sensitivity(project.graph).unmasked] == ['mv.email']


def test_the_gate_stops_an_exposure(jaffle: Path, dbt_on_path: None) -> None:
    project = Project(jaffle)
    project.build()
    _repository(jaffle)
    customers = jaffle / 'models' / 'customers.sql'
    customers.write_text(
        customers.read_text().replace(
            'customers.last_name,', 'customers.last_name,\n        upper(customers.last_name) as shouted,'
        )
    )
    findings = project.gate(base='HEAD', run_dbt=True, warehouses=True).findings
    messages = [f.message for f in findings if f.rule == 'sensitive-flow']
    assert any(m.startswith('customer_dashboard shows personal data from customers.') for m in messages)
    assert any(m.startswith('customers receives personal data') and 'shouted' in m for m in messages)


def test_the_gate_stops_an_unmasked_copy(gated: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    (gated.root / 'models.sql').write_text(
        GATED['models.sql'] + 'create table crm.copied as select c.email from crm.contacts c;\n'
    )
    from konstruct.analysis import sensitivity as module

    real = module._Analysis.run

    def run(self: Any) -> Any:
        column = self.graph.column('table.crm.contacts', 'email')
        if column is not None:
            column.attrs['masking_policy'] = 'mask'
        return real(self)

    monkeypatch.setattr(module._Analysis, 'run', run)
    assert [r for r, _ in _gate(gated)] == ['unmasked-copy']


# The erasure map


@pytest.fixture(scope='module')
def jaffle_project(jaffle_built_graph: Path) -> Project:
    return Project(jaffle_built_graph)


def test_erasure_of_a_customer(jaffle_project: Project) -> None:
    result = jaffle_project.erasure('customer')
    assert result.key == 'customers.customer_id' and result.entity == 'customer'
    found = {h.relation: h for h in result.holdings}
    assert set(found) >= {
        'raw_customers',
        'stg_customers',
        'customers',
        'customer_ltv',
        'raw_orders',
        'stg_orders',
        'orders',
        'raw_payments',
        'stg_payments',
    }
    assert found['raw_customers'].action == 'delete' and found['raw_customers'].personal == [
        'first_name',
        'last_name',
    ]
    assert found['stg_customers'].action == 'none'  # a view
    assert found['customers'].action == 'rebuild'
    keys = {(k.column, k.how) for h in result.holdings for k in h.keys}
    assert ('orders.customer_id', 'foreign key') in keys or ('stg_orders.customer_id', 'foreign key') in keys
    # The payments of the customer's orders: rows the customer owns, one level down.
    assert found['raw_payments'].level == 1
    assert 'order' in {k.entity for k in found['raw_payments'].keys}
    assert result.exposures == ['customer_dashboard']
    text = str(result)
    assert text.startswith('Erasure map for customers.customer_id (customer)')
    assert 'Shown in: customer_dashboard' in text
    json.dumps(result.to_dict())


def test_erasure_levels_and_refs(jaffle_project: Project) -> None:
    shallow = jaffle_project.erasure('customer', levels=0)
    assert 'raw_payments' not in {h.relation for h in shallow.holdings}
    by_key = jaffle_project.erasure('orders.customer_id')
    assert by_key.entity == 'customer' and by_key.key == 'orders.customer_id'
    assert 'raw_customers' in {h.relation for h in by_key.holdings}
    by_table = jaffle_project.erasure('stg_customers')
    assert by_table.key == 'stg_customers.customer_id'
    with pytest.raises(QueryError, match='no key Konstruct knows'):
        jaffle_project.erasure('raw_customers')
    with pytest.raises(QueryError, match='erasure takes a key column'):
        jaffle_project.erasure('jaffle_shop_daily')


def test_erasure_follows_hashed_keys(tmp_path: Path) -> None:
    sql = (
        'create table crm.people as select p.id as person_id, p.email from raw.people p;\n'
        'create table mart.events as select md5(cast(c.person_id as varchar)) as person_hash, c.email from crm.people c;\n'
        'create table mart.counts as select count(c.person_id) as people from crm.people c;\n'
    )
    project = build(tmp_path, {'models.sql': sql})
    result = erasure(project.graph, 'crm.people.person_id')
    hows = {h.relation: [k.how for k in h.keys] for h in result.holdings}
    assert hows['events'] == ['hashed']
    assert 'counts' not in hows
    assert 'hashed, found by hashing it' in result.summary()
    assert result.holdings[0].path


def test_nothing_to_erase(tmp_path: Path) -> None:
    project = build(tmp_path, {'models.sql': 'create table a.b as select x.id from raw.x x'})
    result = project.erasure('raw.x.id')
    assert [h.relation for h in result.holdings] == ['b', 'x']
    assert result.entity is None and result.summary().startswith('2 relations hold the rows of x.id')


# Masked values in verify


def test_why_masks_sensitive_values(jaffle_project: Project) -> None:
    result = jaffle_project.why('customer_ltv.first_name', {'customer_id': '3'})
    assert result.value == '<masked: personal>'
    assert result.masked[0] == 'customer_ltv.first_name'
    assert 'raw_customers.first_name' in result.masked
    shown = [r.values for c in result.contributions for r in c.rows]
    assert shown and all(v.get('first_name') in (None, '<masked: personal>') for v in shown)
    assert any('are masked' in line for line in result.explanation())
    plain = jaffle_project.why('customer_ltv.lifetime_value', {'customer_id': '3'})
    assert not isinstance(plain.value, str)


def test_mask() -> None:
    assert mask(['personal', 'pii']) == '<masked: personal, pii>'
    assert mask([]) == '<masked: sensitive>'


# The command line


def test_the_commands(shop: Project, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['-C', str(shop.root), 'sensitivity']) == 0
    assert 'Sensitive data in the graph' in capsys.readouterr().out
    assert main(['-C', str(shop.root), 'sensitivity', '--strict']) == 1
    capsys.readouterr()
    assert main(['-C', str(shop.root), 'sensitivity', 'contacts.email', '--strict', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['subject'] == 'contacts.email' and data['format_version'] == 1
    assert main(['-C', str(shop.root), 'erasure', 'raw.people.id', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['key'] == 'people.id'


def test_scale(tmp_path: Path) -> None:
    """Sensitivity and the erasure map on a generated project of 2,000 models."""
    import time

    from tests.test_scale import make_project

    make_project(tmp_path, 2000)
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    graph = project.graph
    column = next(
        c
        for c in graph.nodes.values()
        if c.kind == 'column' and c.parent and graph.nodes[c.parent].kind in ('source', 'seed', 'table')
    )
    column.attrs['warehouse_tags'] = {'PII': ''}
    started = time.perf_counter()
    result = sensitivity(graph)
    elapsed = time.perf_counter() - started
    assert result.columns and elapsed < 10


def test_mutation_suggestions_quote_no_sensitive_value(shop: Project) -> None:
    from konstruct.analysis.mutation import _sensitive

    graph = shop.graph
    contacts = graph.node_for_relation('crm.contacts')
    hashed = graph.node_for_relation('mart.hashed')
    assert _sensitive(graph, contacts, 'email') and not _sensitive(graph, contacts, 'id')
    assert _sensitive(graph, contacts) and _sensitive(graph, hashed)
    assert not _sensitive(graph, None) and not _sensitive(graph, graph.node_for_relation('mart.active'))


def test_mutation_suggestions_follow_the_settings(tmp_path: Path) -> None:
    from konstruct.analysis.mutation import _sensitive

    graph = _built(
        tmp_path,
        'create table mart.copy as select t.email from raw.t t',
        {'warehouse_tags': {'restricted': ''}},
    )
    copy = graph.node_for_relation('mart.copy')
    assert not _sensitive(graph, copy, 'email', sensitive_columns(graph))
    assert _sensitive(graph, copy, 'email', sensitive_columns(graph, Settings(tags=('restricted',))))


def test_why_masks_each_share_of_a_sensitive_source(tmp_path: Path) -> None:
    """An aggregate over large groups is not sensitive, but each row's share of it is one
    person's value."""
    from tests.test_verify import make_project

    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Amount\n    definition: a.\n    sensitivity: personal\n    columns: [raw.orders.amount]\n'
        '  - name: Customer\n    kind: entity\n    definition: c.\n    columns: [raw.customers.id]\n'
        '  - name: Region\n    definition: r.\n    sensitivity: personal\n    columns: [raw.customers.region]\n'
    )
    project = make_project(
        tmp_path,
        'create table main.stats as select o.status, sum(o.amount) as total from raw.orders o '
        'group by o.status having count(distinct o.customer_id) >= 10;\n'
        'create table main.linked as select o.id, c.region from raw.orders o join raw.customers c '
        'on o.customer_id = c.id',
    )
    assert project.sensitivity('main.stats.total').columns == []
    result = project.why('main.stats.total', {'status': 'ok'}, limit=5)
    assert isinstance(result.value, (int, float)) or not str(result.value).startswith('<masked')
    assert result.contributions and all(
        v == '<masked: personal>' for c in result.contributions for v in c.values
    )
    assert result.aggregates[0].total == '<masked: personal>'
    assert result.masked[0] == "stats.total (each row's share)"
    assert all(r.values.get('amount') == '<masked: personal>' for c in result.contributions for r in c.rows)


def test_erasure_finds_cast_keys_copies_without_the_key_and_joins(tmp_path: Path) -> None:
    sql = (
        'create table stg.people as select cast(p.id as varchar) as person_id, p.email from raw.people p;\n'
        'create table mart.people as select s.person_id, s.email from stg.people s;\n'
        'create table mart.emails as select distinct s.email from stg.people s;\n'
        'create table mart.hashed as select md5(s.person_id) as hk, s.email from stg.people s;\n'
        'create table mart.counted as select count(s.person_id) as n from stg.people s;\n'
        'create table mart.orders as select o.order_id, o.person_id, o.amount from raw.orders o '
        'join stg.people s on o.person_id = s.person_id;\n'
    )
    defs = 'version: 1\nterms:\n  - name: Contact\n    definition: c.\n    sensitivity: personal\n    columns: [raw.people.email]\n'
    project = build(tmp_path, {'models.sql': sql, 'definitions.yml': defs})
    result = project.erasure('mart.people.person_id')
    hows = {h.relation: [(k.column, k.how) for k in h.keys] for h in result.holdings}
    # Three relations are called people: each is shown by its warehouse name.
    assert hows['raw.people'] == [('raw.people.id', 'transformed')]
    assert hows['stg.people'] == [('stg.people.person_id', 'copy')]
    assert hows['emails'] == [('emails.email', 'personal data')]
    assert hows['hashed'] == [('hashed.hk', 'hashed')]
    assert hows['raw.orders'] == [('raw.orders.person_id', 'foreign key')]
    assert hows['mart.orders'] == [('mart.orders.person_id', 'copy')]
    assert 'counted' not in hows
    assert {h.relation: h.action for h in result.holdings}['raw.people'] == 'delete'


def test_a_reading_of_a_relation_reads_its_columns() -> None:
    """A query that reads a relation twice names each reading (``"orders#2"``): a count over one
    reading of a key counts that key."""
    import sqlglot
    from sqlglot import exp

    from konstruct.analysis.sensitivity import _reads

    column = sqlglot.parse_one('main."orders#1/2".id')
    assert isinstance(column, exp.Column)
    assert _reads(column, 'db.main.orders', 'id')
    assert not _reads(column, 'db.main.order_lines', 'id')
