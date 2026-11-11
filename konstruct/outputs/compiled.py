"""The ontology compiled for a semantic platform: the tables, relationships and metrics that the
Snowflake semantic view and Databricks metric view exports write, and what they leave out.

It holds what the Apache Ossie export holds, so every target carries the same entities:

- **Tables**: each relation that holds an entity and each relation a metric reads, with its
  columns and their descriptions. A relation holds the entity relationships read it as holding
  (:func:`konstruct.semantics.ontology.holder`): its key is the table's primary key when the
  ontology knows it to be unique, and the unique keys of the other entities it holds are its
  unique keys.
- **Relationships**: each ontology relationship whose cardinality is known, to the primary key or
  a unique key of the table it refers to, as both platforms require.
- **Metrics**: each aggregate measure whose formula, composed down to source columns (or, for a
  metric read from an Ossie model, its own expression), reads the columns of one table, and whose
  way down passes no filter the composed formula would drop. The metric belongs to that table.

Nothing is invented: a name, description, synonym, grain or filter is written only when the
ontology or the graph holds it. What cannot be written is listed in :attr:`Compiled.omitted`,
which each export writes at its top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from konstruct.core.graph import Graph, Node
from konstruct.semantics.ontology import Measure, Ontology, holder

_PLAIN = re.compile(r'[A-Za-z][A-Za-z0-9_]*')
# Snowflake's reserved keywords (https://docs.snowflake.com/en/sql-reference/reserved-keywords),
# which need quotes as names. Quotes make a Snowflake name case sensitive, so nothing else is quoted.
SNOWFLAKE_RESERVED = frozenset(
    """ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONNECTION CONSTRAINT
    CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DATABASE DELETE
    DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN
    INCREMENT INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP
    MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS
    SAMPLE SCHEMA SELECT SET SOME START TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION
    UNIQUE UPDATE USING VALUES VIEW WHEN WHENEVER WHERE WINDOW WITH""".split()
)
# Databricks' reserved words and expression keywords
# (https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-reserved-words), and the keywords
# of standard SQL: backticks never change what a Databricks name means, so a keyword is quoted
# whether or not Databricks enforces it.
DATABRICKS_RESERVED = SNOWFLAKE_RESERVED | frozenset(
    'ANTI EXCEPT MASK SEMI DEFAULT USER DATE TIME TIMESTAMP INTERVAL LIMIT OFFSET'.split()
)
RESERVED = {'snowflake': SNOWFLAKE_RESERVED, 'databricks': DATABRICKS_RESERVED}


@dataclass
class CompiledColumn:
    """One column of a table, written as a dimension."""

    name: str
    """The dimension's identifier, unique in its table ignoring case."""
    column: str
    """The physical column it reads."""
    description: str | None = None
    """The column's description, if it has one."""
    id: str = field(default='', repr=False)
    """The column's graph id."""


@dataclass
class CompiledTable:
    """A relation, as a logical table or a metric view's source."""

    name: str
    """Its identifier, unique in the model: the relation's short name where that is free."""
    source: str
    """The physical relation (``analytics.main.customers``)."""
    entity: str | None = None
    """The entity whose instances it holds one row each of, if any."""
    key: list[str] = field(default_factory=list)
    """The physical key columns of that entity, when the ontology knows them to be unique."""
    synonyms: list[str] = field(default_factory=list)
    """The entity's name and the term that names it."""
    description: str | None = None
    """The relation's description, if it has one."""
    columns: list[CompiledColumn] = field(default_factory=list)
    """Its columns, in their order."""
    id: str = field(default='', repr=False)
    """The relation's graph id."""
    unique: list[list[str]] = field(default_factory=list)
    """The unique keys of the other entities it holds, each as its physical columns."""


@dataclass
class CompiledLink:
    """A relationship: rows of ``source`` refer to one row of ``target``."""

    name: str
    """Its identifier, unique in the model (``order_to_customer``)."""
    source: str
    """The referring table, by name."""
    target: str
    """The table referred to, by name; its columns are its entity's unique key."""
    source_columns: list[str]
    """The physical columns of ``source`` that refer."""
    target_columns: list[str]
    """The physical columns of ``target`` referred to."""
    cardinality: str
    """``many-to-one`` or ``one-to-one``."""
    tested: bool = False
    """Whether a dbt ``relationships`` test vouches for it."""


