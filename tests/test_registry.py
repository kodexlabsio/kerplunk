"""Ontology packs: shared ontologies for common source schemas, bound when the graph has them."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.engine.config import ConfigError, load_config
from konstruct.semantics import registry
from konstruct.semantics.registry import find, load_packs, suffixes

ROOT = Path(__file__).parent.parent
DOCS = ROOT / 'docs'
CHARGE = (
    'id varchar, amount bigint, amount_refunded bigint, captured boolean, paid boolean, '
    'currency varchar, livemode boolean, customer_id varchar'
)
STRIPE = f"""
create schema stripe;
create table stripe.charge ({CHARGE});
create table stripe.customer (id varchar, email varchar, delinquent boolean, livemode boolean);
create table stripe.invoice (id varchar, customer_id varchar, amount_due bigint);
"""


def generator() -> ModuleType:
    """The script that generates the packs, loaded as a module."""
    if 'generate_packs' not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            'generate_packs', ROOT / '.kodex' / 'scripts' / 'generate_packs.py'
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules['generate_packs'] = module
        spec.loader.exec_module(module)
    return sys.modules['generate_packs']


def warehouse(root: Path, *scripts: str, name: str = 'warehouse') -> Path:
    path = root / f'{name}.duckdb'
    con = duckdb.connect(str(path))
    for script in scripts:
        con.execute(script)
    con.close()
    return path


def test_every_pack_loads_and_names_real_tables() -> None:
    packs = {p.name: p for p in load_packs()}
    assert {
        'ga4',
        'hubspot',
        'salesforce',
        'shopify',
        'stripe',
        'zendesk',
        'jira',
        'google_ads',
    } <= set(packs)
    assert len(packs) >= 250
    for pack in packs.values():
        assert pack.match and pack.terms and pack.description and pack.schemas and pack.title, pack.name
        names = [str(t['name']).lower() for t in pack.terms]
        assert len(names) == len(set(names)), pack.name
        for term in pack.terms:
            assert term['name'] and term['definition'] and term['columns'] and term['kind'], (pack.name, term)
            assert 'status' not in term
            assert all(str(c).count('.') == 1 for c in term['columns']), (pack.name, term)
        for rel in pack.relationships:
            assert rel['from'].count('.') == 1 and rel['to'].count('.') == 1, (pack.name, rel)


def test_the_index_holds_every_pack_file() -> None:
    # Rebuild it with: uv run python .kodex/scripts/generate_packs.py --index
    assert registry.read_index() == registry.build_index()


def test_every_match_block_picks_out_its_own_pack() -> None:
    packs = load_packs()
    for pack in packs:
        for block in pack.match:
            tables = {spec.split('|')[0]: set(columns) for spec, columns in block.items() if '*' not in spec}
            if len(tables) != len(block):
                continue  # GA4's daily tables are matched by pattern
            # A block of generic table names alone needs a schema named for its source.
            schema = f'db.{pack.name}' if registry.needs_name(block) else 'db.raw'
            found = find(packs, {schema: tables}, {schema: set(tables)})
            assert [m.pack.name for m in found] == [pack.name], (pack.name, sorted(tables))


def test_every_match_block_requires_columns_that_are_not_generic() -> None:
    for pack in load_packs():
        for block in pack.match:
            distinctive = {c for columns in block.values() for c in columns} - registry.GENERIC_COLUMNS
            assert len(distinctive) >= 2, (pack.name, block)


def test_a_block_of_generic_tables_needs_the_schemas_name() -> None:
    pack = registry.read_pack(
        {
            'name': 'desk',
            'schemas': ['*desk*'],
            'match': [{'tickets': ['id', 'via_channel', 'ticket_form_id']}],
            'terms': [
                {
                    'name': 'Desk ticket',
                    'kind': 'entity',
                    'definition': 'A ticket.',
                    'columns': ['tickets.id'],
                }
            ],
        },
        'desk',
        'test',
    )
    tables = {'tickets': {'id', 'via_channel', 'ticket_form_id'}}
    assert find([pack], {'db.app': tables}, {'db.app': {'tickets'}}) == []
    assert [m.schema for m in find([pack], {'db.desk': tables})] == ['db.desk']


def test_the_docs_give_the_packs_numbers() -> None:
    data = registry.read_index()
    entities = sum(1 for p in data for t in p['terms'] if t.get('kind') == 'entity')
    measures = sum(1 for p in data for t in p['terms'] if t.get('kind') == 'measure')
    relationships = sum(len(p['relationships']) for p in data)
    guide = (DOCS / 'guide' / 'ontology.md').read_text(encoding='utf-8')
    assert f'Konstruct ships {len(data)} ontology packs' in guide
    assert f'{entities:,} entities, {measures:,} measures and {relationships:,} relationships' in guide
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert f'{len(data)} ontology packs for the source systems' in readme
    listing = (DOCS / 'guide' / 'ontology-packs.md').read_text(encoding='utf-8')
    assert all(f'| `{p["name"]}` |' in listing for p in data)


ZENDESK_FIVETRAN = """
create schema zendesk;
create table zendesk.ticket (id bigint, via_channel varchar, ticket_form_id bigint, subject varchar,
    organization_id bigint, requester_id bigint);
