"""Apache Ossie portability: each fixture of the M0 to M3 ladder read through a build and written
back by ``konstruct export ossie``, graded element by element in the ladder's outcome vocabulary.

The fixtures and manifest are Ossie's own (``compliance/portability``, see ``fixtures/ossie/NOTICE``).
A metric or field graded PORTABLE came back with the dialect tag it was authored in and the same
expression. The bare variants write model-scoped metrics as ``SUM(amount)``, which the
specification's name resolution does not allow (a model-scoped metric names ``dataset.field``), so
Konstruct reports each and leaves it out: DROPPED_DIAGNOSED.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from konstruct import Project
from tests.test_ossie import FIXTURES, valid

LADDER = FIXTURES / 'portability'
MANIFEST = yaml.safe_load((LADDER / 'manifest.yaml').read_text())
VARIANTS = [variant for rung in MANIFEST['rungs'] for variant in rung['variants']]
PORTABLE_DIALECTS = ('ANSI_SQL', 'OSSIE_SQL_2026')


def _text(expression: Any) -> str:
    """Whitespace, including around brackets and commas, does not change an expression."""
    collapsed = re.sub(r'\s+', ' ', str(expression)).strip()
    return re.sub(r'\s*([(),])\s*', r'\1', collapsed)


def _dialects(expression: Any) -> dict[str, str]:
    """An expression's text under each dialect tag; ``sql`` is OSSIE_SQL_2026."""
    found: dict[str, str] = {}
    if not isinstance(expression, dict):
        return found
    for entry in expression.get('dialects') or []:
        found[str(entry['dialect'])] = _text(entry['expression'])
    if 'sql' in expression:
        found['OSSIE_SQL_2026'] = _text(expression['sql'])
    return found


