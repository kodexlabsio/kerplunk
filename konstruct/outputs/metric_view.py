"""The ontology as Databricks metric views: one ``CREATE OR REPLACE VIEW ... WITH METRICS`` per table.

Each view's body is the metric view YAML of Databricks' reference
(https://docs.databricks.com/aws/en/business-semantics/metric-views/yaml-reference), in the
``CREATE VIEW ... WITH METRICS LANGUAGE YAML AS $$ ... $$`` statement of
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view, version 1.1:

- ``source``: one table of :mod:`konstruct.outputs.compiled`, and ``comment`` its description.
- ``joins``: each table it refers to directly through a relationship to its primary key or a
  unique key, as a many-to-one join with ``rely: at_most_one_match``, under the table's name (or
  the referring column's, when it refers to that table more than once). The tables those refer to are
  not joined in turn, which would put every table upstream of a fact in its view.
- ``fields`` (version 1.1's name for what earlier versions call ``dimensions``): every column of
  the source, as ``source.<column>``, then every column of each joined table, named
  ``<join>_<column>``.
- ``measures``: each aggregate measure that reads the source, its columns as ``source.<column>``
  so that no field of the same name stands in for one, with the term's name as its
  ``display_name``, at most ten of its synonyms, and a comment that holds its definition, grain,
  required filters and unit. Names that differ only by case are kept apart, and a keyword is
  quoted.

A comment that holds ``$$`` would end the statement, which has no way to escape it, so it is left
out and listed.

Expressions are written in Databricks SQL, translated by sqlglot from the warehouse's dialect. A
measure that reads more than one table is left out, as is the rest that
:mod:`konstruct.outputs.compiled` lists; the statements open with comments that say what.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from sqlglot import exp

from konstruct.core.graph import Graph
from konstruct.outputs.compiled import Compiled, CompiledTable, compile_ontology, plain, render
from konstruct.semantics.ontology import Ontology
from konstruct.semantics.ossie import unique_name

DIALECT = 'databricks'
VERSION = 1.1
MAX_SYNONYMS = 10
MAX_TEXT = 255
"""Databricks' limits on a field's or measure's synonyms and on each synonym and display name;
a synonym over the length is not written, rather than cut."""


def export(graph: Graph, ontology: Ontology, name: str | None = None) -> str:
    """The ontology as Databricks metric view statements, one per table."""
    from konstruct.outputs.export import ExportError

    model = compile_ontology(graph, ontology, name)
    if not model.tables:
        raise ExportError(model.nothing())
    views = [(table, view(model, table)) for table in model.tables]
    lines = model.header('--', 'Databricks metric views, one per table')
    for table, body in views:
        text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)
        if '$$' in text:  # a name or expression, which cannot be left out as text can
            raise ExportError(f'the metric view of {table.name} holds $$, which would end its statement')
        lines += [
            '',
            f'CREATE OR REPLACE VIEW {_identifier(table.name + "_metrics")}',
            'WITH METRICS',
            'LANGUAGE YAML',
            'AS $$',
            text.rstrip('\n'),
            '$$;',
        ]
    return '\n'.join(lines) + '\n'


def view(model: Compiled, table: CompiledTable) -> dict[str, Any]:
    """The metric view YAML of one table, as a mapping; what it cannot hold is added to
    ``model.omitted``."""
    body: dict[str, Any] = {'version': VERSION, 'source': _relation(table.source)}
    comment = _comment(model, table.description, table.name)
    if comment:
        body['comment'] = comment
    fields = [
        _field(model, column.name, f'source.{_identifier(column.column)}', column.description, table.name)
        for column in table.columns
    ]
    joins = _joins(model, table, fields)
    if joins:
        body['joins'] = joins
    body['fields'] = fields
    # Fields and measures share one namespace, in which case does not tell names apart.
    used = {f['name'].lower() for f in fields}
    measures = []
    for metric in model.metrics:
        if metric.table != table.name:
            continue
        tree = metric.expression.copy()
        for column in tree.find_all(exp.Column):
            # source.column is the source's column, never a field that has the same name.
            column.set('table', exp.to_identifier('source'))
        name = unique_name(metric.name, used, fold=True)
        measure: dict[str, Any] = {'name': name, 'expr': render(tree, DIALECT)}
        if metric.title != name:
            if _fits(metric.title):
                measure['display_name'] = metric.title
            else:
                model.omitted.append(
                    f'the display name of {name} in its metric view: Databricks takes at most '
                    f'{MAX_TEXT} characters without $$'
                )
        note = _comment(model, metric.note, name)
        if note:
            measure['comment'] = note
        candidates = [s for s in metric.synonyms if s != metric.title]
        synonyms = [s for s in candidates if _fits(s)][:MAX_SYNONYMS]
        if len(synonyms) < len(candidates):
            model.omitted.append(
                f'{len(candidates) - len(synonyms)} synonyms of {name} in its metric view: Databricks '
                f'takes ten of at most {MAX_TEXT} characters without $$'
            )
        if synonyms:
            measure['synonyms'] = synonyms
        measures.append(measure)
    if measures:
        body['measures'] = measures
    return body


def _joins(model: Compiled, table: CompiledTable, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The many-to-one joins from ``table`` to each table it refers to, adding each joined table's
    columns to ``fields``. Only direct references are joined: following them further would put
    every table upstream of a fact into its view. A join takes its table's name, or the
    referring column's when the source refers to that table more than once."""
    links = [link for link in model.links if link.source == table.name]
    targets = [link.target for link in links]
    tables = {t.name: t for t in model.tables}
    used = {f['name'].lower() for f in fields}
    names: set[str] = set()
    joins = []
    for link in links:
        target = tables[link.target]
        # A table referred to more than once is joined once for each referring column, after it.
        join = unique_name(
            target.name if targets.count(link.target) == 1 else _referrer(link.source_columns),
            names,
            fold=True,
        )
        alias = _identifier(join)
        on = ' AND '.join(
            f'source.{_identifier(a)} = {alias}.{_identifier(b)}'
            for a, b in zip(link.source_columns, link.target_columns, strict=True)
        )
        for column in target.columns:
            fields.append(
                _field(
                    model,
                    unique_name(f'{join}_{column.name}', used, fold=True),
                    f'{alias}.{_identifier(column.column)}',
                    column.description,
                    f'{target.name}.{column.name}',
                )
            )
        joins.append(
            {'name': join, 'source': _relation(target.source), 'on': on, 'rely': {'at_most_one_match': True}}
        )
    return joins