@dataclass
class CompiledMetric:
    """An aggregate measure, on the table whose columns it reads."""

    name: str
    """Its identifier, unique in its table among its columns and metrics."""
    table: str
    """The table it belongs to, by name."""
    expression: exp.Expression = field(repr=False)
    """Its formula, with each column as ``table.column`` in the warehouse's dialect."""
    title: str
    """The term's own name (``Customer lifetime value``)."""
    description: str | None = None
    """The term's definition."""
    synonyms: list[str] = field(default_factory=list)
    """The term's name and synonyms, without the identifier."""
    entity: str | None = None
    """The entity its grain names."""
    grain: str | None = None
    """The term's grain as written."""
    filters: list[str] = field(default_factory=list)
    """Filters any answer must apply, as written."""
    unit: str | None = None
    """Its unit, such as ``currency``."""
    currency: str | None = None
    """Its currency, for a monetary measure."""
    status: str = 'draft'
    """``draft``, ``agreed``, ``approved`` or ``retired``."""
    owner: str | None = None
    """Who owns the definition."""

    @property
    def note(self) -> str | None:
        """The definition with the grain, required filters and unit: what a platform's comment on
        the metric says, since neither platform has fields for them."""
        parts = [self.description.strip()] if self.description and self.description.strip() else []
        if self.grain:
            parts.append(f'Grain: {self.grain}.')
        if self.filters:
            parts.append('Required filters: ' + '; '.join(self.filters) + '.')
        if self.unit:
            parts.append(f'Unit: {self.unit}' + (f' ({self.currency}).' if self.currency else '.'))
        return ' '.join(parts) or None


@dataclass
class Compiled:
    """The ontology as tables, relationships and metrics, ready for a platform's format."""

    name: str
    """The model's identifier: the project's name."""
    dialect: str | None
    """The sqlglot dialect the metrics' expressions are read in."""
    tables: list[CompiledTable] = field(default_factory=list)
    """The tables, by name."""
    links: list[CompiledLink] = field(default_factory=list)
    """The relationships between them."""
    metrics: list[CompiledMetric] = field(default_factory=list)
    """The metrics, by table and name."""
    omitted: list[str] = field(default_factory=list)
    """What the ontology holds that is not written, each with the reason."""

    def table(self, name: str) -> CompiledTable:
        """The table called ``name``."""
        return next(t for t in self.tables if t.name == name)

    def nothing(self) -> str:
        """Why there is nothing to export: everything the ontology holds was left out, or it holds
        nothing."""
        why = '; '.join(self.omitted) or 'the ontology has no entities or measures'
        return f'nothing to export: {why}'

    def keyed(self) -> list[CompiledLink]:
        """The relationships that refer to their target's primary key or one of its unique keys,
        as both platforms require; each other is listed in :attr:`omitted`."""
        tables = {t.name: t for t in self.tables}
        kept = []
        for link in self.links:
            target = tables[link.target]
            if link.target_columns == target.key or link.target_columns in target.unique:
                kept.append(link)
                continue
            self.omitted.append(
                f'relationship {link.name} ({link.source} to {link.target}): '
                f'{link.target}.{", ".join(link.target_columns)} is not a key of {link.target}'
            )
        return kept

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'tables': [
                {
                    'name': t.name,
                    'source': t.source,
                    'entity': t.entity,
                    'key': t.key,
                    'columns': [c.column for c in t.columns],
                }
                for t in self.tables
            ],
            'relationships': [
                {
                    'name': link.name,
                    'from': link.source,
                    'to': link.target,
                    'from_columns': link.source_columns,
                    'to_columns': link.target_columns,
                    'cardinality': link.cardinality,
                }
                for link in self.links
            ],
            'metrics': [
                {'name': m.name, 'table': m.table, 'expression': m.expression.sql(dialect=self.dialect)}
                for m in self.metrics
            ],
            'omitted': self.omitted,
        }

    def __str__(self) -> str:
        lines = [
            f'{self.name}: {len(self.tables)} tables, {len(self.links)} relationships, '
            f'{len(self.metrics)} metrics'
        ]
        lines += [f'  left out: {item}' for item in self.omitted]
        return '\n'.join(lines)

    def header(self, comment: str, what: str) -> list[str]:
        """The lines that open an export: what it is and what it leaves out, as comments."""
        lines = [f'{comment} {what}, written by Konstruct from the ontology of {self.name}.']
        if self.omitted:
            lines.append(f'{comment} Left out:')
            lines += [f'{comment} - {item}' for item in self.omitted]
        return lines


