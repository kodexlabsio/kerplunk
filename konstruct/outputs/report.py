"""REPORT.md: a summary of the graph and the gaps worth fixing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from konstruct.core.graph import RELATION_KINDS, Graph, Node

LIST_LIMIT = 50

# Relations that are expected to have no upstream: data enters the stack here.
ROOT_KINDS = frozenset({'source', 'seed'})


@dataclass
class Findings:
    orphans: list[Node]
    untested: list[Node]
    no_lineage: list[Node]
    documented_missing: list[Node]
    hubs: list[tuple[Node, int]]
    lineage_covered: int
    lineage_total: int


def _is_produced(graph: Graph, node: Node) -> bool:
    """Whether Konstruct knows how the relation is built (dbt model, SQL or task that writes it)."""
    if node.kind in ('model', 'snapshot'):
        return True
    # A table a loader (Fivetran, Airbyte) lands is a root: it arrives from outside.
    return any(
        graph.nodes[e.source].kind in RELATION_KINDS | {'task'}
        for e in graph.in_edges(node.id, 'feeds')
        if e.attrs.get('role') != 'reference'
        and e.source in graph.nodes
        and not graph.nodes[e.source].attrs.get('loader')
    )


def analyse(graph: Graph) -> Findings:
    relations = sorted(graph.relations(), key=lambda n: n.id)
    orphans = [
        n
        for n in relations
        if not any(e.kind == 'feeds' for e in graph.in_edges(n.id))
        and not any(e.kind in ('feeds', 'runs') for e in graph.out_edges(n.id))
        and not graph.in_edges(n.id, 'runs')
    ]

    def tested(node: Node) -> bool:
        if graph.in_edges(node.id, 'tests'):
            return True
        return any(graph.in_edges(column.id, 'tests') for column in graph.columns_of(node.id))

    untested = [n for n in relations if n.kind in ('model', 'snapshot') and not tested(n)]

    no_lineage: list[Node] = []
    covered = total = 0
    for node in relations:
        if node.kind in ROOT_KINDS or not _is_produced(graph, node):
            continue
        for column in graph.columns_of(node.id):
            if column.attrs.get('nested') or column.attrs.get('documented_only'):
                continue
            total += 1
            if graph.out_edges(column.id, 'derived_from'):
                covered += 1
            else:
                no_lineage.append(column)

    documented_missing = [
        column
        for node in relations
        if node.attrs.get('in_warehouse')
        for column in graph.columns_of(node.id)
        if column.attrs.get('documented_only')
    ]

    downstream_counts = []
    for node in graph.nodes.values():
        if node.kind != 'column':
            continue
        count = len(graph.in_edges(node.id, 'derived_from')) + len(
            [e for e in graph.out_edges(node.id, 'feeds') if e.attrs.get('role') == 'reference']
        )
        if count:
            downstream_counts.append((node, count))
    downstream_counts.sort(key=lambda item: (-item[1], item[0].id))
    return Findings(
        orphans=orphans,
        untested=untested,
        no_lineage=no_lineage,
        documented_missing=documented_missing,
        hubs=downstream_counts[:10],
        lineage_covered=covered,
        lineage_total=total,
    )


def _bullets(graph: Graph, nodes: Iterable[Node], detail: bool = True) -> list[str]:
    items = list(nodes)
    lines = []
    for node in items[:LIST_LIMIT]:
        label = graph.label(node)
        extra = (
            f' ({node.kind}{", " + node.path if detail and node.path else ""})'
            if node.kind != 'column'
            else ''
        )
        lines.append(f'- `{label}`{extra}')
    if len(items) > LIST_LIMIT:
        lines.append(f'- and {len(items) - LIST_LIMIT} more')
    return lines or ['None.']


def render(graph: Graph) -> str:
    findings = analyse(graph)
    stats = graph.stats()
    meta = graph.meta
    inputs = meta.get('inputs', {})
    lines = [
        '# Konstruct report',
        '',
        f'Built {meta.get("generated_at", "")} from `{meta.get("root", "")}` by Konstruct '
        f'{meta.get("konstruct_version", "")} in {meta.get("build_seconds", "?")}s. SQL dialect: '
        f'{meta.get("dialect") or "sqlglot default"}.',
        '',
        '## Summary',
        '',
        '| Input | Count |',
        '|-------|-------|',
        *(f'| {name.replace("_", " ")} | {count} |' for name, count in inputs.items()),
        '',
        '| Node kind | Count |',
        '|-----------|-------|',
        *(f'| {kind} | {count} |' for kind, count in sorted(stats.items()) if not kind.startswith('edge:')),
        '',
        '| Edge kind | Count |',
        '|-----------|-------|',
        *(f'| {kind[5:]} | {count} |' for kind, count in sorted(stats.items()) if kind.startswith('edge:')),
        '',
    ]
    if findings.lineage_total:
        share = 100 * findings.lineage_covered / findings.lineage_total
        lines += [
            f'Column lineage covers {findings.lineage_covered} of {findings.lineage_total} columns of '
            f'models and SQL-built tables ({share:.0f}%).',
            '',
        ]
    lines += [
        '## Most used columns',
        '',
        'Columns with the most direct dependants (derived columns and joins).',
        '',
    ]
    if findings.hubs:
        lines += ['| Column | Dependants |', '|--------|------------|']
        lines += [f'| `{graph.label(node)}` | {count} |' for node, count in findings.hubs]
    else:
        lines.append('None.')
    usage = graph.meta.get('usage')
    if usage:
        queried = sorted(
            (n for n in graph.nodes.values() if n.kind == 'column' and n.attrs.get('usage')),
            key=lambda n: (-int(n.attrs['usage']['queries']), graph.label(n)),
        )
        lines += [
            '',
            '## Most queried columns',
            '',
            f'From {", ".join(usage["sources"])}, the last {usage["days"]} days: the columns people, '
            'dashboards and jobs actually read.',
            '',
        ]
        if queried:
            lines += ['| Column | Queries | Users | Score |', '|--------|---------|-------|-------|']
            for node in queried[:25]:
                use = node.attrs['usage']
                lines.append(
                    f'| `{graph.label(node)}` | {use["queries"]} | {use["users"]} | {use["score"]} |'
                )
            unread = [
                n
                for n in graph.nodes.values()
                if n.kind == 'column'
                and not n.attrs.get('usage')
                and graph.parent_of(n) is not None
                and _is_produced(graph, graph.parent_of(n))  # type: ignore[arg-type]
            ]
            lines += [
                '',
                f'{len(unread)} column{"s" if len(unread) != 1 else ""} of models and built tables '
                f'{"were" if len(unread) != 1 else "was"} not queried in that time.',
            ]
        else:
            lines.append('No queries matched columns in the graph.')
    terms = sorted((n for n in graph.nodes.values() if n.kind == 'term'), key=lambda n: n.name.lower())
    if terms:
        lines += [
            '',
            '## Business terms',
            '',
            'From definitions.yml. Problems with them are listed under Warnings.',
            '',
            '| Term | Status | Owner | Columns |',
            '|------|--------|-------|---------|',
        ]
        for term in terms:
            owner = term.attrs.get('owner') or {}
            who = (
                f'{owner["name"]} ({owner["role"]})' if owner.get('role') else owner.get('name', '')
            ) or 'none'
            columns = sorted(graph.label(graph.nodes[e.target]) for e in graph.out_edges(term.id, 'defines'))
            bound = ', '.join(f'`{c}`' for c in columns) or 'none'
            lines.append(f'| {term.name} | {term.attrs.get("status", "draft")} | {who} | {bound} |')
        approved = {
            e.target
            for t in terms
            if t.attrs.get('status') == 'approved'
            for e in graph.out_edges(t.id, 'defines')
        }
        lines += [
            '',
            f'{len(approved)} column{"s" if len(approved) != 1 else ""} hold an approved definition.',
        ]
    lines += [
        '',
        '## Orphans',
        '',
        'Relations with nothing upstream or downstream, and no task that runs them.',
        '',
        *_bullets(graph, findings.orphans),
        '',
        '## Untested models',
        '',
        'dbt models and snapshots with no test on the model or any of its columns.',
        '',
        *_bullets(graph, findings.untested),
        '',
        '## Columns with no lineage',
        '',
        'Columns of models and SQL-built tables with no upstream column: constants, columns Konstruct '
        'could not resolve, or SQL it could not parse (see Warnings).',
        '',
        *_bullets(graph, findings.no_lineage),
        '',
        '## Documented columns missing from the warehouse',
        '',
        'Columns described in dbt YAML that the warehouse does not have.',
        '',
        *_bullets(graph, findings.documented_missing),
        '',
        '## Warnings',
        '',
    ]
    if graph.warnings:
        lines += [f'- {w}' for w in graph.warnings[:200]]
        if len(graph.warnings) > 200:
            lines.append(f'- and {len(graph.warnings) - 200} more (see graph.json)')
    else:
        lines.append('None.')
    return '\n'.join(lines) + '\n'


def write_report(graph: Graph, path: Path) -> Path:
    path.write_text(render(graph), encoding='utf-8')
    return path