def _field(model: Compiled, name: str, expr: str, description: str | None, what: str) -> dict[str, Any]:
    entry: dict[str, Any] = {'name': name, 'expr': expr}
    comment = _comment(model, description, what)
    if comment:
        entry['comment'] = comment
    return entry


def _comment(model: Compiled, text: str | None, what: str) -> str | None:
    """``text`` as a comment, or None when there is none or it holds ``$$``, which would end the
    statement: that is left out and listed, since the statement has no way to escape it."""
    if not text or not text.strip():
        return None
    if '$$' in text:
        model.omitted.append(
            f'the comment of {what} in its metric view: it holds $$, which would end the statement'
        )
        return None
    return text.strip()


def _referrer(columns: list[str]) -> str:
    """A join's name from the columns that refer: ``billing_customer_id`` gives ``billing_customer``."""
    return '_'.join(re.sub(r'(?i)_(?:id|key|pk|sk|uuid|guid)$', '', c) or c for c in columns)


def _fits(text: str) -> bool:
    return len(text) <= MAX_TEXT and '$$' not in text


def _identifier(name: str) -> str:
    return name if plain(name, DIALECT) else '`' + name.replace('`', '``') + '`'


def _relation(source: str) -> str:
    return '.'.join(_identifier(part) for part in source.split('.'))
