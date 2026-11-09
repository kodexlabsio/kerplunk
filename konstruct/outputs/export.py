"""Export the graph for other tools: OpenLineage, Mermaid, Graphviz DOT, CSV and Apache Ossie.

* ``openlineage``: one OpenLineage ``JobEvent`` per relation that something builds (a dbt model,
  a SQL file, an Airflow or Dagster task's SQL), as JSON lines. Each lists its input datasets and
  its output dataset with a schema facet and a column lineage facet (``ColumnLineageDatasetFacet``
  1-2-0): per output column, the input columns with ``DIRECT`` transformations (``IDENTITY``,
  ``TRANSFORMATION`` or ``AGGREGATION``), and dataset-wide ``INDIRECT`` dependencies for the
  columns used in joins, filters and grouping. These are static lineage events, without runs, as
  OpenLineage defines them for lineage known ahead of execution; Marquez, DataHub, OpenMetadata
  and other OpenLineage consumers accept them.
* ``mermaid`` and ``dot``: the relation graph (or the columns of each relation with
  ``columns=True``) for documentation and pull requests.
* ``csv``: one row per column lineage edge, for spreadsheets and warehouses.
* ``ossie``: the ontology as an Apache Ossie semantic model (see :mod:`konstruct.semantics.ossie`).

``select`` restricts any format but ``ossie`` to the lineage of one node, upstream and downstream.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from konstruct import __version__
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS

Format = Literal['openlineage', 'mermaid', 'dot', 'csv', 'ossie']
FORMATS: tuple[str, ...] = ('openlineage', 'mermaid', 'dot', 'csv', 'ossie')


class ExportError(ValueError):
    """An export that cannot be written as asked: an unknown format, a selection with the Ossie
    export, or an ontology with nothing to export."""


PRODUCER = f'https://github.com/kodexlabsio/konstruct/tree/v{__version__}'
SPEC = 'https://openlineage.io/spec/2-0-2/OpenLineage.json'
SCHEMA_FACET = 'https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet'
LINEAGE_FACET = (
    'https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet'
)
SQL_FACET = 'https://openlineage.io/spec/facets/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet'
SOURCE_FACET = (
    'https://openlineage.io/spec/facets/1-0-1/SourceCodeLocationJobFacet.json'
    '#/$defs/SourceCodeLocationJobFacet'
)
VIEW_KINDS = (*RELATION_KINDS, 'task', 'exposure')


def export(
    graph: Graph,
    fmt: str,
    *,
    select: Node | None = None,
    columns: bool = False,
    namespace: str | None = None,
    job_namespace: str = 'konstruct',
    event_time: str | None = None,
) -> str:
    """The graph (or the lineage of ``select``) in ``fmt``; see the module docstring."""
    if fmt == 'ossie':
        from konstruct.semantics.ontology import derive
        from konstruct.semantics.ossie import export as ossie_export

        if select is not None:
            raise ExportError('the ossie export is the whole ontology; it does not take a selection')
        root = graph.meta.get('root')
        return ossie_export(graph, derive(graph), name=Path(str(root)).name if root else None)
    keep = _selection(graph, select) if select is not None else None
    if fmt == 'openlineage':
        return openlineage(
            graph, keep, namespace=namespace, job_namespace=job_namespace, event_time=event_time
        )
    if fmt == 'mermaid':
        return mermaid(graph, keep, columns=columns)
    if fmt == 'dot':
        return dot(graph, keep, columns=columns)
    if fmt == 'csv':
        return column_csv(graph, keep)
    raise ExportError(f'unknown export format {fmt!r}; choose from {", ".join(FORMATS)}')


def _selection(graph: Graph, node: Node) -> set[str]:
    """Relation and task ids in the lineage of ``node`` (and the node's own relation)."""
    keep: set[str] = set()
    for step in [*graph.impact(node), *graph.trace(node)]:
        target = step.node
        if target.kind == 'column' and target.parent:
            keep.add(target.parent)
        elif target.kind in VIEW_KINDS:
            keep.add(target.id)
    keep.add(node.parent if node.kind == 'column' and node.parent else node.id)
    return keep


def _relations(graph: Graph, keep: set[str] | None, kinds: Iterable[str] = VIEW_KINDS) -> list[Node]:
    return sorted(
        (n for n in graph.nodes.values() if n.kind in kinds and (keep is None or n.id in keep)),
        key=lambda n: n.id,
    )


def _relation_edges(graph: Graph, ids: set[str]) -> list[tuple[str, str, str]]:
    """(source, target, kind) between relations and tasks, columns standing in for their relation."""
    found: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        if edge.kind not in ('feeds', 'runs'):
            continue
        source, target = edge.source, edge.target
        if edge.kind == 'feeds':
            source = graph.nodes[source].parent or source if graph.nodes[source].kind == 'column' else source
            target = graph.nodes[target].parent or target if graph.nodes[target].kind == 'column' else target
        if source != target and source in ids and target in ids:
            found.add((source, target, edge.kind))
    return sorted(found)


# OpenLineage


def _dataset_name(node: Node) -> str:
    return node.relation or node.id


def _facet(schema: str, **fields: Any) -> dict[str, Any]:
    return {'_producer': PRODUCER, '_schemaURL': schema, **fields}


def _schema_facet(graph: Graph, node: Node) -> dict[str, Any]:
    fields = []
    for column in graph.columns_of(node.id):
        if '.' in column.name:
            continue  # nested fields appear inside their struct column in the warehouse's own type
        field: dict[str, Any] = {'name': column.name}
        if column.data_type:
            field['type'] = column.data_type
        if column.description:
            field['description'] = column.description
        fields.append(field)
    return _facet(SCHEMA_FACET, fields=fields)


def _subtype(expression: Any, transform: Any, dialect: str | None) -> str:
    if transform == 'direct':
        return 'IDENTITY'
    if isinstance(expression, str):
        try:
            tree = sqlglot.parse_one(expression, read=dialect)
        except PARSE_ERRORS:
            return 'TRANSFORMATION'
        if any(isinstance(n, exp.AggFunc) for n in tree.walk()):
            return 'AGGREGATION'
    return 'TRANSFORMATION'


def _indirect_subtypes(conditions: list[str], relation: str, column: str) -> list[str]:
    """JOIN, GROUP_BY, FILTER, WINDOW or SORT for each kind of condition that uses the column.

    Conditions are stored with fully qualified columns, so the column is matched with its
    relation; a bare name is tried only when no qualified match exists."""

    def kinds(pattern: re.Pattern[str]) -> list[str]:
        found = []
        for condition in conditions:
            head, _, body = condition.partition(':')
            if not pattern.search(body):
                continue
            head = head.upper()
            kind = (
                'JOIN'
                if 'JOIN' in head
                else 'GROUP_BY'
                if 'GROUP' in head
                else 'WINDOW'
                if 'WINDOW' in head or 'PARTITION' in head
                else 'SORT'
                if 'ORDER' in head
                else 'FILTER'
            )
            if kind not in found:
                found.append(kind)
        return found

    qualified = re.compile(rf'(?<![\w.]){re.escape(relation)}\.{re.escape(column)}\b', re.IGNORECASE)
    return kinds(qualified) or kinds(re.compile(rf'\b{re.escape(column)}\b', re.IGNORECASE)) or ['FILTER']


def openlineage(
    graph: Graph,
    keep: set[str] | None = None,
    *,
    namespace: str | None = None,
    job_namespace: str = 'konstruct',
    event_time: str | None = None,
) -> str:
    """One static OpenLineage ``JobEvent`` per built relation, as JSON lines, with schema and column lineage facets."""
    dialect = graph.meta.get('dialect')
    dialect = dialect if isinstance(dialect, str) else None
    namespace = namespace or f'{dialect or "sql"}://konstruct'
    when = event_time or str(graph.meta.get('generated_at') or datetime.now(UTC).isoformat())
    lines = []
    for node in _relations(graph, keep, RELATION_KINDS):
        lineage: dict[str, Any] = {}
        inputs: dict[str, Node] = {}
        for column in graph.columns_of(node.id):
            sources = []
            for edge in graph.out_edges(column.id, 'derived_from'):
                upstream = graph.nodes[edge.target]
                owner = graph.parent_of(upstream)
                if owner is None:
                    continue
                inputs[owner.id] = owner
                transform = {
                    'type': 'DIRECT',
                    'subtype': _subtype(column.attrs.get('expression'), edge.attrs.get('transform'), dialect),
                }
                if edge.attrs.get('transform') != 'direct' and isinstance(
                    column.attrs.get('expression'), str
                ):
                    transform['description'] = column.attrs['expression']
                sources.append(
                    {
                        'namespace': namespace,
                        'name': _dataset_name(owner),
                        'field': upstream.name,
                        'transformations': [transform],
                    }
                )
            if sources:
                lineage[column.name] = {'inputFields': sources}
        indirect = []
        conditions = [str(c) for c in node.attrs.get('conditions', [])]
        for edge in graph.in_edges(node.id, 'feeds'):
            if edge.attrs.get('role') != 'reference':
                if graph.nodes[edge.source].kind in RELATION_KINDS:
                    inputs[edge.source] = graph.nodes[edge.source]
                continue
            used = graph.nodes[edge.source]
            owner = graph.parent_of(used)
            if owner is None:
                continue
            inputs[owner.id] = owner
            indirect.append(
                {
                    'namespace': namespace,
                    'name': _dataset_name(owner),
                    'field': used.name,
                    'transformations': [
                        {'type': 'INDIRECT', 'subtype': subtype}
                        for subtype in _indirect_subtypes(conditions, _dataset_name(owner), used.name)
                    ],
                }
            )
        if not lineage and not inputs:
            continue  # nothing builds it: a source, a seed or a table read from the warehouse
        output_facets: dict[str, Any] = {'schema': _schema_facet(graph, node)}
        if lineage or indirect:
            column_facet = _facet(LINEAGE_FACET, fields=lineage)
            if indirect:
                column_facet['dataset'] = sorted(indirect, key=lambda f: (f['name'], f['field']))
            output_facets['columnLineage'] = column_facet
        job_facets: dict[str, Any] = {}
        if node.path:
            job_facets['sourceCodeLocation'] = _facet(SOURCE_FACET, type='git', url=node.path, path=node.path)
        event = {
            'eventTime': when,
            'producer': PRODUCER,
            'schemaURL': f'{SPEC}#/$defs/JobEvent',
            'job': {'namespace': job_namespace, 'name': _dataset_name(node), 'facets': job_facets},
            'inputs': [
                {
                    'namespace': namespace,
                    'name': _dataset_name(i),
                    'facets': {'schema': _schema_facet(graph, i)},
                }
                for i in sorted(inputs.values(), key=lambda n: _dataset_name(n))
                if i.id != node.id
            ],
            'outputs': [{'namespace': namespace, 'name': _dataset_name(node), 'facets': output_facets}],
        }
        lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
    return '\n'.join(lines) + ('\n' if lines else '')


# Mermaid and DOT


def _safe_id(node_id: str, ids: dict[str, str]) -> str:
    if node_id not in ids:
        ids[node_id] = f'n{len(ids)}'
    return ids[node_id]


def _mermaid_text(text: str) -> str:
    return text.replace('"', '#quot;').replace('<', '#lt;').replace('>', '#gt;')


def mermaid(graph: Graph, keep: set[str] | None = None, *, columns: bool = False) -> str:
    """A Mermaid ``flowchart`` of relations and tasks, or of columns and column lineage with ``columns``."""
    nodes = _relations(graph, keep)
    ids: dict[str, str] = {}
    lines = ['flowchart LR']
    shapes = {
        'source': ('[("', '")]'),
        'seed': ('[/"', '"/]'),
        'task': ('(["', '"])'),
        'exposure': ('{{"', '"}}'),
    }
    present = {n.id for n in nodes}
    for node in nodes:
        name = _mermaid_text(graph.label(node))
        if columns and graph.columns_of(node.id):
            lines.append(f'  subgraph {_safe_id(node.id, ids)}["{name}"]')
            for column in graph.columns_of(node.id):
                lines.append(f'    {_safe_id(column.id, ids)}["{_mermaid_text(column.name)}"]')
            lines.append('  end')
            continue
        start, end = shapes.get(node.kind, ('["', '"]'))
        lines.append(f'  {_safe_id(node.id, ids)}{start}{name}{end}')
    if columns:
        for node in nodes:
            for column in graph.columns_of(node.id):
                for edge in graph.out_edges(column.id, 'derived_from'):
                    upstream = graph.nodes[edge.target]
                    if upstream.parent in present:
                        arrow = '-->' if edge.attrs.get('transform') == 'direct' else '-.->'
                        lines.append(f'  {_safe_id(upstream.id, ids)} {arrow} {_safe_id(column.id, ids)}')
    else:
        for source, target, kind in _relation_edges(graph, present):
            arrow = '-->' if kind == 'feeds' else '-.->'
            lines.append(f'  {_safe_id(source, ids)} {arrow} {_safe_id(target, ids)}')
    for kind in ('source', 'seed', 'table', 'model', 'snapshot', 'task', 'exposure'):
        members = [ids[n.id] for n in nodes if n.kind == kind and n.id in ids]
        if members:
            lines.append(f'  class {",".join(members)} {kind}')
    lines += [
        '  classDef model fill:#273338,stroke:#7DC3FF,color:#FFFFFF',
        '  classDef table fill:#273338,stroke:#3F7FB5,color:#FFFFFF',
        '  classDef source fill:#1F2A2E,stroke:#9AD1FF,color:#FFFFFF',
        '  classDef seed fill:#1F2A2E,stroke:#9AD1FF,color:#FFFFFF',
        '  classDef snapshot fill:#273338,stroke:#3F7FB5,color:#FFFFFF',
        '  classDef task fill:#E4F1FF,stroke:#1F6FB2,color:#1F2A2E',
        '  classDef exposure fill:#E8F1F7,stroke:#1F6FB2,color:#1F2A2E',
    ]
    return '\n'.join(lines) + '\n'


def _dot_text(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"')


def dot(graph: Graph, keep: set[str] | None = None, *, columns: bool = False) -> str:
    """A Graphviz graph of relations and tasks, or of columns as record ports with ``columns``."""
    nodes = _relations(graph, keep)
    present = {n.id for n in nodes}
    shapes = {'source': 'cylinder', 'seed': 'note', 'task': 'cds', 'exposure': 'hexagon', 'snapshot': 'box3d'}
    lines = [
        'digraph konstruct {',
        '  graph [rankdir=LR, fontname="Manrope", bgcolor="#1F2A2E", fontcolor="#FFFFFF"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#273338", color="#7DC3FF", '
        'fontcolor="#FFFFFF", fontname="Manrope"];',
        '  edge [color="#7DC3FF88"];',
    ]
    for node in nodes:
        label = _dot_text(graph.label(node))
        if columns and graph.columns_of(node.id):
            rows = '|'.join(f'<{i}> {_dot_text(c.name)}' for i, c in enumerate(graph.columns_of(node.id)))
            lines.append(
                f'  "{_dot_text(node.id)}" [shape=record, style=filled, label="{{{label}|{rows}}}"];'
            )
        else:
            lines.append(f'  "{_dot_text(node.id)}" [label="{label}", shape={shapes.get(node.kind, "box")}];')
    if columns:
        for node in nodes:
            ports = {c.id: i for i, c in enumerate(graph.columns_of(node.id))}
            for column in graph.columns_of(node.id):
                for edge in graph.out_edges(column.id, 'derived_from'):
                    upstream = graph.nodes[edge.target]
                    if upstream.parent not in present:
                        continue
                    up_ports = {c.id: i for i, c in enumerate(graph.columns_of(upstream.parent))}
                    style = '' if edge.attrs.get('transform') == 'direct' else ' [style=dashed]'
                    lines.append(
                        f'  "{_dot_text(upstream.parent)}":{up_ports[upstream.id]} -> '
                        f'"{_dot_text(node.id)}":{ports[column.id]}{style};'
                    )
    else:
        for source, target, kind in _relation_edges(graph, present):
            style = '' if kind == 'feeds' else ' [style=dashed]'
            lines.append(f'  "{_dot_text(source)}" -> "{_dot_text(target)}"{style};')
    lines.append('}')
    return '\n'.join(lines) + '\n'


# CSV

CSV_FIELDS = (
    'relation',
    'column',
    'upstream_relation',
    'upstream_column',
    'transform',
    'verified',
    'expression',
    'relation_kind',
    'upstream_kind',
    'path',
)


def column_csv(graph: Graph, keep: set[str] | None = None) -> str:
    """One CSV row per column lineage edge: relation, column, upstream relation and column, transform, verification, expression, kinds and path."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator='\n')
    writer.writeheader()
    rows = []
    for edge in graph.edges:
        if edge.kind != 'derived_from':
            continue
        column, upstream = graph.nodes[edge.source], graph.nodes[edge.target]
        owner, up_owner = graph.parent_of(column), graph.parent_of(upstream)
        if owner is None or up_owner is None:
            continue
        if keep is not None and owner.id not in keep:
            continue
        rows.append(
            {
                'relation': graph.label(owner),
                'column': column.name,
                'upstream_relation': graph.label(up_owner),
                'upstream_column': upstream.name,
                'transform': edge.attrs.get('transform', ''),
                'verified': edge.attrs.get('verified', ''),
                'expression': column.attrs.get('expression', ''),
                'relation_kind': owner.kind,
                'upstream_kind': up_owner.kind,
                'path': owner.path or '',
            }
        )
    rows.sort(key=lambda r: (r['relation'], r['column'], r['upstream_relation'], r['upstream_column']))
    writer.writerows(rows)
    return buffer.getvalue()