def plain(name: str, dialect: str) -> bool:
    """Whether ``name`` needs no quotes as a name in ``dialect`` (``snowflake`` or ``databricks``)."""
    return bool(_PLAIN.fullmatch(name)) and name.upper() not in RESERVED[dialect]


def render(tree: exp.Expression, dialect: str) -> str:
    """``tree`` as ``dialect``'s SQL, each name quoted where that dialect needs it."""
    copy = tree.copy()
    for identifier in copy.find_all(exp.Identifier):
        identifier.set('quoted', bool(identifier.args.get('quoted')) or not plain(identifier.name, dialect))
    return copy.sql(dialect=dialect)


def compile_ontology(graph: Graph, ontology: Ontology, name: str | None = None) -> Compiled:
    """The ontology of ``graph`` as tables, relationships and metrics (see the module documentation)."""
    from konstruct.analysis.explain import Explainer
    from konstruct.semantics.definitions import slug
    from konstruct.semantics.ossie import (
        aggregate,
        composed_reason,
        identifier,
        measure_tree,
        placeholder,
        unique_name,
    )
    from konstruct.semantics.rows import Rows

    dialect = graph.meta.get('dialect')
    result = Compiled(identifier(name or 'konstruct'), dialect if isinstance(dialect, str) else None)
    explainer = Explainer(graph, qualified=True)
    rows = Rows(graph)  # once per export: it reads every test of the graph
    names: dict[str, str] = {}  # relation id -> table name
    tables: dict[str, CompiledTable] = {}  # relation id -> table

    def table_for(relation: Node) -> CompiledTable:
        if relation.id not in tables:
            # The short label where it is free, else the full relation key, as the Ossie export does.
            label, full = identifier(graph.label(relation)), identifier(relation.relation or relation.id)
            # Told apart ignoring case, as unquoted names are.
            taken = {n.lower() for n in names.values()}
            table_name = label if label.lower() not in taken else unique_name(full, taken, fold=True)
            names[relation.id] = table_name
            used: set[str] = set()
            tables[relation.id] = CompiledTable(
                name=table_name,
                source=relation.relation or graph.label(relation),
                description=relation.description or None,
                columns=[
                    CompiledColumn(
                        unique_name(identifier(c.name), used, fold=True), c.name, c.description or None, c.id
                    )
                    for c in graph.columns_of(relation.id)
                ],
                id=relation.id,
            )
        return tables[relation.id]

    def reference(column: Node) -> exp.Column | None:
        owner = graph.parent_of(column)
        if owner is None or column.kind != 'column':
            return None
        return exp.column(column.name, table=table_for(owner).name)

    # The entities each table holds, by the key columns that say so.
    held: dict[str, dict[str, Node]] = {}  # relation id -> entity name -> its key column there
    for entity in ontology.entities:
        for key_id in entity.key_ids:
            column = graph.nodes.get(key_id)
            owner = graph.parent_of(column) if column is not None else None
            if column is not None and owner is not None:
                table_for(owner)
                held.setdefault(owner.id, {}).setdefault(entity.name, column)
    entities = {e.name: e for e in ontology.entities}
    for relation_id, keyed in held.items():
        table = tables[relation_id]
        chosen = holder(keyed, graph.label(graph.nodes[relation_id]))
        if chosen is None:
            continue
        entity, key = entities[chosen], keyed[chosen]
        table.entity = chosen
        # The entity's name, and the term's where it is not the same word.
        table.synonyms = [chosen]
        if entity.term and entity.term.lower() != chosen.replace('_', ' '):
            table.synonyms.append(entity.term)
        if key.id in entity.unique_ids:
            table.key = [key.name]
        else:
            result.omitted.append(f'the key of {chosen!r} ({graph.label(key)}): it is not known to be unique')
        table.unique = [
            [column.name]
            for other, column in sorted(keyed.items())
            if other != chosen and column.id in entities[other].unique_ids
        ]

    for measure in ontology.measures:
        term = graph.nodes.get(f'term.{slug(measure.name)}')
        # Placeholders first, so that a measure left out for its formula adds no table.
        trial = measure_tree(graph, explainer, measure, placeholder, rows)
        # A metric an Ossie dataset holds belongs to it, even when it reads no column (COUNT(*)).
        owner_id = term.attrs.get('ossie_dataset') if term is not None else None
        owner = graph.nodes.get(owner_id) if isinstance(owner_id, str) else None
        reason = None
        if trial is None:
            reason = 'its formula does not reach source columns'
        elif not aggregate(trial):
            reason = 'it is not an aggregate'
        elif not list(trial.find_all(exp.Column)) and owner is None:
            reason = 'it reads no column, so no table it belongs to'
        else:
            reason = composed_reason(graph, measure, trial, rows)
        tree = measure_tree(graph, explainer, measure, reference, rows) if reason is None else None
        homes = sorted({str(c.table) for c in tree.find_all(exp.Column)}) if tree is not None else []
        if not homes and owner is not None and tree is not None:
            homes = [table_for(owner).name]
        if reason is None and tree is None:
            reason = 'a column its formula reads is not in the data stack'
        elif reason is None and len(homes) != 1:
            reason = f'it reads columns of more than one table ({", ".join(homes)})'
        if reason is not None or tree is None:
            result.omitted.append(f'measure {measure.name!r}: {reason}')
            continue
        home = next(t for t in tables.values() if t.name == homes[0])
        result.metrics.append(_metric(measure, term, tree, home, used=_used(home, result.metrics)))

    for rel in ontology.relationships:
        which = f'relationship {rel.source} to {rel.target} ({rel.via})'
        if rel.columns is None or rel.cardinality is None:
            result.omitted.append(f'{which}: the key it refers to is not known to be unique')
            continue
        many, one = graph.nodes.get(rel.columns[0]), graph.nodes.get(rel.columns[1])
        left = reference(many) if many is not None else None
        right = reference(one) if one is not None else None
        if left is None or right is None or many is None or one is None:
            result.omitted.append(f'{which}: a column it joins is not in the data stack')
            continue
        if left.table == right.table:
            result.omitted.append(f'{which}: a table refers to itself')
            continue
        result.links.append(
            CompiledLink(
                unique_name(
                    identifier(f'{rel.source}_to_{rel.target}'), {link.name for link in result.links}
                ),
                str(left.table),
                str(right.table),
                [many.name],
                [one.name],
                rel.cardinality,
                rel.tested,
            )
        )
    result.tables = sorted(tables.values(), key=lambda t: t.name)
    result.links = result.keyed()
    result.metrics.sort(key=lambda m: (m.table, m.name))
    return result


def _used(table: CompiledTable, metrics: list[CompiledMetric]) -> set[str]:
    """The names taken in ``table``, in lower case: its columns and the metrics already on it."""
    return {c.name.lower() for c in table.columns} | {
        m.name.lower() for m in metrics if m.table == table.name
    }


def _metric(
    measure: Measure, term: Node | None, tree: exp.Expression, home: CompiledTable, used: set[str]
) -> CompiledMetric:
    from konstruct.semantics.ossie import identifier, unique_name

    name = unique_name(identifier(measure.name.lower()), used, fold=True)
    synonyms = [measure.name, *(term.attrs.get('synonyms', []) if term is not None else [])]
    return CompiledMetric(
        name=name,
        table=home.name,
        expression=tree,
        title=measure.name,
        description=measure.definition or None,
        synonyms=[s for s in dict.fromkeys(str(s) for s in synonyms) if s != name],
        entity=measure.entity,
        grain=measure.grain,
        filters=list(measure.filters),
        unit=measure.unit,
        currency=measure.currency,
        status=measure.status,
        owner=measure.owner,
    )
