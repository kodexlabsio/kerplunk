"""Ontology packs: shared ontologies for common source schemas, bound when the graph has them."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.engine.config import ConfigError, load_config
from konstruct.semantics.registry import find, load_packs, suffixes

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


def warehouse(root: Path, *scripts: str, name: str = 'warehouse') -> Path:
    path = root / f'{name}.duckdb'
    con = duckdb.connect(str(path))
    for script in scripts:
        con.execute(script)
    con.close()
    return path


def test_every_pack_loads_and_names_real_tables() -> None:
    packs = {p.name: p for p in load_packs()}
    assert set(packs) == {'ga4', 'hubspot', 'salesforce', 'shopify', 'stripe'}
    for pack in packs.values():
        assert pack.match and pack.terms and pack.description and pack.schemas
        for term in pack.terms:
            assert term['name'] and term['definition'] and term['columns']
            assert 'status' not in term


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
        (main / 'konstruct.toml').read_text() + '\n[repositories]\npaths = ["../marketing"]\n'
    )
    graph = Project(main).build(run_dbt=False).graph
    assert [n for n in graph.nodes if n.startswith('term.stripe_customer')] == ['term.stripe_customer']
    assert not [w for w in graph.warnings if 'more than one repository' in w]
