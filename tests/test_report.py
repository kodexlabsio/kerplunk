from __future__ import annotations

from pathlib import Path

from konstruct.core.graph import Graph, Node
from konstruct.outputs.report import analyse, render, write_report


def graph() -> Graph:
    g = Graph()
    g.meta = {
        'generated_at': '2026-11-06T00:00:00+00:00',
        'root': '/x',
        'konstruct_version': '0.1.0',
        'inputs': {'dbt_projects': 1},
    }
    src = g.add_node(Node('source.p.raw.a', 'source', 'a', relation='db.raw.a'))
    tested = g.add_node(
        Node('model.p.tested', 'model', 'tested', relation='db.main.tested', path='models/tested.sql')
    )
    untested = g.add_node(Node('model.p.untested', 'model', 'untested', relation='db.main.untested'))
    g.add_node(Node('table.db.main.lonely', 'table', 'lonely', relation='db.main.lonely'))
    warehouse_model = g.add_node(
        Node('model.p.wh', 'model', 'wh', relation='db.main.wh', attrs={'in_warehouse': True})
    )
    test = g.add_node(Node('test.p.t', 'test', 'not_null_tested_id'))
    g.add_column(src, 'id')
    g.add_column(tested, 'id')
    g.add_column(tested, 'constant')
    g.add_column(untested, 'id')
    g.add_column(warehouse_model, 'ghost', attrs={'documented_only': True})
    g.add_edge(src.id, tested.id, 'feeds')
    g.add_edge(tested.id, untested.id, 'feeds')
    g.add_edge(untested.id, warehouse_model.id, 'feeds')
    g.add_edge('model.p.tested#id', 'source.p.raw.a#id', 'derived_from')
    g.add_edge('model.p.untested#id', 'model.p.tested#id', 'derived_from')
    g.add_edge(test.id, 'model.p.tested#id', 'tests')
    g.warn('dbt model x: column lineage failed')
    return g


def test_findings() -> None:
    g = graph()
    findings = analyse(g)
    assert [n.name for n in findings.orphans] == ['lonely']
    assert [n.name for n in findings.untested] == ['untested', 'wh']
    assert [g.label(n) for n in findings.no_lineage] == ['tested.constant']
    assert [g.label(n) for n in findings.documented_missing] == ['wh.ghost']
    assert (findings.lineage_covered, findings.lineage_total) == (2, 3)
    assert [(g.label(n), c) for n, c in findings.hubs] == [('tested.id', 1), ('a.id', 1)]


def test_render_and_write(tmp_path: Path) -> None:
    text = render(graph())
    assert text.startswith('# Konstruct report\n')
    assert '| dbt projects | 1 |' in text
    assert 'Column lineage covers 2 of 3 columns' in text
    assert '- `lonely` (table)' in text
    assert '- `untested` (model)' in text
    assert '- `tested.constant`' in text
    assert '- dbt model x: column lineage failed' in text
    assert write_report(graph(), tmp_path / 'REPORT.md').read_text() == text


def test_long_lists_are_cut() -> None:
    g = Graph()
    for i in range(60):
        g.add_node(Node(f'table.t{i:02}', 'table', f't{i:02}', relation=f'db.s.t{i:02}'))
    text = render(g)
    assert '- and 10 more' in text
    assert 'None.' in text
