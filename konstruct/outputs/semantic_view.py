"""The ontology as a Snowflake semantic view: one ``CREATE OR REPLACE SEMANTIC VIEW`` statement.

The statement follows Snowflake's reference for ``CREATE SEMANTIC VIEW``
(https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view) and its validation rules
(https://docs.snowflake.com/en/user-guide/views-semantic/validation-rules):

- ``TABLES``: one logical table per table of :mod:`konstruct.outputs.compiled`, aliased by its
  name, with the entity's key as ``PRIMARY KEY`` when the ontology knows it to be unique, the
  unique keys of the other entities it holds as ``UNIQUE``, the entity's name and term
  ``WITH SYNONYMS``, and the relation's description as its ``COMMENT``.
- ``RELATIONSHIPS``: ``source (columns) REFERENCES target (key)`` for each relationship to the
  target's primary key or a unique key. Snowflake allows no cycle, even through other tables, so a
  relationship that would close one is left out.
- ``DIMENSIONS``: every column of every table, under its own name, with its description. Names
  that differ only by case are kept apart, as unquoted Snowflake names are not case sensitive, and
  a reserved word is quoted.
- ``METRICS``: each aggregate measure on the table it reads, with the term's synonyms and a comment
  that holds its definition, grain, required filters and unit.

Expressions are written in Snowflake SQL, translated by sqlglot from the warehouse's dialect.
Konstruct does not tell facts from dimensions, so no ``FACTS`` are written, and a measure that
reads more than one table is left out, as Snowflake's metrics read other tables only through facts.
What is left out is listed in comments at the top of the statement.
"""

from __future__ import annotations

from konstruct.core.graph import Graph
from konstruct.outputs.compiled import Compiled, CompiledLink, compile_ontology, plain, render
from konstruct.semantics.ontology import Ontology

DIALECT = 'snowflake'


def export(graph: Graph, ontology: Ontology, name: str | None = None) -> str:
    """The ontology as a Snowflake ``CREATE OR REPLACE SEMANTIC VIEW`` statement."""
    from konstruct.outputs.export import ExportError

    model = compile_ontology(graph, ontology, name)
    if not model.tables:
        raise ExportError(model.nothing())
    links = _acyclic(model)
    lines = model.header('--', 'A Snowflake semantic view')
    lines.append(f'CREATE OR REPLACE SEMANTIC VIEW {_identifier(model.name)}')
    tables = []
    for table in model.tables:
        entry = f'{_identifier(table.name)} AS {_relation(table.source)}'
        if table.key:
            entry += f'\n      PRIMARY KEY ({", ".join(_identifier(k) for k in table.key)})'
        for unique in table.unique:
            entry += f'\n      UNIQUE ({", ".join(_identifier(k) for k in unique)})'
        entry += _synonyms(table.synonyms) + _comment(table.description)
        tables.append(entry)
    lines += _clause('TABLES', tables)
    if links:
        lines += _clause(
            'RELATIONSHIPS',
            [
                f'{_identifier(link.name)} AS {_identifier(link.source)} '
                f'({", ".join(_identifier(c) for c in link.source_columns)}) '
                f'REFERENCES {_identifier(link.target)} '
                f'({", ".join(_identifier(c) for c in link.target_columns)})'
                for link in links
            ],
        )
    dimensions = [
        f'{_identifier(table.name)}.{_identifier(column.name)} AS '
        f'{_identifier(table.name)}.{_identifier(column.column)}' + _comment(column.description)
        for table in model.tables
        for column in table.columns
    ]
    if dimensions:
        lines += _clause('DIMENSIONS', dimensions)
    if model.metrics:
        lines += _clause(
            'METRICS',
            [
                f'{_identifier(metric.table)}.{_identifier(metric.name)} AS '
                f'{render(metric.expression, DIALECT)}' + _synonyms(metric.synonyms) + _comment(metric.note)
                for metric in model.metrics
            ],
        )
    lines.append(
        "  COMMENT = 'Entities, relationships and measures derived by Konstruct from the data stack.';"
    )
    return '\n'.join(lines) + '\n'


def _acyclic(model: Compiled) -> list[CompiledLink]:
    """The relationships Snowflake accepts: each that would close a cycle of references between
    tables is left out (and listed in ``model.omitted``)."""
    refers: dict[str, list[str]] = {}  # table -> the tables its kept relationships refer to
    kept = []
    for link in model.links:
        # A cycle closes when the target already reaches the source.
        stack, seen = [link.target], {link.target}
        while stack and link.source not in seen:
            for target in refers.get(stack.pop(), []):
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        if link.source in seen:
            model.omitted.append(
                f'relationship {link.name} ({link.source} to {link.target}): Snowflake allows no cycle '
                'of relationships'
            )
            continue
        kept.append(link)
        refers.setdefault(link.source, []).append(link.target)
    return kept


def _clause(keyword: str, entries: list[str]) -> list[str]:
    body = ',\n'.join(f'    {entry}' for entry in entries)
    return [f'  {keyword} (', body, '  )']


def _identifier(name: str) -> str:
    return name if plain(name, DIALECT) else '"' + name.replace('"', '""') + '"'


def _relation(source: str) -> str:
    return '.'.join(_identifier(part) for part in source.split('.'))


def _string(text: str) -> str:
    # Snowflake reads backslash escapes in single-quoted strings.
    escaped = text.replace('\\', '\\\\').replace("'", "\\'").replace('\r', '\\r').replace('\n', '\\n')
    return f"'{escaped}'"


def _comment(text: str | None) -> str:
    return f'\n      COMMENT = {_string(text.strip())}' if text and text.strip() else ''


def _synonyms(names: list[str]) -> str:
    return f'\n      WITH SYNONYMS = ({", ".join(_string(n) for n in names)})' if names else ''