create table zendesk.organization (id bigint, name varchar, domain_names varchar, organization_fields varchar);
create table zendesk.user (id bigint, name varchar, ticket_restriction varchar, suspended boolean);
"""
ZENDESK_SINGER = """
create schema tap_zendesk;
create table tap_zendesk.tickets (id bigint, allow_attachments boolean, allow_channelback boolean,
    subject varchar, organization_id bigint);
create table tap_zendesk.organizations (id bigint, name varchar, domain_names varchar,
    organization_fields varchar);
"""


@pytest.mark.parametrize(('script', 'ticket'), [(ZENDESK_FIVETRAN, 'ticket'), (ZENDESK_SINGER, 'tickets')])
def test_a_generated_pack_binds_its_entities_and_relationships(
    tmp_path: Path, script: str, ticket: str
) -> None:
    # Fivetran's singular tables and a Singer tap's plural ones are the same pack's.
    warehouse(tmp_path, script)
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    term = graph.nodes['term.zendesk_support_ticket']
    assert (term.attrs['registry'], term.attrs['status']) == ('zendesk', 'draft')
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges(term.id, 'defines')] == [
        f'{ticket}.id'
    ]
    ontology = project.ontology()
    rel = next(r for r in ontology.relationships if r.source == 'zendesk_support_ticket')
    assert rel.target == 'zendesk_support_organisation' and rel.cardinality is None
    assert rel.evidence == ['ontology pack zendesk']


def _pack(
    name: str, match: dict[str, list[str]], optional: Sequence[str] = (), schemas: Sequence[str] = ()
) -> registry.Pack:
    tables = [*match, *optional]
    terms = [
        {'name': f'{name} {t}', 'kind': 'entity', 'definition': f'A {t}.', 'columns': [f'{t}.id']}
        for t in tables
    ]
    data = {'name': name, 'match': [match], 'terms': terms, 'schemas': list(schemas)}
    return registry.read_pack(data, name, 'test')


def test_every_pack_that_matches_binds_and_contention_has_a_tie_rule() -> None:
    charges = _pack('charges', {'charges': ['id', 'livemode', 'captured']}, ['users'])
    desk = _pack('desk', {'brands': ['id', 'brand_url', 'subdomain']}, ['users', 'groups'])
    people = _pack('people', {'groups': ['id', 'team_kind'], 'rosters': ['id', 'roster_kind', 'captain_id']})
    schema = {
        'charges': {'id', 'livemode', 'captured'},
        'brands': {'id', 'brand_url', 'subdomain'},
        'groups': {'id', 'team_kind'},
        'rosters': {'id', 'roster_kind', 'captain_id'},
        'users': {'id'},
    }
    found = find([charges, desk, people], {'db.raw': schema}, {'db.raw': {'charges'}})
    # Packs that hold in different tables all bind. A table one pack requires is not another's
    # optional table, and an optional table two bound packs name alike goes to neither.
    assert {m.pack.name: m.tables for m in found} == {
        'charges': {'charges': 'charges'},
        'desk': {'brands': 'brands'},
        'people': {'groups': 'groups', 'rosters': 'rosters'},
    }
    # Packs that hold in the same tables contend: the one the schema is named for wins, then
    # the one whose block requires more tables, then more columns, then the first by name.
    one = _pack('one', {'account_plan': ['id', 'plan_tier', 'seats']}, schemas=['*one*'])
    two = _pack('two', {'account_plan': ['id', 'plan_tier', 'seats']}, schemas=['*two*'])
    plan = {
        'account_plan': {'id', 'plan_tier', 'seats', 'renewal_term'},
        'seat_grant': {'id', 'seat_kind', 'granted_by'},
    }
    assert [m.pack.name for m in find([one, two], {'db.two': plan}, {'db.two': {'seat_grant'}})] == ['two']
    wider = _pack('wider', {'account_plan': ['id', 'plan_tier'], 'seat_grant': ['id', 'seat_kind']})
    assert [m.pack.name for m in find([one, two, wider], {'db.raw': plan}, {'db.raw': {'seat_grant'}})] == [
        'wider'
    ]
    more = _pack('more', {'account_plan': ['id', 'plan_tier', 'seats', 'renewal_term']})
    assert [m.pack.name for m in find([two, one, more], {'db.raw': plan}, {'db.raw': {'seat_grant'}})] == [
        'more'
    ]
    assert [m.pack.name for m in find([two, one], {'db.raw': plan}, {'db.raw': {'seat_grant'}})] == ['one']


def test_three_sources_landed_into_one_schema_each_get_their_pack(tmp_path: Path) -> None:
    # One Airbyte destination schema holding streams from Stripe, Zendesk and HubSpot.
    a = ', _airbyte_raw_id varchar, _airbyte_extracted_at timestamp'
    warehouse(
        tmp_path,
        'create schema airbyte;'
        f'create table airbyte.charges ({CHARGE}, pending_webhooks bigint{a});'
        f'create table airbyte.customers (id varchar, email varchar, delinquent boolean, livemode boolean{a});'
        f'create table airbyte.tickets (id bigint, allow_attachments boolean, allow_channelback boolean,'
        f' brand_id bigint, organization_id bigint{a});'
        f'create table airbyte.brands (id bigint, brand_url varchar, has_help_center boolean, subdomain varchar{a});'
        f'create table airbyte.organizations (id bigint, name varchar{a});'
        f'create table airbyte.email_events (id varchar, appname varchar, causedby varchar, cc varchar{a});',
    )
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    registries = {n.attrs.get('registry') for n in graph.nodes.values() if n.kind == 'term'}
    assert {'stripe', 'zendesk', 'hubspot'} <= registries
    assert graph.nodes['term.zendesk_support_ticket'].attrs['registry'] == 'zendesk'
    assert graph.nodes['term.stripe_charge'].attrs['registry'] == 'stripe'
    rel = next(
        r
        for r in project.ontology().relationships
        if r.source == 'zendesk_support_ticket' and r.target == 'zendesk_support_brand'
    )
    assert rel.evidence == ['ontology pack zendesk']


def test_a_hand_written_term_wins_over_a_generated_one_in_any_naming() -> None:
    own = registry.read_pack(
        {'name': 's', 'match': {'charge': ['id']}, 'terms': [{'name': 'Mine', 'columns': ['charge.amount']}]},
        's',
        'hand-written',
    )
    generated = registry.read_pack(
        {
            'name': 's',
            'match': {'charges': ['id']},
            'terms': [
                {'name': 'Theirs', 'columns': ['charges|charge.amount']},
                {'name': 'Other', 'columns': ['charges|charge.amount_refunded']},
            ],
        },
        's',
        'generated',
    )
    assert [t['name'] for t in registry.merge(own, generated).terms] == ['Mine', 'Other']


def test_a_schema_matches_by_its_tables_columns_and_name_or_loader() -> None:
    (stripe,) = load_packs(['stripe'])
    charges = {'id', 'amount', 'amount_refunded', 'captured', 'paid', 'currency', 'livemode', 'customer'}
    customers = {'id', 'delinquent', 'livemode'}
    schemas = {
        'db.stripe': {'charges': charges, 'customers': customers},
        'db.app': {'charges': charges, 'customers': customers},
        'db.billing': {'charges': charges, 'customers': customers},
        'db.other': {'charge': {'id'}},
    }
    matches = find([stripe], schemas, {'db.billing': {'charges'}})
    assert [m.schema for m in matches] == ['db.billing', 'db.stripe']
    assert matches[1].tables == {'charge': 'charges', 'customer': 'customers'}
    assert suffixes(matches) == {'db.billing': 'billing', 'db.stripe': 'stripe'}


def test_the_build_binds_a_matching_pack(tmp_path: Path) -> None:
    warehouse(tmp_path, STRIPE)
    (tmp_path / 'revenue.sql').write_text(
        'create table warehouse.main.revenue as select customer_id, sum(amount) as gross '
        'from stripe.charge c join stripe.customer u on c.customer_id = u.id group by 1'
    )
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    term = graph.nodes['term.stripe_charge_amount']
    assert (term.attrs['registry'], term.attrs['status'], term.origins) == ('stripe', 'draft', ['registry'])
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges(term.id, 'defines')] == [
        'charge.amount'
    ]
    assert 'term.stripe_subscription' not in graph.nodes  # the schema has no subscription table
    ontology = project.ontology()
    assert {'stripe_charge', 'stripe_customer', 'stripe_invoice'} <= {e.name for e in ontology.entities}
    assert not graph.warnings
    # A pack's key is not tested, so the join gives no cardinality.
    assert all(r.cardinality is None for r in ontology.relationships)
    assert {'input': 'ontology pack', 'name': 'stripe', 'detail': 'warehouse.stripe: 5 terms'} in graph.meta[
        'sources'
    ]


def test_an_applications_own_tables_match_nothing(tmp_path: Path) -> None:
    warehouse(
        tmp_path,
        'create table main.orders (id int, total_price double, customer_id int);'
        'create table main.customers (id int, email varchar);'
        f'create table main.charges ({CHARGE});'
        'create table main.customer (id varchar, delinquent boolean, livemode boolean);',
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not [n for n in graph.nodes.values() if n.attrs.get('registry')]


def test_a_projects_own_model_of_the_source_wins(tmp_path: Path) -> None:
    warehouse(tmp_path, STRIPE)
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n'
        '  - name: Stripe charge amount\n    definition: Ours.\n    columns: [stripe.charge.amount]\n'
        '  - name: Payer\n    kind: entity\n    definition: Who pays.\n    columns: [stripe.customer.id]\n'
        '  - name: Bill\n    synonyms: [Stripe invoice]\n    definition: A bill.\n    columns: [stripe.invoice.id]\n'
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert graph.nodes['term.stripe_charge_amount'].description == 'Ours.'
    assert 'term.stripe_customer' not in graph.nodes and 'term.stripe_invoice' not in graph.nodes
    assert not [w for w in graph.warnings if 'ontology pack' in w or 'also a name' in w or 'rename' in w]


def test_packs_can_be_chosen(tmp_path: Path) -> None:
    warehouse(tmp_path, STRIPE)
    (tmp_path / 'konstruct.toml').write_text('[ontologies]\npacks = []\n')
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not [n for n in graph.nodes.values() if n.attrs.get('registry')]
    (tmp_path / 'konstruct.toml').write_text('[ontologies]\npacks = ["strip"]\n')
    with pytest.raises(ConfigError, match="no pack named 'strip'"):
        load_config(tmp_path)


def test_the_same_pack_in_two_catalogues_and_ga4_daily_tables(tmp_path: Path) -> None:
    warehouse(tmp_path, STRIPE, name='prod')
    warehouse(tmp_path, STRIPE, name='dev')
    warehouse(
        tmp_path,
        'create schema analytics_123;'
        + ''.join(
            f'create table analytics_123.{t} (event_name varchar, event_timestamp bigint, user_pseudo_id varchar);'
            for t in (
                'events_20261101',
                'events_20261102',
                'events_intraday_20261103',
                'events_fresh_20261103',
            )
        ),
        name='ga',
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert (
        'term.stripe_customer_prod_stripe' in graph.nodes and 'term.stripe_customer_dev_stripe' in graph.nodes
    )
    assert not [w for w in graph.warnings if 'same id' in w or 'also a name' in w]
    event = graph.nodes['term.ga4_event_name']
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges(event.id, 'defines')] == [
        'events_20261102.event_name'
    ]


def test_repositories_that_share_a_warehouse_share_its_pack_terms(tmp_path: Path) -> None:
    shared = warehouse(tmp_path, STRIPE)
    main, other = tmp_path / 'main', tmp_path / 'marketing'
    for root in (main, other):
        root.mkdir()
        (root / 'konstruct.toml').write_text(f'[[warehouse]]\ntype = "duckdb"\npath = "{shared}"\n')
    (main / 'konstruct.toml').write_text(
        'repositories = ["../marketing"]\n' + (main / 'konstruct.toml').read_text()
    )
    graph = Project(main).build(run_dbt=False).graph
    assert [n for n in graph.nodes if n.startswith('term.stripe_customer')] == ['term.stripe_customer']
    assert not [w for w in graph.warnings if 'more than one repository' in w]


def test_an_application_database_landed_by_a_loader_matches_nothing(tmp_path: Path) -> None:
    # An application's own tables, replicated by Fivetran: generic names and columns only.
    f = ', _fivetran_synced timestamp'
    warehouse(
        tmp_path,
        'create schema app_db;'
        f'create table app_db.users (id bigint, email varchar, name varchar, created_at timestamp, role varchar{f});'
        f'create table app_db.accounts (id bigint, name varchar, type varchar, plan varchar{f});'
        f'create table app_db.orders (id bigint, user_id bigint, total_price double, status varchar{f});'
        f'create table app_db.events (id bigint, user_id bigint, name varchar, properties json{f});'
        f'create table app_db.tickets (id bigint, subject varchar, status varchar, priority varchar,'
        f' requester_id bigint, organization_id bigint{f});'
        f'create table app_db.organizations (id bigint, name varchar, domain varchar{f});'
        f'create table app_db.projects (id bigint, name varchar, created_at timestamp, object varchar{f});'
        f'create table app_db.schedule (id bigint, start_time timestamp, end_time timestamp, name varchar{f});'
        f'create table app_db.invoices (id bigint, customer_id bigint, amount_due bigint, currency varchar{f});',
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert (
        graph.nodes['app_db.users'].attrs.get('loaded_by') == 'fivetran'
        if 'app_db.users' in graph.nodes
        else True
    )
    assert not [n for n in graph.nodes.values() if n.attrs.get('registry')]


def _origin(loader: str, *streams: Any) -> Any:
    gp = generator()
    return gp.Origin(
        loader, 'demo', 'https://example.com/demo', 'MIT', 'abc', streams={s.name: s for s in streams}
    )


def test_history_report_and_composite_tables_and_stray_keys_are_not_entities() -> None:
    gp = generator()
    fivetran = _origin(
        'fivetran',
        gp.Stream('campaign_history', columns={'id': 'unknown', 'updated_at': 'unknown', 'name': 'unknown'}),
        gp.Stream(
            'ad_group_stats',
            columns={'id': 'unknown', 'date': 'unknown', 'clicks': 'unknown'},
            descriptions={'clicks': 'The number of clicks.'},
        ),
        gp.Stream('transactionline', columns={'id': 'unknown', 'transaction': 'unknown'}, composite=True),
        gp.Stream(
            'account', columns={'id': 'unknown', '_fivetran_active': 'unknown', '_fivetran_start': 'unknown'}
        ),
        gp.Stream('legacy_widget', columns={'id': 'unknown'}, description='Deprecated: use widget instead.'),
        gp.Stream('ticket', columns={'id': 'unknown', 'organization_id': 'unknown'}),
    )
    singer = _origin(
        'singer',
        gp.Stream('subscribers', key=['email'], columns={'email': 'string', 'list_id': 'string'}),
        gp.Stream('daily_usage', key=['id'], columns={'id': 'string', 'cost': 'number'}),
        gp.Stream('organizations', key=['id'], columns={'id': 'integer', 'name': 'string'}),
        gp.Stream(
            'phone_calls', key=['phonecallid'], columns={'phonecallid': 'string', 'duration': 'integer'}
        ),
    )
    tables = gp.tables_of([fivetran, singer])
    pack = gp.pack_for('demo', 'Demo', [fivetran, singer], tables)
    entities = {t['name']: t['columns'] for t in pack['terms'] if t['kind'] == 'entity'}
    assert entities == {
        'Demo organisation': ['organizations.id'],
        'Demo phone call': ['phone_calls.phonecallid'],
        'Demo ticket': ['ticket.id'],
    }
    measures = {t['name'] for t in pack['terms'] if t['kind'] == 'measure'}
    assert measures == {'Demo ad group stats clicks', 'Demo daily usage cost', 'Demo phone call duration'}
    assert pack['relationships'] == [{'from': 'ticket.organization_id', 'to': 'organizations.id'}]


def test_a_grain_of_several_columns_or_a_row_per_one_thing_per_another_is_not_an_entity(
    tmp_path: Path,
) -> None:
    gp = generator()
    # Workday's business plans: one row per plan, per company, per fiscal period, as the source
    # says with commas, and a staging model whose grain is two columns and source_relation.
    repo = tmp_path / 'dbt_demo'
    (repo / 'models').mkdir(parents=True)
    (repo / 'models' / 'stg_demo__plan.sql').write_text("select * from {{ source('demo', 'plan') }}")
    (repo / 'models' / 'stg_demo__worktag.sql').write_text("select * from {{ source('demo', 'worktag') }}")
    (repo / 'models' / 'stg_demo.yml').write_text(
        'models:\n'
        '  - name: stg_demo__plan\n'
        '    description: The grain of this model is plan_id, plan_index, source_relation.\n'
        '  - name: stg_demo__worktag\n'
        '    description: The grain of this model is worktag_id, source_relation.\n'
    )
    _, composite = gp.staging(repo)
    assert composite == {'plan'}
    assert gp.NOT_ONE_ROW.search('The header of a plan. One row per plan, per company, per fiscal period.')
    origin = _origin(
        'fivetran',
        gp.Stream(
            'business_plan',
            columns={'id': 'unknown', 'index': 'unknown'},
            description='One row per plan, per company, per fiscal period.',
        ),
        gp.Stream(
            'worktag', columns={'id': 'unknown', 'cost': 'unknown'}, descriptions={'cost': 'The cost.'}
        ),
    )
    pack = gp.pack_for('demo', 'Demo', [origin], gp.tables_of([origin]))
    assert [t['name'] for t in pack['terms'] if t['kind'] == 'entity'] == ['Demo worktag']


def test_seeds_type_the_columns_fivetran_leaves_untyped(tmp_path: Path) -> None:
    gp = generator()
    seeds = tmp_path / 'dbt_demo' / 'integration_tests' / 'seeds'
    seeds.mkdir(parents=True)
    rows = ['id,duration,amount,price', '1,once,"3,000.75",1.5', '2,forever,12,2', '3,repeating,9,39.01a']
    (seeds / 'demo_coupon_data.csv').write_text('\n'.join(rows) + '\n')
    columns, repeats, strings = gp.seeds(tmp_path / 'dbt_demo')
    assert columns == {'demo_coupon': {'id', 'duration', 'amount', 'price'}}
    assert repeats == {}
    # Text in most rows is text; a malformed number in one row of three is a test of a cast.
    assert strings == {'demo_coupon': {'duration'}}
    stream = gp.Stream('coupon', source_name='coupon')
    assert gp._sample(stream, strings, 'demo_analytics') == {'duration'}


def test_measures_are_quantities() -> None:
    gp = generator()
    fivetran = _origin(
        'fivetran',
        gp.Stream(
            'invoice',
            columns=dict.fromkeys(('id', 'amount', 'tax', 'discount', 'total', 'balance', 'fee'), 'unknown'),
            descriptions={
                'amount': 'The amount due, in cents.',
                'tax': 'Boolean that says whether tax applies.',
                'discount': 'Indicates if a discount was given.',
                'total': 'Deprecated; use amount.',
                'balance': 'The balance, or returns null when unknown.',
                'fee': 'Set to true when a fee applies.',
            },
        ),
    )
    singer = _origin(
        'singer',
        gp.Stream(
            'invoices',
            key=['id'],
            columns={
                'id': 'string',
                'amount': 'number',
                'price': 'string',
                # Settings, not quantities.
                'max_amount': 'number',
                'default_quantity': 'integer',
                'interval_count': 'integer',
                'timeout_seconds': 'integer',
                'retention_count': 'integer',
                'limit_count': 'integer',
            },
        ),
        # A row for each version of a plan repeats its figures.
        gp.Stream(
            'plan_history', key=['id'], columns={'id': 'string', 'price': 'number', 'budget': 'number'}
        ),
    )
    pack = gp.pack_for('demo', 'Demo', [fivetran, singer], gp.tables_of([fivetran, singer]))
    measures = {t['name']: t['definition'] for t in pack['terms'] if t['kind'] == 'measure'}
    assert measures == {'Demo invoice amount': 'The amount due, in cents.'}
    # Settings, by their names or their descriptions, and a column a seed holds text in.
    settings = _origin(
        'fivetran',
        gp.Stream(
            'workspace',
            columns={
                'id': 'unknown',
                'rounding_minutes': 'unknown',
                'delay_seconds': 'unknown',
                'working_hours_in_minutes': 'unknown',
                'duration': 'unknown',
                'budget': 'unknown',
                'coupon_duration': 'string',
                'visit_count': 'unknown',
            },
            descriptions={
                'rounding_minutes': 'The minutes entries are rounded to.',
                'delay_seconds': 'Seconds before the trigger runs.',
                'working_hours_in_minutes': 'The working day, in minutes.',
                'duration': 'Default length of the interview, in minutes.',
                'budget': 'Budget settings for the ad group.',
                'coupon_duration': 'The duration of the coupon.',
                'visit_count': 'The number of visits. If the Page setting Show map is off, this is off too.',
            },
        ),
    )
    pack = gp.pack_for('demo', 'Demo', [settings], gp.tables_of([settings]))
    assert [t['columns'] for t in pack['terms'] if t['kind'] == 'measure'] == [['workspace.visit_count']]
    # The columns of the packs that are not quantities: text, as their seeds show, or settings.
    packs = {p.name: p for p in load_packs()}
    not_measures = {
        'stripe': {'coupon.duration'},
        'youtube_analytics': {'video.content_details_duration'},
        'apple_store': {
            'sales_subscription_event_summary.marketing_opt_in_duration',
            'sales_subscription_event_summary.standard_subscription_duration',
            'sales_subscription_event_summary.subscription_offer_duration',
            'sales_subscription_summary.standard_subscription_duration',
        },
        'linkedin_pages': {'post_content.poll_settings_duration', 'share_content.poll_settings_duration'},
        'aws_cloud_cost': {'aws_cost_report.discount'},
        'servicenow': {'task_sla.duration', 'task_sla.business_duration'},
        'greenhouse': {'job_interview.duration', 'job_interviews.duration'},
        'toggl': {'workspaces.rounding_minutes', 'workspaces.working_hours_in_minutes'},
        'mailchimp': {'automation_email.delay_amount'},
        'calltrackingmetrics': {'trigger.delay_seconds'},
        'nextdoor': {'ad_groups.budget'},
        'clerk': {'users.lockout_expires_in_seconds'},
        'campfire': {'contracts.auto_renew_duration'},
    }
    for name, columns in not_measures.items():
        held = {
            f'{table}.{ref.rsplit(".", 1)[1]}'
            for term in packs[name].terms
            if term['kind'] == 'measure'
            for ref in term['columns']
            for table in ref.rsplit('.', 1)[0].split('|')
        }
        assert not held & columns, (name, held & columns)
    # A few malformed values in a seed of numbers leave its column a number.
    amazon = {
        c for t in packs['amazon_selling_partner'].terms if t['kind'] == 'measure' for c in t['columns']
    }
    assert {'orders.order_total_amount', 'order_item.item_price_amount'} <= amazon


@pytest.mark.parametrize(
    ('plural', 'one'),
    [
        ('warehouses', 'warehouse'),
        ('statuses', 'status'),
        ('status', 'status'),
        ('categories', 'category'),
        ('matches', 'match'),
        ('addresses', 'address'),
        ('sizes', 'size'),
        ('responses', 'response'),
        ('purchases', 'purchase'),
        ('taxes', 'tax'),
        ('macros', 'macro'),
        ('ads', 'ad'),
        ('people', 'person'),
        ('news', 'news'),
        ('analyses', 'analysis'),
    ],
)
def test_singular(plural: str, one: str) -> None:
    assert generator().singular(plural) == one


def test_the_text_of_a_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    gp = generator()
    assert [gp.article(w) for w in ('user', 'hour', 'SMS', 'URL', 'update', 'one-off', 'apple', 'SENT')] == [
        'a',
        'an',
        'an',
        'a',
        'an',
        'a',
        'an',
        'a',
    ]
    assert gp.prose('a hour in a organization -- organized by the Help Center at Hubspot') == (
        'An hour in an organisation, organised by the Help Center at HubSpot.'
    )
    # A sentence that lists a field's values keeps their spelling, as quoted text does.
    assert gp.prose("One of 'canceled' or 'active'.") == "One of 'canceled' or 'active'."
    assert gp.prose('The "color" field holds the color.') == 'The "color" field holds the colour.'
    # Plural words inside a name stay plural; a phrase keeps its last word plural too.
    assert gp.human('organizations_clusters_token') == 'organisations clusters token'
    assert gp.human('futures_exchange') == 'futures exchange'
    assert gp.singular_name('price_to_earnings') == 'price_to_earnings'
    assert gp.singular_name('return_on_assets') == 'return_on_assets'
    assert gp.singular('employmentstatuses') == 'employmentstatus'
    assert gp.human('channel_basic_a_2') == 'channel basic A2'
    assert gp.human('sla_policy_vat_crm') == 'SLA policy VAT CRM'
    # Words run together are told apart by a word list, the likeliest split winning.
    words = (
        'device status info employee asset user log cost center campaign extended spend row bounce statistic'
    )
    monkeypatch.setattr(gp, 'DICTIONARY', set(words.split()) | {'de', 'vice'})
    monkeypatch.setattr(gp, 'VOCABULARY', Counter(words.split() * 10))
    assert gp.human('devicestatusinfo') == 'device status info'
    assert gp.human('employeeasset') == 'employee asset'
    assert gp.human('userlog_costcenter') == 'user log cost centre'
    assert gp.human('extendedspendrow_campaignid') == 'extended spend row campaign ID'
    assert gp.human('bouncestatistic') == 'bounce statistic'
    assert gp.measure_name('campaign_budget', 'budget') == 'campaign budget value'
    assert gp.measure_name('order_item', 'item_price_amount') == 'order item price amount'
    origin = _origin('singer', gp.Stream('ad_groups', key=['ad_group_id'], columns={'ad_group_id': 'string'}))
    (table,) = gp.tables_of([origin]).values()
    assert gp.entity_definition(table, 'Demo', 'ad_group_id') == (
        'An ad group in Demo, one row per ad group, keyed on its ad group ID.'
    )


def test_every_word_of_a_term_name_is_a_word_or_an_acronym() -> None:
    # The generator writes the words of the names that its word list knows, so a name made of
    # words run together, or of a token no list knows, is not in it.
    words = set((ROOT / 'tests' / 'fixtures' / 'pack_words.txt').read_text(encoding='utf-8').split())
    for pack in registry.read_index():
        for term in pack['terms']:
            for word in re.findall(r'[A-Za-z]+', term['name'].removeprefix(pack['title'])):
                assert word.lower() in words or word.isupper(), (pack['name'], term['name'], word)


def test_the_most_specific_schema_name_wins_and_history_mode_tables_are_not_entities(tmp_path: Path) -> None:
    broad = _pack('broad', {'account': ['id', 'account_source', 'billing_city']}, schemas=['*sales*'])
    narrow = _pack(
        'narrow', {'account': ['id', 'account_source', 'billing_city']}, schemas=['*sales_history*']
    )
    tables = {'account': {'id', 'account_source', 'billing_city'}}
    assert [m.pack.name for m in find([broad, narrow], {'db.sales_history': tables})] == ['narrow']
    assert [m.pack.name for m in find([broad, narrow], {'db.sales': tables})] == ['broad']
    # Fivetran's Salesforce history mode, as its package's seeds land it: a row for each version.
    seeds = ROOT / 'tests' / 'fixtures' / 'salesforce_history.sql'
    warehouse(tmp_path, seeds.read_text(encoding='utf-8'))
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    # The pack matches, but binds neither entities nor measures to versioned tables.
    packs = {s['name']: s['detail'] for s in graph.meta['sources'] if s['input'] == 'ontology pack'}
    assert packs.get('salesforce', '').startswith('warehouse.salesforce_history:')
    bound = [n for n in graph.nodes.values() if n.kind == 'term' and n.attrs.get('registry')]
    assert not [n for n in bound if n.attrs.get('kind') in ('entity', 'measure')]
    assert not [e for e in project.ontology().entities if 'salesforce' in e.name]


def test_shopify_landed_by_airbyte_in_a_schema_of_any_name(tmp_path: Path) -> None:
    # Airbyte's Shopify streams, with their own column names, in a schema not named for Shopify.
    a = '_airbyte_raw_id varchar, _airbyte_extracted_at timestamp'
    orders = (
        'id bigint, name varchar, email varchar, total_price varchar, subtotal_price varchar, total_tax varchar,'
        ' total_discounts varchar, financial_status varchar, fulfillment_status varchar, taxes_included boolean,'
        ' currency varchar, customer_id bigint, browser_ip varchar, cart_token varchar, checkout_token varchar,'
        ' order_number bigint, processed_at timestamp, created_at timestamp, updated_at timestamp'
    )
    customers = (
        'id bigint, email varchar, first_name varchar, last_name varchar, verified_email boolean,'
        ' orders_count bigint, total_spent varchar, tax_exempt boolean, created_at timestamp'
    )
    warehouse(
        tmp_path,
        f'create schema airbyte_raw; create table airbyte_raw.orders ({orders}, {a});'
        f'create table airbyte_raw.customers ({customers}, {a});',
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert graph.nodes['term.shopify_order'].attrs['registry'] == 'shopify'
    assert graph.nodes['term.shopify_customer'].attrs['registry'] == 'shopify'


def test_the_packs_text_is_in_the_house_style() -> None:
    gp = generator()
    ok_a = (
        'user',
        'unit',
        'uni',
        'use',
        'usa',
        'usu',
        'uti',
        'uri',
        'url',
        'utm',
        'uuid',
        'eur',
        'one',
        'once',
    )
    for pack in registry.read_index():
        assert re.fullmatch(r'[A-Z].*\.', pack['description']) and not re.search(
            r': \d+ entit|\b0 (entities|measures|relationships)\b', pack['description']
        )
        assert not re.search(
            r'\b(Api|Crm|Dbt|Hubspot|Github|Gitlab|Bigcommerce|Netsuite|Linkedin)\b', pack['title']
        )
        for term in pack['terms']:
            text = term['definition']
            assert chr(0x2014) not in text and chr(0x2013) not in text and ' -- ' not in text, term['name']
            assert not re.search(r'\b(\w+) \1\b', term['name'].lower()), term['name']
            assert not re.search(r'one row per ([\w ]+ )?id\b', text), text
            for article, word in re.findall(r'\b(an?) ([a-z][\w-]*)', text):
                assert article == gp.article(word) or word.startswith(ok_a), (pack['name'], text)


def test_history_tables_are_not_entities_in_the_packs() -> None:
    packs = {p.name: p for p in load_packs()}
    # Tables with a row per day, per version or per one thing in another, as each source's
    # descriptions, seeds and grain columns show.
    not_entities = {
        'netsuite2': {'transactionline'},
        'mntn': {'advertiser', 'campaign_group', 'creative', 'creative_group', 'segment'},
        'openai': {'project_user'},
        'zendesk': {'schedule'},
        'workday_financial_management': {
            'business_plan_detail',
            'business_plan_entry_line',
            'worktag',
            'custom_worktag',
        },
        'salesforce_marketing_cloud': {'list_subscriber'},
        'fivetran_platform': {'connection', 'log'},
    }
    for name, tables_ in not_entities.items():
        # Each table is one the pack names, so the check below holds of something, but for those
        # that no term of the pack holds at all.
        named = {t for block in packs[name].match for t in block} | {
            t
            for ref in [
                *(ref for term in packs[name].terms for ref in term['columns']),
                *(r[end] for r in packs[name].relationships for end in ('from', 'to')),
            ]
            for t in ref.rsplit('.', 1)[0].split('|')
        }
        unnamed = {
            'netsuite2': {'transactionline'},
            'zendesk': {'schedule'},
            'workday_financial_management': {'worktag', 'custom_worktag'},
        }
        assert tables_ - named == unnamed.get(name, set()), (name, tables_ - named)
    for name in ('google_ads', 'facebook_ads', 'recurly', *not_entities):
        for term in packs[name].terms:
            if term['kind'] == 'entity':
                for ref in term['columns']:
                    tables = set(ref.rsplit('.', 1)[0].split('|'))
                    assert not any(re.search(r'history|_stats|report', t) for t in tables), (name, term)
                    assert not tables & not_entities.get(name, set()), (name, term)
    assert 'domo' not in packs  # one company's own datasets
    for pack in packs.values():
        for term in pack.terms:
            if term['kind'] == 'measure':
                assert not re.search(r'_history\b|snapshot', term['columns'][0].split('.')[0]), (
                    pack.name,
                    term,
                )
    google_ads = {t['name'] for t in packs['google_ads'].terms if t['kind'] == 'measure'}
    assert {'Google Ads ad group stats clicks', 'Google Ads ad group stats impressions'} <= google_ads
    assert 'Google Ads ad group stats cost micros' in google_ads
    # Measures whose descriptions Fivetran keeps in doc blocks.
    jira = {t['name']: t['definition'] for t in packs['jira'].terms if t['kind'] == 'measure'}
    assert jira['Jira issue time spent'] == 'The time that was spent working on this issue, in seconds.'


def test_a_schema_named_for_its_source_binds_entity_tables_in_any_naming(tmp_path: Path) -> None:
    # A Singer tap's tap_jira and dlt's hubspot_dataset hold the sources' objects in the plural.
    warehouse(
        tmp_path,
        'create schema tap_jira;'
        'create table tap_jira.issues (id varchar, key varchar, fields json, _sdc_extracted_at timestamp);'
        'create table tap_jira.projects (id varchar, key varchar, name varchar, _sdc_extracted_at timestamp);'
        'create schema hubspot_dataset;'
        'create table hubspot_dataset.contacts (id varchar, email varchar, _dlt_load_id varchar, _dlt_id varchar);'
        'create table hubspot_dataset.companies (id varchar, name varchar, _dlt_load_id varchar, _dlt_id varchar);'
        'create schema app;'
        'create table app.issues (id varchar, title varchar);'
        'create table app.projects (id varchar, name varchar);',
    )
    graph = Project(tmp_path).build(run_dbt=False).graph

    def bound(term: str) -> list[str]:
        return [e.target.split('.', 1)[1] for e in graph.out_edges(f'term.{term}', 'defines')]

    # The same tables in a schema named for nothing bind nothing.
    assert bound('jira_issue') == ['warehouse.tap_jira.issues#id']
    assert bound('jira_project') == ['warehouse.tap_jira.projects#id']
    assert bound('hubspot_contact') == ['warehouse.hubspot_dataset.contacts#id']
    assert bound('hubspot_company') == ['warehouse.hubspot_dataset.companies#id']


def test_the_licence_expression_covers_every_source_of_the_packs() -> None:
    expression = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['license']
    sources = json.loads((registry.PACKS / 'SOURCES.json').read_text(encoding='utf-8'))
    # The word list only tells words apart; nothing of it is distributed.
    shipped = {s['licence'] for s in sources if s['loader'] != 'word list'}
    assert shipped <= set(expression.split(' AND ')), (shipped, expression)
    assert 'airbytehq' not in json.dumps(sources) and 'Airbyte' not in shipped
    # The notice names the licences the sources are under and no other.
    notice = ' '.join((registry.PACKS / 'NOTICE').read_text(encoding='utf-8').split())
    taps = re.search(
        r'Singer and Meltano taps whose repositories are licensed under the (.+?) licences?,', notice
    )
    assert taps and set(re.split(r', | or ', taps.group(1))) == {
        s['licence'] for s in sources if s['loader'] == 'singer'
    }
    assert ('BSD' in notice) == any(s['licence'].startswith('BSD') for s in sources)