def _elements(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    elements = {}
    for dataset in document.get('datasets') or []:
        for item in dataset.get('fields') or []:
            elements[('field', f'{dataset["name"]}.{item["name"]}')] = _dialects(item.get('expression'))
        for metric in dataset.get('metrics') or []:
            elements[('metric', metric['name'])] = _dialects(metric.get('expression'))
    for metric in document.get('metrics') or []:
        elements[('metric', metric['name'])] = _dialects(metric.get('expression'))
    return elements


def grade(authored: dict[str, Any], written: dict[str, Any], warnings: list[str]) -> dict[str, str]:
    """Each element's outcome, by ``kind:name``, as the ladder's manifest defines them."""
    before, after = _elements(authored), _elements(written)
    outcomes = {}
    for key, dialects in before.items():
        tag = next((d for d in dialects if d in PORTABLE_DIALECTS), next(iter(dialects), ''))
        returned = after.get(key)
        if returned is None:
            # Konstruct names a metric as metric 'name' and a field as field dataset.name.
            named = any(f'{key[0]} {key[1]!r}' in w or f'{key[0]} {key[1]} ' in w for w in warnings)
            outcome = 'DROPPED_DIAGNOSED' if named else 'DROPPED_SILENT'
        elif returned.get(tag) == dialects[tag]:
            outcome = 'PORTABLE'
        elif dialects[tag] in returned.values():
            outcome = 'RELABELED'
        elif any(d in PORTABLE_DIALECTS for d in returned):
            outcome = 'REWRITTEN'
        else:
            outcome = 'SURVIVED_VENDOR_ONLY'
        outcomes[f'{key[0]}:{key[1]}'] = outcome
    for key in after.keys() - before.keys():
        outcomes[f'{key[0]}:{key[1]}'] = 'ADDED'
    return outcomes


def round_trip(fixture: Path, root: Path) -> tuple[dict[str, Any], list[str]]:
    """The model Konstruct writes back after a build reads ``fixture``, and the build's warnings."""
    shutil.copy(fixture, root / 'model.ossie.yaml')
    # The ladder's datasets read samples.sales.orders: a DuckDB database named samples.
    con = duckdb.connect(str(root / 'samples.duckdb'))
    con.execute('create schema sales')
    con.execute(
        'create table sales.orders (order_id integer, customer_id integer, amount decimal(12, 2), '
        'status varchar)'
    )
    con.close()
    project = Project(root)
    graph = project.build(run_dbt=False).graph
    return valid(project.export('ossie')), [w for w in graph.warnings if w.startswith('ossie: ')]


@pytest.mark.parametrize('variant', VARIANTS, ids=[v['id'] for v in VARIANTS])
def test_the_portability_ladder(variant: dict[str, Any], tmp_path: Path) -> None:
    fixture = LADDER / variant['file'].replace('.ossie.yaml', '.ossie.fixture.yaml')
    authored = yaml.safe_load(fixture.read_text())
    written, warnings = round_trip(fixture, tmp_path)
    outcomes = grade(authored, written, warnings)
    if variant['reference_form'] == 'bare':
        assert {k: v for k, v in outcomes.items() if k.startswith('field:')} == {
            k: 'PORTABLE' for k in outcomes if k.startswith('field:')
        }
        assert {k: v for k, v in outcomes.items() if k.startswith('metric:')} == {
            k: 'DROPPED_DIAGNOSED' for k in outcomes if k.startswith('metric:')
        }
        assert all('names each field as dataset.field' in w for w in warnings)
    else:
        assert outcomes == dict.fromkeys(outcomes, 'PORTABLE')
        assert warnings == []
        # The rest of the model comes back as authored too, with Konstruct's provenance added.
        assert written['name'] == authored['name'] and written['description'] == authored['description']
        assert written['datasets'] == authored['datasets']
        # Metrics are written in name order, as the ontology lists measures.
        by_name = sorted(authored['metrics'], key=lambda m: m['name'])
        for before, after in zip(by_name, written['metrics'], strict=True):
            extension = after.pop('custom_extensions')
            assert [e['vendor_name'] for e in extension] == ['KONSTRUCT']
            assert after == before
    if variant['tag'] == 'OSSIE_SQL_2026':
        # The OSSIE_SQL_2026 tag itself survives: TAG_PRESERVED.
        [metric] = written['metrics']
        assert metric['expression']['dialects'][0]['dialect'] == 'OSSIE_SQL_2026'


def test_the_grader_tells_outcomes_apart() -> None:
    authored = {
        'datasets': [{'name': 'd', 'fields': [{'name': 'a', 'expression': {'sql': 'a'}}]}],
        'metrics': [
            {'name': 'same', 'expression': {'dialects': [{'dialect': 'ANSI_SQL', 'expression': 'SUM(d.a)'}]}},
            {
                'name': 'moved',
                'expression': {'dialects': [{'dialect': 'ANSI_SQL', 'expression': 'MAX(d.a)'}]},
            },
            {'name': 'changed', 'expression': {'sql': 'MIN(d.a)'}},
            {'name': 'vendor', 'expression': {'sql': 'AVG(d.a)'}},
            {'name': 'told', 'expression': {'sql': 'COUNT(d.a)'}},
            {'name': 'lost', 'expression': {'sql': 'COUNT(*)'}},
        ],
    }
    written = {
        'datasets': [{'name': 'd', 'fields': [{'name': 'a', 'expression': {'sql': 'a'}}]}],
        'metrics': [
            {
                'name': 'same',
                'expression': {'dialects': [{'dialect': 'ANSI_SQL', 'expression': 'SUM( d.a )'}]},
            },
            {'name': 'moved', 'expression': {'sql': 'MAX(d.a)'}},
            {'name': 'changed', 'expression': {'sql': 'MIN(a)'}},
            {
                'name': 'vendor',
                'expression': {'dialects': [{'dialect': 'SNOWFLAKE', 'expression': 'AVG(a)'}]},
            },
            {'name': 'new', 'expression': {'sql': 'SUM(d.a)'}},
        ],
    }
    assert grade(authored, written, ["ossie: m.yaml: model 'm': metric 'told' reads 'x'"]) == {
        'field:d.a': 'PORTABLE',
        'metric:same': 'PORTABLE',
        'metric:moved': 'RELABELED',
        'metric:changed': 'REWRITTEN',
        'metric:vendor': 'SURVIVED_VENDOR_ONLY',
        'metric:told': 'DROPPED_DIAGNOSED',
        'metric:lost': 'DROPPED_SILENT',
        'metric:new': 'ADDED',
    }
