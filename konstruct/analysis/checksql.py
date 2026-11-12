"""`konstruct check-sql`: an ad-hoc query checked against what the project defines.

An agent or an analyst writes SQL to answer a business question; this module reads it, without
running it, and says where it disagrees with the project. Every finding must be one an analyst
would accept: an agent told that its correct SQL is wrong will break it, so a check that cannot
tell stays silent.

The query is qualified once and read scope by scope. Each output column that aggregates is a
*measure*: the aggregate that produces it (followed through CTEs, subqueries, wrappers such as
``ROUND`` or ``COALESCE(..., 0)``, each branch of a UNION and each scalar subquery), the rows it
reads (the WHERE, ON and HAVING clauses on the way, the condition of a ``FILTER`` or ``CASE`` inside
the aggregate, and the filters on the routes of the columns it reads in the graph) and its formula
composed down to source columns, as :mod:`konstruct.analysis.conflicts` composes the graph's. The
checks:

* **definitions**: a measure computed without a filter its term in ``definitions.yml`` (or an
  ontology pack) requires; a measure named for a term, or for a column of the project, that
  computes it differently, leaves out a filter the column applies or excludes rows the column
  keeps; a measure recomputed that a column already holds; a column of a retired term. A term
  matches only an output named exactly for it or a declared synonym, and a match by name alone is
  never an error;
* **joins**: a sum, count or average over a join that repeats the rows it aggregates (from the
  keys, unique tests, composite and grouped keys and relationships of the derived ontology); a
  result that repeats a measure of a coarser grain on every row of a finer one; two related
  relations joined on columns their relationship does not use; a join nothing vouches for;
* **names**: a relation or column the project does not have.

Each finding carries its fix. Where a fix changes the measure and no other column of the result
(a filter added where only the measure reads the rows it removes, or written into the measure's
aggregate otherwise; a join put back on its relationship's columns; a retired column swapped for
its replacement under the same output name), the result carries the corrected SQL. A filter is
not added where the measure ranks or limits the rows (QUALIFY, ORDER BY with LIMIT, a window over
it), since the rows kept would change too.

The same checks run over warehouse query history (:func:`check_history`): queries that compute an
approved measure differently are shadow metrics, counted by queries, runs and people.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import re
import secrets
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from konstruct.analysis.conflicts import top_function
from konstruct.analysis.explain import Explainer
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import (
    PARSE_ERRORS,
    Canonical,
    StatementLineage,
    analyse,
    build_schema,
    normal_condition,
    split_conjuncts,
    table_key,
    without_readings,
)
from konstruct.inputs.usage import HistoryQuery, QueryHistory
from konstruct.semantics.ontology import Relationship, derivation, entity_name, id_like, name_matches

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

SEVERITIES = ('error', 'warning', 'info')

RULES: dict[str, str] = {
    'missing-filter': 'A measure is computed without a filter its definition, or the column that holds it, applies.',
    'formula-differs': 'A column computes a measure differently from the column that holds it.',
    'recomputes-measure': 'A column recomputes a measure that a column of the project already holds.',
    'retired-column': 'The query reads a column bound to a retired term.',
    'fan-out': 'A sum, count or average runs over a join that repeats the rows it aggregates.',
    'mixed-grain': 'The result repeats a measure of a coarser grain on every row of a finer one.',
    'join-off-relationship': 'Two related relations are joined on columns their relationship does not use.',
    'unknown-join': 'A join that no relationship or unique key in the project vouches for.',
    'presumed-key': 'A join the check took as safe because an ontology pack presumes a column unique.',
    'unknown-relation': 'The query reads a relation the project does not have.',
    'unknown-column': 'The query reads a column its relation does not have.',
    'unparsed': 'The SQL could not be parsed.',
    'unchecked': 'A statement could be parsed but not analysed, so it was not checked.',
}
"""Every rule, with what it finds."""

SHADOW_RULES = frozenset({'missing-filter', 'formula-differs'})
"""The rules that make a query in history a shadow metric: it computes a measure differently."""

_FILTER_LABELS = ('WHERE', 'HAVING', 'QUALIFY')
_QUALIFY_ERRORS: tuple[type[Exception], ...] = (*PARSE_ERRORS, KeyError, IndexError, AttributeError)
_ANALYSIS_ERRORS: tuple[type[Exception], ...] = (*_QUALIFY_ERRORS, AssertionError)
_NOT_ADDITIVE = (exp.Min, exp.Max)
_MOST_NAMED = 20
"""A name more columns than this share is too generic to stand for one idea."""
_VERBS = {'SUM': 'adds', 'AVG': 'averages', 'COUNT': 'counts'}
_TOP_AGGREGATE = re.compile(r'(?:SUM|AVG|COUNT)\(', re.IGNORECASE)
_AGGREGATE_CALL = re.compile(
    r'\b(?:SUM|AVG|COUNT|COUNT_IF|COUNTIF|MIN|MAX|ANY_VALUE|ARRAY_AGG|STRING_AGG|LISTAGG|MEDIAN|'
    r'STDDEV\w*|VARIANCE|VAR_\w+|BOOL_\w+|APPROX_\w+)\s*\(',
    re.IGNORECASE,
)
_WINDOW = re.compile(r'\bOVER\s*\(', re.IGNORECASE)


@dataclass
class Finding:
    """One thing the query gets wrong, or may, with what to do about it."""

    rule: str
    """One of :data:`RULES`."""
    severity: str
    """``error`` (an approved definition is broken), ``warning`` or ``info``."""
    message: str
    """What is wrong."""
    fix: str
    """What to change."""
    column: str | None = None
    """The output column it concerns, when there is one."""
    term: str | None = None
    """The business term it concerns, when there is one."""
    detail: str | None = None
    """The filter that is missing or extra, or the formula the query uses: what groups findings
    in history."""
    rewrite: str | None = None
    """The change made in the corrected SQL, when the fix was safe to make."""
    evidence: list[str] = field(default_factory=list)
    """Why: the definitions, keys, tests and relationships the finding rests on."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SqlCheck:
    """The result of checking one piece of SQL against the project."""

    sql: str
    """The SQL as given."""
    findings: list[Finding] = field(default_factory=list)
    """Everything found, errors first."""
    corrected: str | None = None
    """The SQL with every safe fix applied, when there was one to apply."""
    statements: int = 0
    """Statements checked."""
    terms: list[str] = field(default_factory=list)
    """Business terms the query reads through their own columns."""
    dialect: str | None = None
    """The SQL dialect the query was read in."""

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def checked(self) -> bool:
        """Whether every statement was analysed: one that parses but cannot be analysed has an
        ``unchecked`` note, and makes this false."""
        return not any(f.rule == 'unchecked' for f in self.findings)

    @property
    def passed(self) -> bool:
        """Whether every statement was checked and nothing breaks an approved definition (no
        finding of severity ``error``): a statement never analysed does not pass."""
        return self.checked and self.count('error') == 0

    def failed(self, *, strict: bool = False) -> bool:
        """Whether the check fails: on an error, or with ``strict`` on a warning as well."""
        return not self.passed or (strict and self.count('warning') > 0)

    def summary(self) -> str:
        if not self.findings:
            return f'No findings in {_plural(self.statements, "statement")}.'
        parts = [
            _plural(self.count('error'), 'error'),
            _plural(self.count('warning'), 'warning'),
            _plural(self.count('info'), 'note'),
        ]
        return f'{", ".join(parts)} in {_plural(self.statements, "statement")}.'

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, sql_check_lines

        return show(sql_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def markdown(self) -> str:
        """The result as markdown, for a pull request comment or an agent's notes."""
        lines = [f'**{self.summary()}**']
        if self.findings:
            lines += ['', '| Severity | Rule | Finding | Fix |', '|---|---|---|---|']
            for finding in self.findings:
                where = f'`{finding.column}`: ' if finding.column else ''
                message = f'{where}{finding.message}'.replace('|', '\\|')
                fix = finding.fix.replace('|', '\\|')
                lines.append(f'| {finding.severity} | `{finding.rule}` | {message} | {fix} |')
        if self.corrected:
            lines += ['', '```sql', self.corrected, '```']
        return '\n'.join(lines) + '\n'

    def to_dict(self) -> dict[str, Any]:
        return {
            'passed': self.passed,
            'checked': self.checked,
            'summary': self.summary(),
            'statements': self.statements,
            'dialect': self.dialect,
            'terms': self.terms,
            'findings': [f.to_dict() for f in self.findings],
            'corrected': self.corrected,
        }


_PLURALS = {'query': 'queries', 'person': 'people'}


def _plural(count: int, word: str) -> str:
    return f'{count} {word if count == 1 else _PLURALS.get(word, word + "s")}'


def _capped(severity: str) -> str:
    """A severity no stronger than a warning: what a match by name alone can claim."""
    return 'warning' if severity == 'error' else severity


# The project as the checks see it


@dataclass
class _Term:
    node: Node
    status: str
    measure: bool
    """Whether the ontology derives a measure from it, with a formula to compare."""
    bound: list[Node]
    roots: list[Node]
    """The bound columns not derived from another bound column: the ones that define it."""

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def names(self) -> list[str]:
        return [self.node.name, *self.node.attrs.get('synonyms', [])]

    @property
    def severity(self) -> str:
        return 'error' if self.status == 'approved' else 'warning'

    @property
    def where(self) -> str:
        line = self.node.attrs.get('line')
        return f'{self.node.path}:{line}' if self.node.path and line else self.node.path or self.node.id


@dataclass
class _Condition:
    """One conjunct of a filter, normalised so equal filters compare equal however they are written."""

    text: str
    """As shown to people."""
    key: str
    """Columns replaced by the column whose values they copy, then put in one form
    (:func:`konstruct.core.sql.normal_condition`)."""
    tree: exp.Expr
    """As written, for rewriting."""
    columns: dict[str, Node]
    """Each column in ``tree`` (by its SQL) to the graph column it names."""
    label: str = 'WHERE'
    """The clause it comes from."""
    source: str | None = None
    """Where it is applied: the term that requires it, or the relation that applies it."""
    written: bool = False
    """Whether the query itself applies it, rather than the route of a column it reads."""
    own: bool = True
    """Whether it filters the relations the measure's own columns come from, rather than a relation
    joined to them."""
    opposite: str = ''
    """The key of the condition that keeps exactly the rows this one leaves out."""
    limited: frozenset[str] = frozenset()
    """For a filter a model applies on the nullable side of an outer join (``LEFT JOIN WHERE``, a
    filter in a ``LEFT JOIN``'s ON), the relations whose rows alone it restricts; empty for one that
    restricts every row."""


@dataclass
class _Form:
    """A formula composed down to source columns, without value-preserving wrappers and with the
    condition written into its aggregate taken out as a filter."""

    formula: str | None
    sources: frozenset[str]
    complete: bool
    conditions: list[_Condition] = field(default_factory=list)


@dataclass
class _Fan:
    """Why the rows of one source of a scope repeat."""

    relation: str
    by: str
    evidence: str
    pre_aggregate: str | None = None
    severity: str = 'warning'
    """``info`` when only the absence of a known key on the other side suggests it."""


@dataclass
class _ScopeInfo:
    unique: list[frozenset[str]] = field(default_factory=list)
    """Sets of output columns known to hold one row per value; an empty set means one row."""
    physical: dict[str, Node] = field(default_factory=dict)
    """Output columns that pass a graph column through unchanged."""
    fanned: dict[str, _Fan] = field(default_factory=dict)
    """Output columns whose rows repeat, with why."""


@dataclass
class _Join:
    """An equality between columns of two sources in one scope."""

    left: str
    left_column: str
    right: str
    right_column: str
    node: exp.EQ
    join: int | None
    """The index of the join whose ON clause holds it, or None for the WHERE clause."""
    conjunct: int


@dataclass
class _Rows:
    """The rows a measure reads: the filters on them and the relations they come from."""

    conditions: dict[str, _Condition] = field(default_factory=dict)
    tables: list[tuple[Scope, str, Node]] = field(default_factory=list)
    """Each scope, alias and relation the rows are read from, where a filter could be added."""
    nearby: set[str] = field(default_factory=set)
    """Columns (by the column they copy) that the query filters on rows tied to the measure's (a
    semi-join, an anti-join, a self-join on another column) without filtering the measure's own."""

    def merge(self, other: _Rows) -> None:
        for key, condition in other.conditions.items():
            self.conditions.setdefault(key, condition)
        self.tables += [t for t in other.tables if t not in self.tables]
        self.nearby |= other.nearby


@dataclass
class _Site:
    """One measure of the query: the aggregate that produces an output column, in its scope."""

    output: str
    scope: Scope
    core: exp.Expr
    aggregates: list[exp.Expr]
    rows: _Rows
    form: _Form
    shown: str | None
    """The formula as people read it."""
    reads: list[Node]
    """The graph columns the aggregate's value reads."""


def _from_pack(relationship: Relationship) -> str:
    """Where a relationship comes from, when an ontology pack alone gives it: the pack bound it by
    the names of the tables, and nothing in the project shows it."""
    packs = [
        e.removeprefix('ontology pack ') for e in relationship.evidence if e.startswith('ontology pack ')
    ]
    if not packs or len(packs) != len(relationship.evidence):
        return ''
    return f', as the {", ".join(sorted(packs))} pack gives it'


class Checker:
    """Checks SQL against one graph; built once, so that many queries (query history) share the
    derived ontology and composed formulas."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        self.sqlglot = Dialect.get_or_raise(self.dialect)
        catalog, schema = graph.meta.get('default_catalog'), graph.meta.get('default_schema')
        self.default_catalog = catalog if isinstance(catalog, str) and catalog else None
        self.default_db = schema if isinstance(schema, str) and schema else None
        self.schema = self._schema()
        self.derived = derivation(graph)
        self.ontology = self.derived.ontology
        self.keys: dict[str, str] = dict(self.derived.keys)
        # What a test, a key or an identifier field shows unique, and the identifiers a pack binds
        # as its source's own; never every entity key, which can be a referring column a definition
        # merely lists, shared by many rows.
        self.unique: frozenset[str] = self.derived.unique | frozenset(self.derived.presumed)
        self.explainer = Explainer(graph)
        self.composer = Explainer(graph, qualified=True)
        self.relationships = self.ontology.relationships
        self.by_columns: dict[frozenset[str], Relationship] = {}
        self.by_entities: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
        self.foreign: dict[str, Relationship] = {}
        for rel in self.relationships:
            self.by_entities[(rel.source, rel.target)].append(rel)
            if rel.columns:
                self.by_columns[frozenset(rel.columns)] = rel
                self.foreign.setdefault(rel.columns[0], rel)
        self._upstream: dict[str, set[str]] = {}
        self._origins: dict[str, str] = {}
        self._entities: dict[str, str | None] = {}
        self._virtual = 0
        self._conjuncts: dict[str, list[_Condition]] = {}
        self._forms: dict[str, _Form] = {}
        self._keys: dict[str, list[frozenset[str]]] = {}
        self._names: dict[str, list[tuple[int, Node]]] | None = None
        self._named: dict[str, list[Node]] = {}
        self._additive: set[str] | None = None
        measures = {m.name for m in self.ontology.measures if not m.expression}
        self.terms = self._terms(measures)
        self.bound_to: dict[str, list[_Term]] = defaultdict(list)
        for term in self.terms:
            for column in term.bound:
                self.bound_to[column.id].append(term)
        for foreign in list(self.foreign):
            self.foreign.setdefault(self.origin(foreign), self.foreign[foreign])

    # The graph

    def _schema(self) -> dict[str, dict[str, str]]:
        schema: dict[str, dict[str, str]] = {}
        for node in self.graph.relations():
            if node.relation:
                columns = {
                    c.name: c.data_type or 'UNKNOWN'
                    for c in self.graph.columns_of(node.id)
                    if not c.attrs.get('nested') and not c.attrs.get('documented_only')
                }
                if columns:
                    schema[node.relation] = columns
        return schema

    def _terms(self, measures: set[str]) -> list[_Term]:
        terms = []
        for node in sorted(
            (n for n in self.graph.nodes.values() if n.kind == 'term'), key=lambda n: n.name.lower()
        ):
            bound = [
                self.graph.nodes[e.target]
                for e in self.graph.out_edges(node.id, 'defines')
                if e.attrs.get('role') != 'input'
                and e.target in self.graph.nodes
                and self.graph.nodes[e.target].kind == 'column'
            ]
            ids = {c.id for c in bound}
            roots = [c for c in bound if not (self.upstream(c.id) & ids)]
            status = str(node.attrs.get('status', 'draft'))
            terms.append(_Term(node, status, node.name in measures, bound, roots))
        return terms

    def upstream(self, column_id: str) -> set[str]:
        """Every column ``column_id`` is derived from, at any distance."""
        found = self._upstream.get(column_id)
        if found is None:
            node = self.graph.nodes.get(column_id)
            steps = self.graph.trace(node) if node is not None else []
            found = {s.node.id for s in steps if s.node.kind == 'column' and s.node.id != column_id}
            self._upstream[column_id] = found
        return found

    def origin(self, column_id: str) -> str:
        """The column whose values ``column_id`` copies unchanged, through any number of copies."""
        found = self._origins.get(column_id)
        if found is None:
            current, seen = column_id, {column_id}
            while True:
                edges = self.graph.out_edges(current, 'derived_from')
                if len(edges) != 1 or edges[0].attrs.get('transform') != 'direct' or edges[0].target in seen:
                    break
                current = edges[0].target
                seen.add(current)
            found = self._origins[column_id] = current
        return found

    def entity_of(self, relation: Node) -> str | None:
        if relation.id not in self._entities:
            self._entities[relation.id] = self.derived.entity_of(relation)
        return self._entities[relation.id]

    def route(self, column: Node) -> list[Node]:
        """The relation of ``column`` and of every column it is derived from."""
        relations: dict[str, Node] = {}
        for column_id in [column.id, *sorted(self.upstream(column.id))]:
            node = self.graph.nodes.get(column_id)
            parent = self.graph.parent_of(node) if node is not None else None
            if parent is not None:
                relations.setdefault(parent.id, parent)
        return list(relations.values())

    def terms_named(self, output: str) -> list[_Term]:
        """The terms an output column is named for: exactly, or by a declared synonym."""
        return [t for t in self.terms if t.status != 'retired' and name_matches(output, *t.names)]

    def columns_named(self, output: str) -> list[Node]:
        """The computed columns of the graph named as ``output`` is, unless too many share it."""
        found = self._named.get(output)
        if found is None:
            if self._names is None:
                # By name first: only the columns of a name an output has are asked for their
                # edges, which the graph makes as they are asked for.
                self._names = defaultdict(list)
                for position, node in enumerate(self.graph.nodes.values()):
                    if node.kind == 'column' and isinstance(node.attrs.get('expression'), str):
                        self._names[node.name.lower()].append((position, node))
            named = sorted(
                (item for name, items in self._names.items() if name_matches(output, name) for item in items),
                key=lambda item: item[0],
            )
            computed: list[Node] = []
            for _, node in named:
                edges = self.graph.out_edges(node.id, 'derived_from')
                if edges and any(e.attrs.get('transform') != 'direct' for e in edges):
                    computed.append(node)
                    if len(computed) > _MOST_NAMED:
                        break
            # Each name's columns together, the names in the order their first columns come.
            by_name: dict[str, list[Node]] = {}
            for node in computed:
                by_name.setdefault(node.name.lower(), []).append(node)
            found = [n for nodes in by_name.values() for n in nodes]
            self._named[output] = found = found if len(found) <= _MOST_NAMED else []
        return found

    def names_for(self, relation: Node) -> list[str]:
        """What a relation's rows are called: its name, its entity, and the term that names the
        entity with its synonyms."""
        found = [self.graph.label(relation)]
        entity = self.entity_of(relation)
        if entity:
            found.append(entity)
            held = next((e for e in self.ontology.entities if e.name == entity), None)
            term = next((t for t in self.terms if held is not None and t.name == held.term), None)
            if term is not None:
                found += term.names
        return found

    def relation_keys(self, relation: Node) -> list[frozenset[str]]:
        """The sets of column names (lower case) that hold one row each of ``relation``: its unique
        columns, its composite keys, and the columns it groups by when one grouped query builds it."""
        found = self._keys.get(relation.id)
        if found is None:
            found = [
                frozenset({c.name.lower()}) for c in self.graph.columns_of(relation.id) if c.id in self.unique
            ]
            found += [frozenset(k) for k in self.derived.composite.get(relation.id, [])]
            grouped = self._grouped(relation)
            if grouped is not None and grouped not in found:
                found.append(grouped)
            self._keys[relation.id] = found
        return found

    def _grouped(self, relation: Node) -> frozenset[str] | None:
        """The output columns ``relation`` groups by, when it is built by one grouped query whose
        grouping columns all pass through. Grouping by a unique key of a source and other columns
        of that same source is grouping by the key."""
        # Two readings of a relation grouped by the same key group it by that key.
        groups = sorted(
            {
                'GROUP BY: ' + without_readings(str(c).removeprefix('GROUP BY: '), self.sqlglot)
                for c in relation.attrs.get('conditions', []) or []
                if str(c).startswith('GROUP BY: ')
            }
        )
        if len(groups) != 1:
            return None
        tree = self.parse(f'SELECT 1 GROUP BY {groups[0].removeprefix("GROUP BY: ")}')
        group = tree.args.get('group') if isinstance(tree, exp.Select) else None
        if group is None or not group.expressions:
            return None
        passed = {self.origin(c.id): c.name.lower() for c in self.graph.columns_of(relation.id)}
        items: list[tuple[Node, str]] = []
        for item in group.expressions:
            node = self.column_at([p.name for p in item.parts]) if isinstance(item, exp.Column) else None
            name = passed.get(self.origin(node.id)) if node is not None else None
            if node is None or name is None:
                return None
            items.append((node, name))
        # A model that groups in a CTE and joins the groups back to detail rows does not hold one
        # row per group: every column of one that does is a grouping column or an aggregate.
        grouping = {name for _, name in items}
        for column in self.graph.columns_of(relation.id):
            expression = column.attrs.get('expression')
            aggregated = isinstance(expression, str) and bool(_AGGREGATE_CALL.search(expression))
            if column.name.lower() not in grouping and not (
                aggregated and not _WINDOW.search(str(expression))
            ):
                return None
        for node, name in items:
            if node.id in self.unique and all(n.parent == node.parent for n, _ in items):
                return frozenset({name})
        return frozenset(grouping)

    def read_schema(self, tree: exp.Expr) -> dict[str, dict[str, str]]:
        """The columns of the relations ``tree`` reads: a schema of every relation in a large project
        costs more to build than the whole check."""
        found = {}
        for table in tree.find_all(exp.Table):
            key = table_key(table, self.default_catalog, self.default_db)
            if key in self.schema:
                found[key] = self.schema[key]
        return found

    def column_at(self, parts: list[str]) -> Node | None:
        """The graph column a fully qualified name (relation key, then column) refers to."""
        for split in range(len(parts) - 1, 0, -1):
            relation = self.graph.node_for_reading('.'.join(parts[:split]).lower())
            if relation is not None:
                return self.graph.column(relation.id, parts[split])
        return None

    def additive(self) -> set[str]:
        """Columns the project sums or counts as they are: amounts, quantities, a measure's inputs."""
        if self._additive is None:
            self._additive = set()
            for node in self.graph.nodes.values():
                expression = node.attrs.get('expression')
                if node.kind == 'column' and isinstance(expression, str) and _TOP_AGGREGATE.match(expression):
                    edges = self.graph.out_edges(node.id, 'derived_from')
                    if len(edges) == 1:
                        self._additive.add(edges[0].target)
        return self._additive

    def measure_column(self, column: Node) -> bool:
        """Whether a column holds an amount that a total would add up: bound to a measure term, an
        aggregate itself, or a column the project sums or counts."""
        if any(t.measure for t in self.bound_to.get(column.id, [])):
            return True
        if column.id in self.keys or column.id in self.unique or id_like(column.name):
            return False
        expression = column.attrs.get('expression')
        if isinstance(expression, str) and _TOP_AGGREGATE.match(expression):
            return True
        return column.id in self.additive() or self.origin(column.id) in self.additive()

    def describe(self, column: Node) -> str:
        """A column's label with what vouches for it: its term's status and its verified edges."""
        label = self.graph.label(column)
        notes = [f'{t.status} {t.name!r}' for t in self.bound_to.get(column.id, [])]
        if any(t.measure for t in self.bound_to.get(column.id, [])):
            edges = self.graph.out_edges(column.id, 'derived_from')
            checked = [e for e in edges if e.attrs.get('verified') == 'confirmed']
            if checked:
                notes.append(f'verified {len(checked)}/{len(edges)} edges')
        return f'{label} ({", ".join(notes)})' if notes else label

    # Conditions

    def parse(self, text: str) -> exp.Expr | None:
        try:
            return sqlglot.parse_one(text, read=self.sqlglot)
        except PARSE_ERRORS:
            return None

    def resolved(self, column: exp.Column) -> Node | None:
        return self.column_at([p.name for p in column.parts])

    def conjuncts(self, condition: str, source: str | None = None) -> list[_Condition]:
        """A filter from the graph (``WHERE: ...`` over fully qualified columns), split into its
        conjuncts; other conditions (grouping, DISTINCT, LIMIT, the equalities of a join) give none.
        Where a filter sits before a window or a LIMIT does not change the rows of the columns it
        filters. One on the nullable side of an outer join restricts only that side's rows; one in
        an inner join's ON restricts every row, as a WHERE does."""
        label, sep, text = condition.partition(': ')
        if not sep:
            return []
        label = label.split(' BEFORE ', 1)[0]
        words = label.split()
        joined = words[-2:] in (['JOIN', 'WHERE'], ['JOIN', 'ON'])
        kind = words[-1]
        side = next((w for w in words if w in ('LEFT', 'RIGHT', 'FULL')), '')
        if side in ('RIGHT', 'FULL') or (not joined and label not in _FILTER_LABELS):
            return []
        tree = self.parse(text)
        if tree is None:
            return []
        found = []
        for part in split_conjuncts(tree, between=True):
            made = self.condition(part, self.resolved, 'WHERE' if joined else label, source)
            if made is None:
                continue
            restricted = frozenset(p for n in made.columns.values() if (p := n.parent))
            if joined and kind == 'ON' and (len(restricted) != 1 or not made.columns):
                continue  # an equality between the joined relations is the join itself
            if joined and side:
                made.limited = restricted
            found.append(made)
        return found

    def condition(
        self,
        tree: exp.Expr,
        resolve: Callable[[exp.Column], Node | None],
        label: str = 'WHERE',
        source: str | None = None,
    ) -> _Condition | None:
        columns: dict[str, Node] = {}
        for column in tree.find_all(exp.Column):
            node = resolve(column)
            if node is None:
                return None
            columns[column.sql(dialect=self.sqlglot)] = node

        def swap(item: exp.Expr) -> exp.Expr:
            if isinstance(item, exp.Column):
                node = columns.get(item.sql(dialect=self.sqlglot))
                if node is not None:
                    return exp.Var(this=self.origin(node.id))
            return item

        types = {self.origin(n.id): (n.data_type or '').upper() for n in columns.values()}
        dated = {c for c, t in types.items() if t == 'DATE'}
        timed = {c for c, t in types.items() if t.startswith(('TIMESTAMP', 'DATETIME', 'TIME'))}
        untimed = {c for c, t in types.items() if t not in ('', 'UNKNOWN') and c not in dated | timed}
        normal = normal_condition(
            tree.copy().transform(swap, copy=False), self.sqlglot, dated, timed, untimed
        )
        opposite = normal_condition(exp.Not(this=normal.copy()), self.sqlglot, dated, timed, untimed)
        return _Condition(
            self.short(tree),
            normal.sql(dialect=self.sqlglot),
            tree,
            columns,
            label,
            source,
            opposite=opposite.sql(dialect=self.sqlglot),
        )

    def short(self, tree: exp.Expr) -> str:
        """``tree`` with short relation names, as the rest of Konstruct shows conditions."""
        return self.explainer.namer.text(tree.sql(dialect=self.sqlglot))

    def required(self, term: _Term) -> list[_Condition]:
        """The filters a term requires, each resolved against the project."""
        found = []
        relations = [p for c in term.bound if (p := self.graph.parent_of(c)) is not None]
        for text in term.node.attrs.get('filters', []) or []:
            tree = self.parse(str(text))
            if tree is None:
                continue

            def resolve(column: exp.Column) -> Node | None:
                if column.table:
                    matches = self.graph.resolve('.'.join(p.name for p in column.parts))
                    return next((m for m in matches if m.kind == 'column'), None)
                return next(
                    (c for r in relations if (c := self.graph.column(r.id, column.name)) is not None), None
                )

            for part in split_conjuncts(tree, between=True):
                made = self.condition(part, resolve, 'WHERE', f'term {term.name!r} ({term.where})')
                if made is not None:
                    made.text = part.sql(dialect=self.sqlglot)
                    found.append(made)
        return found

    def relation_conjuncts(self, relation: Node) -> list[_Condition]:
        """The filters of a relation's conditions, worked out once per relation."""
        found = self._conjuncts.get(relation.id)
        if found is None:
            where = self.graph.label(relation) + (f' ({relation.path})' if relation.path else '')
            found = [
                part
                for condition in relation.attrs.get('conditions', []) or []
                for part in self.conjuncts(str(condition), where)
                if part.label in _FILTER_LABELS
            ]
            self._conjuncts[relation.id] = found
        return found

    def route_filters(self, column: Node) -> list[_Condition]:
        """The filters on ``column``'s route back to source, with the relation that applies each: a
        filter on one side of an outer join only where the route reads that side."""
        route = self.route(column)
        reads = {r.id for r in route}
        return [
            part
            for relation in route
            for part in self.relation_conjuncts(relation)
            if not part.limited or part.limited & reads
        ]

    # Formulas

    def form(self, text: str | None, node: Node | None = None) -> _Form:
        """A formula over fully qualified columns, composed down to source columns; with ``node``,
        that graph column's own formula, worked out once."""
        if node is not None and node.id in self._forms:
            return self._forms[node.id]
        if node is None:
            self._virtual += 1
            node = Node(
                id=f'checksql:{self._virtual}', kind='column', name='query', attrs={'expression': text}
            )
        composed = self.composer.compose(node)
        found = _Form(None, composed.sources, composed.complete)
        if composed.tree is not None:
            formula, condition = _formula(composed.tree)
            found.formula = formula.sql(dialect=self.sqlglot)
            # The sources of the value, not of the condition taken out of it, which is compared as
            # a filter.
            read = [self.resolved(c) for c in formula.find_all(exp.Column)]
            if read and all(r is not None for r in read):
                found.sources = frozenset(r.id for r in read if r is not None)
            if condition is not None:
                parts = [
                    self.condition(p, self.resolved, 'WHERE')
                    for p in split_conjuncts(condition, between=True)
                ]
                if any(p is None for p in parts):
                    found.complete = False
                found.conditions = [p for p in parts if p is not None]
        if not node.id.startswith('checksql:'):
            self._forms[node.id] = found
        return found

    def check(self, sql: str) -> SqlCheck:
        """Check ``sql`` against the project."""
        return _Run(self, sql).run()


def reachable(
    aliases: list[str],
    equalities: list[tuple[str, str, str, str]],
    unique: dict[str, list[frozenset[str]]],
    fixed: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """For each source of a join, the sources each of its rows matches at most one row of.

    ``equalities`` are the join's ``(source, column, source, column)`` equalities and ``unique`` the
    sets of columns known to be unique in each source (an empty set: the source has one row).
    ``fixed`` holds the columns of each source an aggregate groups by, which are the same on every
    row of a group, as are the columns equated to them. Within a group, a row of ``a`` matches at
    most one row of ``b`` when the columns of ``b`` it is equated to, with those fixed, cover a unique
    set of ``b``, and through ``b`` at most one row of whatever ``b`` matches at most one of. A source
    that reaches every other keeps its grain: the join repeats none of its rows within a group."""
    matched: dict[tuple[str, str], set[str]] = defaultdict(set)
    for left, left_column, right, right_column in equalities:
        matched[(left, right)].add(right_column)
        matched[(right, left)].add(left_column)
    constant: dict[str, set[str]] = defaultdict(set)
    for alias, columns in (fixed or {}).items():
        constant[alias] |= columns
    changed = True
    while changed:
        changed = False
        for left, left_column, right, right_column in equalities:
            for a, ac, b, bc in (
                (left, left_column, right, right_column),
                (right, right_column, left, left_column),
            ):
                if ac in constant[a] and bc not in constant[b]:
                    constant[b].add(bc)
                    changed = True

    def at_most_one(a: str, b: str) -> bool:
        columns = matched.get((a, b), set()) | constant[b]
        return any(u <= columns for u in unique.get(b, []))

    found = {}
    for alias in aliases:
        reached, stack = {alias}, [alias]
        while stack:
            current = stack.pop()
            for other in aliases:
                if other not in reached and at_most_one(current, other):
                    reached.add(other)
                    stack.append(other)
        found[alias] = reached
    return found


def _drops_zeros(condition: _Condition) -> bool:
    """Whether a filter only leaves out zeros (``x <> 0``), which a sum adds nothing for."""
    tree = condition.tree
    return isinstance(tree, exp.NEQ) and any(
        isinstance(side, exp.Literal) and not side.is_string and side.name in ('0', '0.0')
        for side in (tree.this, tree.expression)
    )


def _excludes(condition: _Condition) -> bool:
    """Whether a filter leaves out rows of some kind (``<>``, ``NOT IN``, ``NOT LIKE``), as a
    definition does, rather than choosing the rows of a question (``=``, ``IN``, a date range)."""
    tree = condition.tree
    while isinstance(tree, exp.Paren):
        tree = tree.this
    if isinstance(tree, exp.NEQ):
        return True
    if isinstance(tree, exp.Like) and tree.args.get('negate'):
        return True
    if isinstance(tree, exp.Not):
        inner = tree.this
        return not (isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null))
    return False


# Formulas: what a measure computes, without what does not change its value


def _zero(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ('0', '0.0')


def _one(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ('1', '1.0')


def _null(node: exp.Expr | None) -> bool:
    return node is None or isinstance(node, exp.Null)


def _unwrap(node: exp.Expr) -> exp.Expr | None:
    """What a value-preserving wrapper wraps: ``ROUND(x, 2)``, ``CAST(x AS DOUBLE)``, ``COALESCE(x,
    0)``, ``x * 1.0``, brackets and ``OVER (...)`` give ``x``; anything else gives None."""
    inner: Any = None
    if (
        isinstance(node, (exp.Alias, exp.Paren, exp.Round))
        or (isinstance(node, exp.Cast) and node.to.is_type(*exp.DataType.NUMERIC_TYPES))
        or (isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and _zero(node.expressions[0]))
        or (isinstance(node, (exp.Mul, exp.Div)) and _one(node.expression))
        # Over a window, an aggregate computes the same measure for each partition.
        or (isinstance(node, exp.Window) and isinstance(node.this, (exp.AggFunc, exp.Filter)))
    ):
        inner = node.this
    elif isinstance(node, exp.Mul) and _one(node.this):
        inner = node.expression
    return inner if isinstance(inner, exp.Expr) else None


def _strip(node: exp.Expr) -> exp.Expr:
    while (inner := _unwrap(node)) is not None:
        node = inner
    return node


def _collapse(tree: exp.Expr) -> exp.Expr:
    """``SUM(SUM(x))`` as ``SUM(x)``, ``SUM(COUNT(x))`` as ``COUNT(x)``: re-aggregating a sum, count,
    minimum or maximum changes nothing."""

    def flatten(node: exp.Expr) -> exp.Expr:
        if isinstance(node, (exp.Sum, exp.Min, exp.Max)):
            inner = _strip(node.this)
            if type(inner) is type(node) or (isinstance(node, exp.Sum) and isinstance(inner, exp.Count)):
                return inner
        return node

    return tree.transform(flatten, copy=False)


def _formula(tree: exp.Expr) -> tuple[exp.Expr, exp.Expr | None]:
    """A formula without its value-preserving wrappers and, when it is one aggregate that counts
    only some rows (``SUM(x) FILTER (WHERE c)``, ``SUM(CASE WHEN c THEN x ELSE 0 END)``,
    ``COUNT(CASE WHEN c THEN 1 END)``, ``SUM(IF(c, 1, 0))``), the aggregate over every row and the
    condition ``c``, which is a filter on the rows it reads."""
    tree = _collapse(_strip(tree.copy()).transform(_strip, copy=False))
    condition: exp.Expr | None = None
    if isinstance(tree, exp.Filter) and isinstance(tree.this, exp.AggFunc):
        where = tree.expression
        condition = where.this if isinstance(where, exp.Where) else where
        tree = tree.this
    if isinstance(tree, exp.AggFunc) and not isinstance(tree.this, exp.Distinct):
        picked = _conditional(tree)
        if picked is not None:
            tree, when = picked
            condition = when if condition is None else exp.and_(condition, when)
    return _collapse(tree), condition


def _conditional(agg: exp.AggFunc) -> tuple[exp.Expr, exp.Expr] | None:
    """An aggregate of ``CASE WHEN c THEN x END`` as the aggregate of ``x`` and ``c``."""
    argument = _strip(agg.this) if isinstance(agg.this, exp.Expr) else None
    if isinstance(argument, exp.Case) and not argument.this and len(argument.args.get('ifs') or []) == 1:
        branch = argument.args['ifs'][0]
        when, value, default = branch.this, branch.args.get('true'), argument.args.get('default')
    elif isinstance(argument, exp.If):
        when, value, default = argument.this, argument.args.get('true'), argument.args.get('false')
    else:
        return None
    if not isinstance(value, exp.Expr):
        return None
    if isinstance(agg, exp.Sum) and _one(value) and (_zero(default) or _null(default)):
        return exp.Count(this=exp.Star()), when
    if not (_null(default) or (isinstance(agg, exp.Sum) and _zero(default))):
        return None
    if isinstance(agg, exp.Count) and isinstance(value, exp.Literal):
        return exp.Count(this=exp.Star()), when
    rebuilt = agg.copy()
    rebuilt.set('this', value.copy())
    return rebuilt, when


# The query


def _aggregates(select: exp.Select) -> Iterator[exp.AggFunc]:
    """The aggregate calls of one SELECT, outside window functions and nested queries."""
    for expression in [*select.expressions, select.args.get('having')]:
        if not isinstance(expression, exp.Expr):
            continue
        for agg in expression.find_all(exp.AggFunc):
            if _owner(agg) is select and agg.find_ancestor(exp.Window, exp.Select) is select:
                yield agg


def _literals(query: exp.Expr | None) -> list[exp.Expr] | None:
    """The values of a query that reads nothing, ``SELECT 'a'`` or ``VALUES ('a'), ('b')``."""
    query = _unnest(query)
    if isinstance(query, exp.Select) and not query.args.get('from_') and len(query.expressions) == 1:
        value = _output(query.expressions[0])
        return [value] if isinstance(value, exp.Literal) else None
    if isinstance(query, exp.Values) and all(
        isinstance(r, exp.Tuple) and len(r.expressions) == 1 and isinstance(r.expressions[0], exp.Literal)
        for r in query.expressions
    ):
        return [r.expressions[0] for r in query.expressions]
    return None


def _values_of(scope: Scope, name: str, depth: int = 0) -> list[exp.Expr] | None:
    """The literals the output ``name`` of a query takes when it only passes on a ``VALUES`` list."""
    select = scope.expression
    if depth > 10 or not isinstance(select, exp.Select):
        return None
    value = next((_output(e) for e in select.expressions if e.alias_or_name.lower() == name), None)
    if not isinstance(value, exp.Column):
        return None
    source = scope.sources.get(value.table)
    if isinstance(source, Scope) and not isinstance(source.expression, exp.Values):
        return _values_of(source, value.name.lower(), depth + 1)
    listed = getattr(select.args.get('from_'), 'this', None)
    if not isinstance(listed, exp.Values) or listed.alias_or_name != value.table:
        return None
    columns = [c.lower() for c in listed.alias_column_names]
    if value.name.lower() not in columns:
        return None
    index = columns.index(value.name.lower())
    found = [
        r.expressions[index]
        for r in listed.expressions
        if isinstance(r, exp.Tuple) and len(r.expressions) > index
    ]
    return found if found and all(isinstance(f, exp.Literal) for f in found) else None


def _constant_queries(part: exp.Expr) -> exp.Expr:
    """A condition with each query in it that reads nothing written as its values: ``x NOT IN
    (SELECT 'a')`` as ``x NOT IN ('a')``, ``x <> ALL (SELECT 'a')`` as ``x NOT IN ('a')``,
    ``x <> (SELECT 'a')`` as ``x <> 'a'``. Unchanged, the condition itself."""
    if part.find(exp.Select, exp.Values) is None:
        return part

    def fold(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.In) and node.args.get('query') is not None:
            values = _literals(node.args['query'])
            if values:
                return exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
        if isinstance(node, (exp.EQ, exp.NEQ)):
            right = node.expression
            if isinstance(right, (exp.All, exp.Any)):
                values = _literals(right.this)
                if values:
                    listed = exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
                    return listed if isinstance(node, exp.EQ) else exp.Not(this=listed)
            elif isinstance(right, exp.Subquery):
                values = _literals(right)
                if values and len(values) == 1:
                    return type(node)(this=node.this.copy(), expression=values[0].copy())
        return node

    folded = part.copy().transform(fold, copy=False)
    return part if folded.find(exp.Select) is not None else folded


def _windowed(select: exp.Select) -> Iterator[tuple[exp.Window, exp.AggFunc]]:
    """The aggregates one SELECT applies over a window, ``SUM(x) OVER (...)``, with the window."""
    for expression in select.expressions:
        for window in expression.find_all(exp.Window):
            inner = window.this.this if isinstance(window.this, exp.Filter) else window.this
            if _owner(window) is select and isinstance(inner, exp.AggFunc):
                yield window, inner


def _owner(node: exp.Expr) -> exp.Expr | None:
    return node.find_ancestor(exp.Select)


def _own_columns(expression: exp.Expr, select: exp.Select) -> list[exp.Column]:
    return [c for c in expression.find_all(exp.Column) if _owner(c) is select]


def _output(expression: exp.Expr) -> exp.Expr:
    return expression.this if isinstance(expression, exp.Alias) else expression


def _ancestors(node: exp.Expr, kind: type[exp.Expr]) -> Iterator[exp.Expr]:
    current = node.find_ancestor(kind)
    while current is not None:
        yield current
        current = current.find_ancestor(kind)


def _unnest(node: exp.Expr | None) -> exp.Expr | None:
    while isinstance(node, exp.Subquery):
        node = node.this
    return node


def _branches(scope: Scope) -> list[Scope]:
    """The SELECTs of a UNION, INTERSECT or EXCEPT, left to right; a SELECT on its own."""
    if isinstance(scope.expression, exp.Select):
        return [scope]
    return [b for child in scope.set_operation_scopes for b in _branches(child)]


def _names(scope: Scope) -> list[str]:
    """The output names of a query: those of its first SELECT."""
    branches = _branches(scope)
    if not branches:
        return []
    select = branches[0].expression
    assert isinstance(select, exp.Select)
    return [e.alias_or_name.lower() for e in select.expressions]


class _Run:
    """One check of one piece of SQL."""

    def __init__(self, checker: Checker, sql: str) -> None:
        self.checker = checker
        self.graph = checker.graph
        self.sql = sql
        self.result = SqlCheck(sql=sql, dialect=checker.dialect)
        self.seen: set[tuple[str, ...]] = set()
        self.rewritten = False
        self.mapped: dict[int, exp.Select] = {}
        self.readers: dict[int, list[Scope]] = defaultdict(list)
        self.infos: dict[int, _ScopeInfo] = {}
        self.laterals: dict[int, Scope] = {}
        self.called: dict[str, list[str]] = {}
        self.canon = Canonical(checker.sqlglot, checker.default_catalog, checker.default_db)
        self.schema = MappingSchema({}, dialect=checker.sqlglot)

    def add(self, finding: Finding, *key: str) -> None:
        identity = (finding.rule, *key) if key else (finding.rule, finding.message)
        if identity not in self.seen:
            self.seen.add(identity)
            self.result.findings.append(finding)

    def run(self) -> SqlCheck:
        checker = self.checker
        try:
            trees = [t for t in sqlglot.parse(self.sql, read=checker.sqlglot) if t is not None]
        except PARSE_ERRORS as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            self.add(
                Finding('unparsed', 'error', f'the SQL could not be parsed: {message}', 'Correct the syntax.')
            )
            return self.result
        for index, (tree, query) in enumerate(zip(trees, _with_queries(trees), strict=True), 1):
            if query is None:
                continue
            try:
                lineage = next(
                    iter(
                        analyse(
                            tree.sql(dialect=checker.sqlglot),
                            dialect=checker.dialect,
                            schema=checker.read_schema(tree),
                            default_catalog=checker.default_catalog,
                            default_db=checker.default_db,
                        )
                    ),
                    None,
                )
                if lineage is None or lineage.kind == 'unparsed':
                    continue
                self.result.statements += 1
                self.names(lineage, query)
                self.retired(self.read(lineage), query)
                self.used(lineage)
                self.statement(query)
            except _ANALYSIS_ERRORS as error:
                # SQL sqlglot reads but cannot analyse (a PIVOT in a subquery, say) is not checked,
                # and the result says so rather than claim it passed.
                self.add(
                    Finding(
                        'unchecked',
                        'info',
                        f'statement {index} could not be analysed ({type(error).__name__}), so it was not checked',
                        'Check it by hand, or write it without the construct the analysis stops at.',
                    ),
                    str(index),
                )
        return self._finish(trees)

    def _finish(self, trees: list[exp.Expr]) -> SqlCheck:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        self.result.findings.sort(key=lambda f: order[f.severity])
        if self.rewritten:
            self.result.corrected = ';\n\n'.join(
                t.sql(dialect=self.checker.sqlglot, pretty=True) for t in trees
            )
        return self.result

    def statement(self, query: exp.Query) -> None:
        """The checks that read the query scope by scope: joins, then measures."""
        checker = self.checker
        # Normalised as the dialect normalises unquoted names (Snowflake upper-cases them), so that
        # qualify resolves the query's columns against the graph's lower-case ones.
        built = build_schema(checker.read_schema(query), checker.sqlglot)
        self.schema = MappingSchema(built.mapping, dialect=checker.sqlglot, normalize=True)
        try:
            qualified = self._qualified(query)
            root = build_scope(qualified)
        except _QUALIFY_ERRORS:
            # qualify stops at a column its relation does not have; say which.
            try:
                unqualified = build_scope(query.copy())
                for scope in unqualified.traverse() if unqualified is not None else []:
                    self.unknown_qualified(scope)
            except _QUALIFY_ERRORS:
                pass
            return
        if root is None:
            return
        originals = list(query.find_all(exp.Select))
        selects = list(qualified.find_all(exp.Select))
        self.mapped = (
            {id(q): o for q, o in zip(selects, originals, strict=True)}
            if len(selects) == len(originals)
            else {}
        )
        self.readers = defaultdict(list)
        self.infos = {}
        self.laterals = {}
        scopes = list(root.traverse())
        for scope in scopes:
            for source in scope.sources.values():
                if isinstance(source, Scope):
                    self.readers[id(source)].append(scope)
            for branch in scope.set_operation_scopes:
                self.readers[id(branch)].append(scope)
        by_select = {id(s.expression): s for s in scopes}
        for scope in scopes:
            if isinstance(scope.expression, exp.Select):
                self.unknown_qualified(scope)
                self.infos[id(scope)] = self.scope(scope, scope is root)
            elif isinstance(scope.expression, exp.Lateral):
                # A LATERAL subquery is a source like any other, with the SELECT inside it.
                inner = by_select.get(id(_unnest(scope.expression.this)))
                if inner is not None and id(inner) in self.infos:
                    self.infos[id(scope)] = self.infos[id(inner)]
                    self.laterals[id(scope)] = inner
        try:
            sites = list(self.sites(root))
        except _QUALIFY_ERRORS:
            return
        for site in sites:
            self.measure(site)

    # Names

    def names(self, lineage: StatementLineage, query: exp.Query) -> None:
        graph = self.graph
        for key in lineage.reads:
            if graph.node_for_relation(key) is None:
                labels = sorted({graph.label(n) for n in graph.relations()} | set(self.checker.schema))
                close = difflib.get_close_matches(key.rsplit('.', 1)[-1], labels, n=3)
                hint = f'; did you mean {", ".join(close)}?' if close else ''
                self.add(
                    Finding(
                        'unknown-relation',
                        'warning',
                        f'{key} is not a relation of the project{hint}',
                        'Read a relation the project has, or build the graph with the warehouse that holds it.',
                    )
                )
        # A PIVOT or UNPIVOT makes new columns out of a relation's, which it does not have.
        pivoted = {
            table_key(t, self.checker.default_catalog, self.checker.default_db)
            for t in query.find_all(exp.Table)
            if t.args.get('pivots') or any(s.args.get('pivots') for s in _ancestors(t, exp.Subquery))
        }
        used = [(s.relation, s.column) for sources in lineage.columns.values() for s in sources]
        for relation_key, column in [*used, *lineage.references]:
            if relation_key not in pivoted:
                self.unknown_column(self.graph.node_for_relation(relation_key), column.split('.', 1)[0])

    def unknown_column(self, relation: Node | None, name: str) -> None:
        graph = self.graph
        if relation is None:
            return
        columns = graph.columns_of(relation.id)
        if not columns or graph.column(relation.id, name) is not None:
            return
        close = difflib.get_close_matches(name, [c.name for c in columns], n=3)
        hint = f'; did you mean {", ".join(close)}?' if close else ''
        self.add(
            Finding(
                'unknown-column',
                'warning',
                f'{graph.label(relation)} has no column {name}{hint}',
                f'Read one of the columns of {graph.label(relation)}.',
            ),
            relation.id,
            name.lower(),
        )

    def unknown_qualified(self, scope: Scope) -> None:
        """A column the query names by its relation's alias (``o.amount_total``), which the
        lineage leaves out when the relation has no such column."""
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table) and not source.args.get('pivots'):
                self.unknown_column(self.relation_of(source), column.name)
            elif isinstance(source, Scope) and column.table:
                # A CTE or subquery has the columns it selects, unless it selects * from something.
                branches = _branches(source)
                if not branches or any(
                    isinstance(b.expression, exp.Select)
                    and (b.expression.is_star or b.expression.args.get('pivots'))
                    for b in branches
                ):
                    continue
                outputs = _names(source)
                if column.name.lower() not in outputs:
                    close = difflib.get_close_matches(column.name.lower(), outputs, n=3)
                    hint = f'; did you mean {", ".join(close)}?' if close else ''
                    self.add(
                        Finding(
                            'unknown-column',
                            'warning',
                            f'{column.table} has no column {column.name}{hint}',
                            f'Read one of the columns {column.table} selects.',
                        ),
                        f'scope:{column.table}',
                        column.name.lower(),
                    )

    def node(self, relation_key: str, column: str) -> Node | None:
        relation = self.graph.node_for_relation(relation_key)
        return self.graph.column(relation.id, column) if relation is not None else None

    def read(self, lineage: StatementLineage) -> dict[str, Node]:
        read: dict[str, Node] = {}
        pairs = [(s.relation, s.column) for sources in lineage.columns.values() for s in sources]
        for relation_key, name in [*pairs, *lineage.references]:
            found = self.node(relation_key, name)
            if found is not None:
                read[found.id] = found
        return read

    def used(self, lineage: StatementLineage) -> None:
        """The terms the query reads through their own columns."""
        names = set(self.result.terms)
        for sources in lineage.columns.values():
            for source in sources:
                found = self.node(source.relation, source.column)
                if found is not None:
                    names |= {t.name for t in self._held_by(found) if t.status != 'retired'}
        self.result.terms = sorted(names)

    def _held_by(self, column: Node) -> list[_Term]:
        """The terms bound to ``column`` or to the column it copies unchanged."""
        checker = self.checker
        terms = list(checker.bound_to.get(column.id, []))
        current = column.id
        while True:
            edges = self.graph.out_edges(current, 'derived_from')
            if len(edges) != 1 or edges[0].attrs.get('transform') != 'direct':
                break
            current = edges[0].target
            terms += [t for t in checker.bound_to.get(current, []) if t not in terms]
        return terms

    # Measures: the aggregates that produce output columns

    def sites(self, root: Scope) -> Iterator[_Site]:
        names = _names(root)
        for branch in _branches(root):
            select = branch.expression
            assert isinstance(select, exp.Select)
            for index, expression in enumerate(select.expressions):
                if index < len(names):
                    yield from self.resolve(branch, expression, names[index], 0)

    def resolve(self, scope: Scope, expression: exp.Expr, name: str, depth: int) -> Iterator[_Site]:
        """The measures behind one output expression, followed through the CTEs and subqueries it
        passes through unchanged, and into a scalar subquery it is."""
        if depth > 20:
            return
        core = _strip(_output(expression))
        select = scope.expression
        if isinstance(core, exp.Column):
            source = scope.sources.get(core.table)
            if isinstance(source, Scope):
                yield from self.through(source, core.name.lower(), name, depth)
            return
        if isinstance(core, exp.Subquery):
            inner = next((s for s in scope.subquery_scopes if s.expression is core.this), None)
            if inner is not None and len(_names(inner)) == 1:
                for branch in _branches(inner):
                    assert isinstance(branch.expression, exp.Select)
                    yield from self.resolve(branch, branch.expression.expressions[0], name, depth + 1)
            return
        if not isinstance(select, exp.Select) or core.find(exp.Select) is not None:
            return  # a value that a query computes on the way is not compared
        own = {id(a) for a in _aggregates(select)} | {id(a) for _, a in _windowed(select)}
        aggregates: list[exp.Expr] = [a for a in core.find_all(exp.AggFunc) if id(a) in own]
        if aggregates:
            yield self.site(scope, core, aggregates, name)

    def through(self, source: Scope, column: str, name: str, depth: int) -> Iterator[_Site]:
        names = _names(source)
        if column not in names:
            return
        index = names.index(column)
        for branch in _branches(source):
            select = branch.expression
            assert isinstance(select, exp.Select)
            if index < len(select.expressions):
                yield from self.resolve(branch, select.expressions[index], name, depth + 1)

    def site(self, scope: Scope, core: exp.Expr, aggregates: list[exp.Expr], name: str) -> _Site:
        checker = self.checker
        select = scope.expression
        assert isinstance(select, exp.Select)
        tree = checker.parse(self.canon.expression(core, scope, self.schema))
        rows = self.rows(scope, [c for a in aggregates for c in _own_columns(a, select)] or None)
        form = _Form(None, frozenset(), False)
        shown = None
        reads: list[Node] = []
        if tree is not None:
            formula, condition = _formula(tree)
            for column in formula.find_all(exp.Column):
                node = checker.resolved(column)
                if node is not None and node not in reads:
                    reads.append(node)
            shown = checker.short(formula)
            # Composed whole, so that the columns of a FILTER or CASE count among its sources, as
            # they do for the graph's columns.
            form = checker.form(tree.sql(dialect=checker.sqlglot))
            conditions = form.conditions
            if form.formula is None and condition is not None:
                parts = [
                    checker.condition(p, checker.resolved, 'WHERE')
                    for p in split_conjuncts(condition, between=True)
                ]
                conditions = [p for p in parts if p is not None]
            for part in conditions:
                part.written = True
                rows.conditions.setdefault(part.key, part)
        return _Site(name, scope, core, aggregates, rows, form, shown, reads)

    def rows(self, scope: Scope, columns: list[exp.Column] | None, depth: int = 0) -> _Rows:
        """The filters on the rows that ``columns`` of ``scope`` come from (every row, for None) and
        the relations those rows are read from."""
        found = _Rows()
        select = scope.expression
        if depth > 20 or not isinstance(select, exp.Select):
            return found
        checker = self.checker
        sources = {alias: source for alias, (_, source) in scope.selected_sources.items()}
        aliases = {c.table for c in columns} & set(sources) if columns else set(sources)
        relations = {relation.id for a in aliases if (relation := self.relation_of(sources[a])) is not None}
        optional = {
            str(j.this.alias_or_name): str(j.side or '').upper() for j in select.args.get('joins') or []
        }
        same = self.same_rows(scope, aliases, sources, optional)
        others: dict[str, list[tuple[exp.Expr, str]]] = defaultdict(list)

        def add(part: exp.Expr, label: str, own: bool, into: _Rows) -> None:
            tree = checker.parse(self.canon.expression(part, scope, self.schema))
            for piece in split_conjuncts(tree, between=True) if tree is not None else []:
                made = checker.condition(piece, checker.resolved, label)
                if made is not None:
                    made.written, made.own = True, own
                    into.conditions.setdefault(made.key, made)

        def written(original: exp.Expr, label: str) -> None:
            part = self.listed_values(scope, _constant_queries(original))
            if part.find(exp.Select) is not None:
                self.semi(scope, original, aliases, found)
                return
            columns = [c for c in original.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
            used = {c.table for c in columns}
            if not used or len(columns) != len(list(part.find_all(exp.Column))):
                return
            siblings = {
                a
                for a in used - aliases
                if (relation := self.relation_of(sources.get(a))) is not None and relation.id in relations
            }
            if siblings - same:
                # Another alias of the same relation, not tied to the measure's rows by their key,
                # filters other rows.
                for alias in siblings - same:
                    others[alias].append((part, label))
                return
            add(part, label, used <= aliases, found)

        where = select.args.get('where')
        if isinstance(where, exp.Where):
            for part in split_conjuncts(where.this):
                written(part, 'WHERE')
        for join in select.args.get('joins') or []:
            on = join.args.get('on')
            side = str(join.side or '').upper()
            if not isinstance(on, exp.Expr) or side in ('RIGHT', 'FULL'):
                continue
            for part in split_conjuncts(on):
                if isinstance(part, exp.EQ) and all(
                    isinstance(p, exp.Column) for p in (part.this, part.expression)
                ):
                    continue  # the join's own equality
                if side == 'LEFT' and aliases != {join.this.alias_or_name}:
                    continue  # a LEFT JOIN's ON clause only filters the rows it joins
                written(part, 'WHERE')
        for key, label in (('having', 'HAVING'), ('qualify', 'QUALIFY')):
            clause = select.args.get(key)
            if isinstance(clause, exp.Expr):
                for part in split_conjuncts(clause.this):
                    written(part, label)
        aside = _Rows()
        for parts in others.values():
            for part, label in parts:
                add(part, label, False, aside)
        found.nearby |= {
            self.checker.origin(n.id) for c in aside.conditions.values() for n in c.columns.values()
        }
        # A relation joined to the measure's own (not on the optional side of a LEFT JOIN) is where a
        # filter on it goes, too.
        for alias in sorted(set(sources) - aliases - set(optional)):
            relation = self.relation_of(sources[alias])
            if relation is not None:
                found.tables.append((scope, alias, relation))
        for alias in sorted(same):
            joined = sources[alias]
            if isinstance(joined, Scope):
                tied = self.inner_rows(joined, None, depth + 1)
                found.conditions.update(
                    {k: v for k, v in tied.conditions.items() if k not in found.conditions}
                )
                found.nearby |= tied.nearby
        for alias in sorted(aliases):
            source = sources[alias]
            names = [c.name.lower() for c in columns or [] if c.table == alias]
            if isinstance(source, exp.Table):
                relation = self.relation_of(source)
                if relation is None:
                    continue
                found.tables.append((scope, alias, relation))
                nodes = [n for name in names if (n := self.graph.column(relation.id, name)) is not None]
                routed = [p for n in nodes for p in checker.route_filters(n)]
                every = (
                    [p for p in checker.relation_conjuncts(relation) if not p.limited] if not names else []
                )
                for routed_part in routed or every:
                    found.conditions.setdefault(routed_part.key, routed_part)
            elif isinstance(source, Scope):
                found.merge(self.inner_rows(source, names or None, depth + 1))
        return found

    def listed_values(self, scope: Scope, part: exp.Expr) -> exp.Expr:
        """``x IN (SELECT v FROM t)`` as ``x IN (...)`` when ``t`` is a CTE or subquery of a
        ``VALUES`` list, which reads no relation: its values are written out."""
        if part.find(exp.Select) is None:
            return part
        subs = {s.expression.sql(): s for s in scope.subquery_scopes}

        def fold(node: exp.Expr) -> exp.Expr:
            query = _unnest(node.args.get('query')) if isinstance(node, exp.In) else None
            sub = subs.get(query.sql()) if query is not None else None
            names = _names(sub) if sub is not None else []
            values = _values_of(sub, names[0]) if sub is not None and len(names) == 1 else None
            if values:
                return exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
            return node

        folded = part.copy().transform(fold, copy=False)
        return part if folded.find(exp.Select) is not None else folded

    def keyed(self, scope: Scope, alias: str, column: exp.Column) -> str | None:
        """The column ``alias.column`` copies, when it holds one row each of ``alias``'s rows: a
        key of the relation or of the subquery it is read from."""
        if column.table != alias:
            return None
        source = scope.sources.get(alias)
        unique: list[frozenset[str]] = []
        relation = self.relation_of(source)
        if relation is not None:
            unique = self.checker.relation_keys(relation)
        elif isinstance(source, Scope) and id(source) in self.infos:
            unique = self.infos[id(source)].unique
        node = self.physical(scope, column)
        if node is None or frozenset({column.name.lower()}) not in unique:
            return None
        return self.checker.origin(node.id)

    def same_rows(
        self, scope: Scope, aliases: set[str], sources: dict[str, Any], optional: dict[str, str]
    ) -> set[str]:
        """The other sources of ``scope`` joined (not optionally) to one of ``aliases`` on its key,
        through the same column: each of their rows is one of the measure's, so their filters are
        its filters."""
        select = scope.expression
        assert isinstance(select, exp.Select)
        found = set()
        for eq in self.equalities(select, set(sources)):
            for mine, mine_column, theirs, their_column in (
                (eq.left, eq.left_column, eq.right, eq.right_column),
                (eq.right, eq.right_column, eq.left, eq.left_column),
            ):
                if mine not in aliases or theirs in aliases or optional.get(theirs):
                    continue
                key = self.keyed(scope, mine, exp.column(mine_column, table=mine))
                node = self.physical(scope, exp.column(their_column, table=theirs))
                if key is not None and node is not None and self.checker.origin(node.id) == key:
                    found.add(theirs)
        return found

    def semi(self, scope: Scope, part: exp.Expr, aliases: set[str], found: _Rows) -> None:
        """A condition with a query in it: ``k IN (SELECT k ...)`` or ``EXISTS (SELECT ... WHERE
        k = outer.k)`` on the measure's key applies the subquery's filters to the measure's rows;
        any other that ties the subquery to the measure's rows only has filters nearby."""
        negated = isinstance(part, exp.Not)
        inner = part.this if negated else part
        query = next(
            (q for q in inner.find_all(exp.Select) if q.find_ancestor(exp.Select) is scope.expression), None
        )
        sub = next((s for s in scope.subquery_scopes if s.expression is query), None)
        if sub is None or not isinstance(sub.expression, exp.Select):
            return
        key: str | None = None
        tied = False
        if isinstance(inner, exp.In) and isinstance(inner.this, exp.Column) and inner.this.table in aliases:
            tied = True
            outputs = sub.expression.expressions
            value = _output(outputs[0]) if len(outputs) == 1 else None
            node = self.physical(sub, value) if isinstance(value, exp.Column) else None
            mine = self.keyed(scope, inner.this.table, inner.this)
            if node is not None and mine is not None and self.checker.origin(node.id) == mine:
                key = mine
        elif isinstance(inner, exp.Exists):
            where = sub.expression.args.get('where')
            for eq in split_conjuncts(where.this) if isinstance(where, exp.Where) else []:
                if not isinstance(eq, exp.EQ):
                    continue
                for outer, own in ((eq.this, eq.expression), (eq.expression, eq.this)):
                    if not (isinstance(outer, exp.Column) and isinstance(own, exp.Column)):
                        continue
                    if outer.table in aliases and own.table in sub.sources:
                        tied = True
                        mine = self.keyed(scope, outer.table, outer)
                        node = self.physical(sub, own)
                        if mine is not None and node is not None and self.checker.origin(node.id) == mine:
                            key = mine
        # The measure's own columns the condition compares with the query's rows: what it filters.
        compared = [
            node
            for c in part.find_all(exp.Column)
            if c.find_ancestor(exp.Select) is scope.expression
            and c.table in aliases
            and (node := self.physical(scope, c)) is not None
        ]
        if not tied and not compared:
            return
        rows = self.rows(sub, None)
        written = {k: c for k, c in rows.conditions.items() if c.written}
        if key is not None and not negated:
            for condition in written.values():
                found.conditions.setdefault(condition.key, condition)
        else:
            found.nearby |= {self.checker.origin(n.id) for c in written.values() for n in c.columns.values()}
            found.nearby |= {self.checker.origin(n.id) for n in compared if not (tied and key is not None)}

    def inner_rows(self, source: Scope, names: list[str] | None, depth: int) -> _Rows:
        """The rows the outputs ``names`` of a CTE or subquery come from (every row, for None). Of a
        UNION, only the filters every branch applies, and no relation to add a filter to."""
        outputs = _names(source)
        branches = []
        for branch in _branches(source):
            select = branch.expression
            assert isinstance(select, exp.Select)
            columns: list[exp.Column] = []
            for name in names or []:
                if name in outputs and outputs.index(name) < len(select.expressions):
                    columns += _own_columns(select.expressions[outputs.index(name)], select)
            branches.append(self.rows(branch, columns or None, depth))
        if len(branches) == 1:
            return branches[0]
        shared = set.intersection(*(set(b.conditions) for b in branches)) if branches else set()
        return _Rows({k: branches[0].conditions[k] for k in shared})

    def measure(self, site: _Site) -> None:
        """Check one measure against the terms it reads, the terms it is named for, and the
        columns of the project it is named like."""
        checker = self.checker
        bound: dict[str, _Term] = {}
        for node in site.reads:
            for term in self._held_by(node):
                if term.status != 'retired':
                    bound.setdefault(term.name, term)
        for term in bound.values():
            if term.measure:
                for condition in checker.required(term):
                    self.missing(condition, term, site, term.severity, None)
        named = [t for t in checker.terms_named(site.output) if t.name not in bound]
        for term in named:
            verdict = self.compare(site, term.roots, term) if term.measure else None
            severity = term.severity if verdict == 'same' else _capped(term.severity)
            for condition in checker.required(term):
                self.missing(condition, term, site, severity, None)
        if bound or named:
            return
        read = {n.id for n in site.reads}
        ancestors = read.union(*(checker.upstream(n) for n in read))
        same = [c for c in checker.columns_named(site.output) if c.id not in ancestors]
        if same:
            self.compare(site, same, None)

    def compare(self, site: _Site, candidates: list[Node], term: _Term | None) -> str | None:
        """Compare a measure with the columns that hold the idea it is named for: ``same`` when one
        computes it the same way from the same sources, ``differs`` when none does, None when they
        cannot be compared."""
        checker, graph = self.checker, self.graph
        mine = site.form
        if mine.formula is None or not mine.complete:
            return None
        compared: list[tuple[Node, _Form]] = []
        for candidate in sorted(candidates, key=graph.label):
            if not isinstance(candidate.attrs.get('expression'), str):
                continue
            theirs = checker.form(None, candidate)
            if theirs.formula is None or not theirs.complete:
                continue
            if term is None and not (mine.sources & theirs.sources):
                continue  # the same name over other data is another idea, not another formula
            compared.append((candidate, theirs))
        if not compared:
            return None
        idea = term.name if term is not None else site.output
        same = [(c, f) for c, f in compared if f.formula == mine.formula and f.sources == mine.sources]
        if not same:
            self.differs(site, compared, term, idea)
            return 'differs'
        present = site.rows.conditions
        verdicts = []
        for candidate, theirs in same:
            conditions = {p.key: p for p in [*checker.route_filters(candidate), *theirs.conditions]}
            missing = [p for k, p in conditions.items() if k not in present]
            extra = [p for k, p in present.items() if p.written and k not in conditions]
            verdicts.append((len(missing) + len(extra), graph.label(candidate), candidate, missing, extra))
        _, _, candidate, missing, extra = min(verdicts, key=lambda v: (v[0], v[1]))
        severity = term.severity if term is not None else 'warning'
        for condition in missing:
            self.missing(condition, term, site, severity, candidate)
        values = {checker.origin(n.id) for n in site.reads}
        # HAVING chooses groups, which is a slice of the question, not the rows the measure adds up.
        # Any exclusion is the same question, on whichever relation or column it is written; a test
        # of the values themselves is one only on the measure's own rows.
        additive = len(site.aggregates) == 1 and isinstance(site.aggregates[0], exp.Sum)
        excluded = [
            p
            for p in extra
            if p.label == 'WHERE'
            and not (additive and _drops_zeros(p))
            and (_excludes(p) or (p.own and not self._dated(p) and self._on_values(p, values)))
        ]

        def columns(conditions: list[_Condition]) -> set[str]:
            return {checker.origin(n.id) for c in conditions for n in c.columns.values()}

        # A column the definition filters is one it has a view on; any other the query leaves rows
        # out by may be the question's own choice, so that is at most a warning.
        defined = columns([*checker.route_filters(candidate), *(checker.required(term) if term else [])])
        for condition in excluded:
            tested = columns([condition])
            if tested & columns(missing):
                continue  # the missing filter's finding already names it
            self.add(
                Finding(
                    'formula-differs',
                    (severity if tested & defined else _capped(severity)) if term is not None else 'info',
                    f'{site.output} computes {idea!r} as {graph.label(candidate)} does, but with '
                    f'{condition.text}, which leaves out rows it keeps',
                    f'Drop {condition.text}, or name the column for what it computes. '
                    f'Or read {checker.describe(candidate)}.',
                    column=_shown(site.output),
                    term=term.name if term is not None else None,
                    detail=f'with {condition.text}',
                    evidence=[f'{graph.label(candidate)} = {checker.explainer.composed_text(candidate)}'],
                ),
                site.output,
                condition.key,
            )
        if not missing and not extra:
            self.add(
                Finding(
                    'recomputes-measure',
                    'info',
                    f'{site.output} recomputes {idea!r} as {graph.label(candidate)} does',
                    f'Read {checker.describe(candidate)}, so the answer stays in step with its definition.',
                    column=_shown(site.output),
                    term=term.name if term is not None else None,
                    detail=site.shown,
                ),
                site.output,
                candidate.id,
            )
        return 'same'

    def _on_values(self, condition: _Condition, values: set[str]) -> bool:
        """Whether a filter tests the values the measure adds up (``amount > 10``), which changes
        what it measures, rather than ``IS NOT NULL``, which an aggregate ignores anyway."""
        tree = condition.tree
        if isinstance(tree, exp.Not) and isinstance(tree.this, exp.Is):
            return False
        origins = {self.checker.origin(n.id) for n in condition.columns.values()}
        return bool(origins) and origins <= values

    def _dated(self, condition: _Condition) -> bool:
        """Whether a filter is on a date or time: a period of the question, not a definition."""
        return any(
            (n.data_type or '').upper().startswith(('DATE', 'TIME')) for n in condition.columns.values()
        )

    def differs(self, site: _Site, compared: list[tuple[Node, _Form]], term: _Term | None, idea: str) -> None:
        checker, graph = self.checker, self.graph
        mine = site.form
        candidate, theirs = compared[0]
        label = graph.label
        differences = []
        only_mine = sorted(label(graph.nodes[s]) for s in mine.sources - theirs.sources if s in graph.nodes)
        only_theirs = sorted(label(graph.nodes[s]) for s in theirs.sources - mine.sources if s in graph.nodes)
        if only_mine:
            differences.append(f'reads {", ".join(only_mine)}, which {label(candidate)} does not')
        if only_theirs:
            differences.append(f'leaves out {", ".join(only_theirs)}')
        top_mine = top_function(mine.formula, checker.dialect)
        top_theirs = top_function(theirs.formula, checker.dialect)
        if top_mine != top_theirs:
            differences.append(f'aggregates with {top_mine or "no aggregate"} against {top_theirs or "none"}')
        held = ', '.join(checker.describe(c) for c, _ in compared)
        self.add(
            Finding(
                'formula-differs',
                _capped(term.severity) if term is not None else 'info',
                f'{site.output} computes {idea!r} as {site.shown}, but {label(candidate)} computes it as '
                f'{checker.explainer.composed_text(candidate)}',
                f'Read {held} instead, or compute it the same way.',
                column=_shown(site.output),
                term=term.name if term is not None else None,
                detail=site.shown,
                evidence=differences,
            ),
            site.output,
            idea,
        )

    def missing(
        self, condition: _Condition, term: _Term | None, site: _Site, severity: str, held: Node | None
    ) -> None:
        """Report ``condition`` if the rows the measure reads are not filtered by it."""
        present = site.rows.conditions
        contradicted = [p for p in present.values() if p.written and p.key == condition.opposite]
        if condition.key in present and not contradicted:
            return
        name = term.name if term is not None else site.output
        if ('missing-filter', name, condition.key, site.output, str(id(site.scope))) in self.seen:
            return
        checker = self.checker
        origins = {checker.origin(n.id) for n in condition.columns.values()}
        similar = contradicted or [
            p
            for p in present.values()
            if p.written
            and p.label in _FILTER_LABELS
            and origins & {checker.origin(n.id) for n in p.columns.values()}
        ]
        if held is None:
            message = f'{name!r} requires {condition.text}, which the query does not apply'
            evidence = [f'required by {condition.source}'] if condition.source else []
        else:
            message = (
                f'{site.output} computes {name!r} as {self.graph.label(held)} does, but without its filter '
                f'{condition.text}'
            )
            evidence = [f'{condition.label} in {condition.source}'] if condition.source else []
        if similar:
            message += f'; it filters the same column with {similar[0].text}'
            severity = _capped(severity)
        elif origins & site.rows.nearby:
            # The query filters that column on rows tied to the measure's in a way the check cannot
            # follow (an anti-join, say): likely deliberate, so only a note.
            message += '; the query filters that column elsewhere, on rows tied to these'
            severity = 'info'
        rewrite = self.place(condition, site) if condition.label == 'WHERE' and not similar else None
        fix = f'Add {condition.text} to the query.' if rewrite is None else f'Apply the filter: {rewrite}.'
        if held is not None:
            fix += f' Or read {checker.describe(held)}, which applies it.'
        self.add(
            Finding(
                'missing-filter',
                severity,
                message,
                fix,
                column=_shown(site.output),
                term=term.name if term is not None else None,
                detail=f'without {condition.text}',
                rewrite=rewrite,
                evidence=evidence,
            ),
            name,
            condition.key,
            site.output,
            str(id(site.scope)),
        )

    def retired(self, read: dict[str, Node], query: exp.Query) -> None:
        checker, graph = self.checker, self.graph
        for column in read.values():
            for term in checker.bound_to.get(column.id, []):
                if term.status != 'retired':
                    continue
                related = {r.lower() for r in term.node.attrs.get('related', []) or []}
                replacements = [
                    t
                    for t in checker.terms
                    if t.status != 'retired'
                    and t.bound
                    and (t.name.lower() in related or name_matches(term.name, *t.names))
                ]
                instead = [c for t in replacements for c in t.bound]
                sibling = next((c for c in instead if c.parent == column.parent), None)
                # Read only through SELECT *, the column is passed on rather than chosen.
                starred = not any(c.name.lower() == column.name.lower() for c in query.find_all(exp.Column))
                rewrite = self.rename(query, column, sibling) if sibling is not None and not starred else None
                if instead:
                    fix = f'Read {", ".join(checker.describe(c) for c in instead)} instead.'
                else:
                    fix = f'Ask the owner of {term.name!r} what replaces it.'
                self.add(
                    Finding(
                        'retired-column',
                        'info' if starred else 'warning',
                        f'{graph.label(column)} holds {term.name!r}, which is retired'
                        + (', and SELECT * passes it on' if starred else ''),
                        fix,
                        term=term.name,
                        rewrite=rewrite,
                        evidence=[f'{term.name!r} is retired ({term.where})'],
                    ),
                    column.id,
                    term.name,
                )

    # Joins

    def physical(self, scope: Scope, column: exp.Column) -> Node | None:
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            relation = self.relation_of(source)
            return self.graph.column(relation.id, column.name) if relation is not None else None
        if isinstance(source, Scope):
            info = self.infos.get(id(source))
            return info.physical.get(column.name.lower()) if info is not None else None
        return None

    def relation_of(self, source: Any) -> Node | None:
        if isinstance(source, exp.Table):
            return self.graph.node_for_relation(
                table_key(source, self.checker.default_catalog, self.checker.default_db)
            )
        return None

    def scope(self, scope: Scope, is_root: bool) -> _ScopeInfo:
        graph, infos = self.graph, self.infos
        select = scope.expression
        assert isinstance(select, exp.Select)
        aliases = list(scope.selected_sources)
        sources = {a: scope.selected_sources[a][1] for a in aliases}
        unique: dict[str, list[frozenset[str]]] = {}
        names: dict[str, str] = {}
        for alias, source in sources.items():
            relation = self.relation_of(source)
            if relation is not None:
                unique[alias] = self.checker.relation_keys(relation)
                names[alias] = graph.label(relation)
            elif isinstance(source, Scope) and id(source) in infos:
                unique[alias] = infos[id(source)].unique
                names[alias] = f'the subquery {alias}'
            else:
                unique[alias] = []
                names[alias] = alias
        group = select.args.get('group')
        aggregates = list(_aggregates(select))
        fixed: dict[str, set[str]] = defaultdict(set)
        nodes = {a: scope.selected_sources[a][0] for a in aliases}
        unnested = {
            a
            for a, node in nodes.items()
            if isinstance(node, (exp.Unnest, exp.TableFromRows))
            or (isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Subquery))
        }
        for item in group.expressions if isinstance(group, exp.Group) else []:
            if isinstance(item, exp.Column) and item.table in sources:
                fixed[item.table].add(item.name.lower())
            elif len(tables := {c.table for c in item.find_all(exp.Column)}) == 1 and tables <= unnested:
                # Grouped by a value of the element alone (``f.value::string``), each group holds
                # one element, as grouping by the element does.
                fixed[next(iter(tables))].add(item.sql())
        equalities = self.equalities(select, set(aliases)) + self.correlated(sources)
        exploded = [a for a in aliases if a in unnested]
        # Grouped by the element, each group holds one element, which repeats no row.
        repeating = [a for a in exploded if not fixed.get(a)]
        # An ASOF join matches each row of the left side to at most one row of the side it joins
        # (whose rows can still repeat, once for each left row that matches them). That holds for
        # the grain of the join alone, not as a key of the relation.
        reaching = dict(unique)
        for join in select.args.get('joins') or []:
            if str(join.args.get('method') or '').upper() == 'ASOF':
                alias = join.this.alias_or_name
                matched = frozenset(
                    e.right_column if e.right == alias else e.left_column
                    for e in equalities
                    if e.join is not None and alias in (e.left, e.right)
                )
                reaching[alias] = [*unique.get(alias, []), matched]
        anti = self.anti_joined(select, equalities, unique)
        self.called = {
            a: self.checker.names_for(r) for a, src in sources.items() if (r := self.relation_of(src))
        }
        counted = [a for a in aliases if a not in anti and a not in exploded]
        kept = [e for e in equalities if e.left in counted and e.right in counted]
        reach = reachable(
            counted, [(e.left, e.left_column, e.right, e.right_column) for e in kept], reaching, fixed
        )
        fans: dict[str, _Fan] = {}
        preserved: list[str] = []
        for alias in counted:
            reached = reach[alias]
            if len(reached) == len(counted) and not repeating:
                preserved.append(alias)
                continue
            others = [a for a in counted if a not in reached]
            fan = self.fan(scope, alias, reached, others, kept, unique, names)
            if fan is None and repeating:
                fan = _Fan(
                    names[alias],
                    repeating[0],
                    f'{repeating[0]} unnests an array, so each {names[alias]} row repeats once per element',
                )
            if fan is not None:
                fans[alias] = fan
        self.relationships(scope, equalities, unique)
        self.presumptions(scope, kept)
        info = _ScopeInfo()
        for expression in select.expressions:
            value = _output(expression)
            if isinstance(value, exp.Column):
                found = self.physical(scope, value)
                if found is not None:
                    info.physical[expression.alias_or_name.lower()] = found
        # Over a grouped SELECT a window runs over the groups, whose rows no join repeats.
        for window, agg in [] if group is not None or aggregates else _windowed(select):
            # Within a window's partition its columns are fixed, as a group's are.
            partition = {**fixed}
            for item in window.args.get('partition_by') or []:
                if isinstance(item, exp.Column) and item.table in sources:
                    partition[item.table] = partition.get(item.table, set()) | {item.name.lower()}
            over = reachable(
                counted, [(e.left, e.left_column, e.right, e.right_column) for e in kept], reaching, partition
            )
            spread = [a for a in repeating if not partition.get(a)]
            windowed = {
                a: f
                for a in counted
                if len(over[a]) < len(counted) or spread
                if (f := fans.get(a)) is not None
            }
            self.aggregate(scope, select, agg, windowed, [a for a in counted if len(over[a]) == len(counted)])
        if group is not None or aggregates:
            for agg in aggregates:
                self.aggregate(scope, select, agg, fans, preserved)
            info.unique = self.grouped_unique(select)
            return info
        listed = [
            e
            for e in select.expressions
            if any(_owner(x) is select for x in e.find_all(exp.Explode, exp.Unnest))
        ]
        if listed:
            # An UNNEST in the SELECT list repeats each row once per element of the array.
            for alias in counted:
                fans.setdefault(
                    alias,
                    _Fan(
                        names[alias],
                        'unnested element',
                        f'{listed[0].alias_or_name} unnests an array in the SELECT list, so each {names[alias]} row '
                        'repeats once per element',
                    ),
                )
        distinct = select.args.get('distinct') is not None
        if distinct:
            # Rows made distinct on a key of a source no longer repeat that source.
            for alias in list(fans):
                if any(self.outputs(select, alias, keys) is not None for keys in unique.get(alias, [])):
                    del fans[alias]
        for expression in select.expressions:
            name = expression.alias_or_name.lower()
            for column in _own_columns(expression, select):
                fan = fans.get(column.table)
                derived = sources.get(column.table)
                if fan is None and not distinct and isinstance(derived, Scope) and id(derived) in infos:
                    fan = infos[id(derived)].fanned.get(column.name.lower())
                if fan is not None:
                    info.fanned.setdefault(name, fan)
        if distinct:
            info.unique = [frozenset(e.alias_or_name.lower() for e in select.expressions)]
        limit = select.args.get('limit')
        if (
            isinstance(limit, exp.Limit)
            and isinstance(limit.expression, exp.Literal)
            and limit.expression.name == '1'
        ):
            info.unique = [frozenset()]  # at most one row
        for alias in preserved:
            for keys in unique.get(alias, []):
                mapped = self.outputs(select, alias, keys)
                if mapped is not None and mapped not in info.unique:
                    info.unique.append(mapped)
        if is_root:
            self.grain(info)
        return info

    def outputs(self, select: exp.Select, alias: str, keys: frozenset[str]) -> frozenset[str] | None:
        """The output names that pass ``alias``'s ``keys`` through, or None if one is not selected."""
        found = set()
        for key in keys:
            name = next(
                (
                    e.alias_or_name.lower()
                    for e in select.expressions
                    if isinstance(value := _output(e), exp.Column)
                    and value.table == alias
                    and value.name.lower() == key
                ),
                None,
            )
            if name is None:
                return None
            found.add(name)
        return frozenset(found)

    def grouped_unique(self, select: exp.Select) -> list[frozenset[str]]:
        group = select.args.get('group')
        if group is None:
            return [frozenset()]  # an aggregate without GROUP BY returns one row
        names = set()
        for item in group.expressions:
            name = next(
                (e.alias_or_name.lower() for e in select.expressions if _output(e) == item),
                None,
            )
            if name is None:
                return []
            names.add(name)
        return [frozenset(names)]

    def equalities(self, select: exp.Select, aliases: set[str]) -> list[_Join]:
        found = []
        clauses: list[tuple[int | None, exp.Expr | None]] = [
            (index, join.args.get('on')) for index, join in enumerate(select.args.get('joins') or [])
        ]
        where = select.args.get('where')
        clauses.append((None, where.this if isinstance(where, exp.Where) else None))
        for index, clause in clauses:
            if not isinstance(clause, exp.Expr):
                continue
            for position, part in enumerate(split_conjuncts(clause)):
                if not isinstance(part, exp.EQ):
                    continue
                left, right = part.this, part.expression
                if (
                    isinstance(left, exp.Column)
                    and isinstance(right, exp.Column)
                    and left.table in aliases
                    and right.table in aliases
                    and left.table != right.table
                ):
                    found.append(
                        _Join(
                            left.table,
                            left.name.lower(),
                            right.table,
                            right.name.lower(),
                            part,
                            index,
                            position,
                        )
                    )
        return found

    def anti_joined(
        self, select: exp.Select, equalities: list[_Join], unique: dict[str, list[frozenset[str]]]
    ) -> set[str]:
        """The sources of an anti-join, ``LEFT JOIN p ... WHERE p.k IS NULL`` on a column of the join
        or a key of ``p``: only rows with no match are kept, so they repeat nothing."""
        where = select.args.get('where')
        nulls = {
            (part.this.table, part.this.name.lower())
            for part in (split_conjuncts(where.this) if isinstance(where, exp.Where) else [])
            if isinstance(part, exp.Is)
            and isinstance(part.expression, exp.Null)
            and isinstance(part.this, exp.Column)
        }
        found = set()
        for join in select.args.get('joins') or []:
            alias = join.this.alias_or_name
            if str(join.side or '').upper() != 'LEFT':
                continue
            joined = {
                e.right_column if e.right == alias else e.left_column
                for e in equalities
                if e.join is not None and alias in (e.left, e.right)
            }
            keys = {next(iter(k)) for k in unique.get(alias, []) if len(k) == 1}
            if any(table == alias and (name in joined or name in keys) for table, name in nulls):
                found.add(alias)
        return found

    def correlated(self, sources: dict[str, Any]) -> list[_Join]:
        """The equalities a LATERAL subquery's WHERE clause makes between its rows and those of the
        sources before it, as equalities of the join: ``LATERAL (SELECT * FROM payments p WHERE
        p.order_id = o.order_id) p`` joins on ``order_id``."""
        found = []
        for alias, source in sources.items():
            inner = self.laterals.get(id(source)) if isinstance(source, Scope) else None
            select = inner.expression if inner is not None else None
            if inner is None or not isinstance(select, exp.Select):
                continue
            where = select.args.get('where')
            for position, part in enumerate(
                split_conjuncts(where.this) if isinstance(where, exp.Where) else []
            ):
                if not isinstance(part, exp.EQ):
                    continue
                for own, outer in ((part.this, part.expression), (part.expression, part.this)):
                    if not (isinstance(own, exp.Column) and isinstance(outer, exp.Column)):
                        continue
                    if own.table not in inner.sources or outer.table not in sources or outer.table == alias:
                        continue
                    name = next(
                        (
                            e.alias_or_name.lower()
                            for e in select.expressions
                            if isinstance(value := _output(e), exp.Column)
                            and value.table == own.table
                            and value.name.lower() == own.name.lower()
                        ),
                        None,
                    )
                    if name is not None:
                        found.append(
                            _Join(outer.table, outer.name.lower(), alias, name, part, None, position)
                        )
        return found

    def fan(
        self,
        scope: Scope,
        alias: str,
        reached: set[str],
        others: list[str],
        equalities: list[_Join],
        unique: dict[str, list[frozenset[str]]],
        names: dict[str, str],
    ) -> _Fan | None:
        """Why ``alias``'s rows repeat, when the project gives evidence that they do."""
        graph = self.graph
        for other in others:
            pairs = [
                (eq.left_column, eq.right_column) if eq.right == other else (eq.right_column, eq.left_column)
                for eq in equalities
                if other in (eq.left, eq.right) and ({eq.left, eq.right} - {other}) & reached
            ]
            if not pairs:
                continue
            columns = sorted({theirs for _, theirs in pairs})
            mine = {m for m, _ in pairs}
            for _, theirs in pairs:
                node = self.physical(scope, exp.column(theirs, table=other))
                if node is None:
                    continue
                rel = self.checker.foreign.get(node.id) or self.checker.foreign.get(
                    self.checker.origin(node.id)
                )
                if rel is not None:
                    return _Fan(
                        names[alias],
                        names[other],
                        f'{graph.label(node)} refers to {rel.target} ({rel.via}){_from_pack(rel)}, so many '
                        f'{names[other]} rows can match one {names[alias]} row',
                        f'{names[other]} to one row per {", ".join(columns)}',
                    )
            keys = sorted((sorted(k) for k in unique.get(other, []) if k), key=lambda k: (len(k), k))
            if keys:
                key = ', '.join(keys[0])
                return _Fan(
                    names[alias],
                    names[other],
                    f'{names[other]} has one row per {key}{self.presumption(scope, other, keys[0])}, not per '
                    f'{", ".join(columns)}, so many of its rows can match one {names[alias]} row',
                    f'{names[other]} to one row per {", ".join(columns)}',
                )
            matched = next((k for k in unique.get(alias, []) if k and k <= mine), None)
            if matched is not None:
                return _Fan(
                    names[alias],
                    names[other],
                    f'{names[alias]} is joined on its key{self.presumption(scope, alias, sorted(matched))}, and '
                    f'nothing says {names[other]} has one row per {", ".join(columns)}',
                    f'{names[other]} to one row per {", ".join(columns)}',
                    severity='info',
                )
        return None

    def presumed(self, scope: Scope, alias: str, column: str) -> tuple[Node, str] | None:
        """The column ``alias.column`` with the pack that presumes it unique, when only a pack
        does: no test, primary key or identifier field says so."""
        node = self.physical(scope, exp.column(column, table=alias))
        if node is None:
            return None
        pack = self.checker.derived.presumed.get(node.id)
        if pack is None or node.id in self.checker.derived.unique:
            return None
        return node, pack

    def presumption(self, scope: Scope, alias: str, key: list[str]) -> str:
        """What a key that decides a finding rests on, when a pack presumes it rather than a test
        showing it: `` (id presumed unique by the stripe pack)``."""
        found = [(name, p[1]) for name in key if (p := self.presumed(scope, alias, name)) is not None]
        if not found:
            return ''
        packs = sorted({pack for _, pack in found})
        names = ', '.join(name for name, _ in found)
        return f' ({names} presumed unique by the {", ".join(packs)} pack{"s" if len(packs) > 1 else ""})'

    def presumptions(self, scope: Scope, equalities: list[_Join]) -> None:
        """A note for each join the check took as safe on a column only a pack presumes unique, so
        a passing check says what it assumed."""
        for eq in equalities:
            for alias, column, other, other_column in (
                (eq.left, eq.left_column, eq.right, eq.right_column),
                (eq.right, eq.right_column, eq.left, eq.left_column),
            ):
                found = self.presumed(scope, alias, column)
                theirs = self.physical(scope, exp.column(other_column, table=other))
                if found is None or (theirs is not None and theirs.id in self.checker.unique):
                    continue
                node, pack = found
                label = self.graph.label(node)
                self.add(
                    Finding(
                        'presumed-key',
                        'info',
                        f'the join {eq.node.sql(dialect=self.checker.sqlglot)} is taken not to repeat rows '
                        f'because the {pack} ontology pack presumes {label} unique; nothing has tested it',
                        f'Add a unique test or a primary key on {label}, so the check rests on it.',
                        evidence=[f'{label}: the identifier of the source system the {pack} pack describes'],
                    ),
                    node.id,
                )

    def aggregate(
        self, scope: Scope, select: exp.Select, agg: exp.AggFunc, fans: dict[str, _Fan], preserved: list[str]
    ) -> None:
        if isinstance(agg, _NOT_ADDITIVE) or isinstance(agg.this, exp.Distinct):
            return
        if not isinstance(agg, (exp.Sum, exp.Avg, exp.Count)):
            return
        if isinstance(agg.this, exp.Star) or (
            isinstance(agg, exp.Count) and isinstance(agg.this, exp.Literal)
        ):
            self.count_rows(agg, fans, preserved, self.called)
            return
        for column in _own_columns(agg, select):
            fan = fans.get(column.table)
            source = scope.sources.get(column.table)
            if fan is None and isinstance(source, Scope) and id(source) in self.infos:
                fan = self.infos[id(source)].fanned.get(column.name.lower())
            if fan is None:
                continue
            node = self.physical(scope, column)
            what = self.graph.label(node) if node is not None else f'{column.table}.{column.name}'
            function = agg.key.upper()
            text = agg.sql(dialect=self.checker.sqlglot)
            fix = (
                f'Aggregate {fan.pre_aggregate} in a subquery before the join, or aggregate '
                f'{fan.relation} on its own and join the result.'
                if fan.pre_aggregate
                else f'Aggregate {fan.relation} on its own and join the result.'
            )
            self.add(
                Finding(
                    'fan-out',
                    fan.severity,
                    f'{text} {_VERBS.get(function, "aggregates")} {what} once per matching {fan.by} row',
                    fix,
                    column=_output_name(agg),
                    detail=text,
                    evidence=[fan.evidence],
                ),
                text,
            )
            return

    def count_rows(
        self, agg: exp.AggFunc, fans: dict[str, _Fan], preserved: list[str], called: dict[str, list[str]]
    ) -> None:
        """``COUNT(*)`` over a join counts the rows of the source no other repeats. It is wrong when
        it is named for a source whose rows the join repeats (``orders_placed``, ``order_count``:
        by the relation, its entity, or the term that names the entity, a synonym or a word of the
        name), and not for one whose rows it keeps; or when every source's rows repeat, so that it
        counts pairs of rows and no one thing."""
        name = _output_name(agg)
        words = [w for w in re.split(r'[^a-z0-9]+', (name or '').lower()) if w]

        def named_for(alias: str) -> bool:
            return any(
                name_matches(w, c) for w in [*([name] if name else []), *words] for c in called.get(alias, [])
            )

        if any(named_for(a) for a in preserved):
            return
        named = [a for a in fans if named_for(a)]
        chosen = named or ([] if preserved else sorted(fans))
        if not chosen:
            return
        fan = fans[chosen[0]]
        text = agg.sql(dialect=self.checker.sqlglot)
        self.add(
            Finding(
                'fan-out',
                fan.severity,
                f'{text} counts each {fan.relation} row once per matching {fan.by} row',
                f'Count DISTINCT the key of {fan.relation}, or aggregate {fan.relation} on its own and join '
                'the result.',
                column=name,
                detail=text,
                evidence=[fan.evidence],
            ),
            text,
        )

    def grain(self, info: _ScopeInfo) -> None:
        for name, fan in info.fanned.items():
            column = info.physical.get(name)
            if column is None or not self.checker.measure_column(column):
                continue
            self.add(
                Finding(
                    'mixed-grain',
                    fan.severity,
                    f'{self.graph.label(column)} has one value per {fan.relation} row, but the result has '
                    f'one row per {fan.by} row, so the value repeats and a total of this column counts it '
                    'more than once',
                    f'Aggregate {fan.by} first, or select {self.graph.label(column)} in a query at '
                    f'{fan.relation} grain.',
                    column=name,
                    evidence=[fan.evidence],
                ),
                name,
            )

    def relationships(
        self, scope: Scope, equalities: list[_Join], unique: dict[str, list[frozenset[str]]]
    ) -> None:
        checker, graph = self.checker, self.graph
        for eq in equalities:
            if eq.join is None:
                continue
            a = self.physical(scope, exp.column(eq.left_column, table=eq.left))
            b = self.physical(scope, exp.column(eq.right_column, table=eq.right))
            if a is None or b is None or frozenset({a.id, b.id}) in checker.by_columns:
                continue
            ka, kb = checker.keys.get(a.id), checker.keys.get(b.id)
            if ka is not None and ka == kb:
                continue
            verdict: tuple[str, Relationship | None, Node, Node] | None = None
            for near, far, far_key in ((a, b, kb), (b, a, ka)):
                if far_key is None:
                    continue
                parent = graph.parent_of(near)
                entity = checker.entity_of(parent) if parent is not None else None
                rels = checker.by_entities.get((entity, far_key), []) if entity else []
                if rels:
                    wanted = {graph.nodes[r.columns[0]].name.lower() for r in rels if r.columns}
                    if near.name.lower() in wanted:
                        verdict = ('follows', None, near, far)
                        break
                    verdict = ('wrong', rels[0], near, far)
                    break
                if near.name.lower() == far.name.lower() or entity_name(near.name) == far_key:
                    verdict = ('follows', None, near, far)
                    break
            if verdict is not None and verdict[0] == 'follows':
                continue
            if verdict is not None and verdict[1] is not None:
                rel, near, far = verdict[1], verdict[2], verdict[3]
                near_alias = eq.left if near is a else eq.right
                far_alias = eq.right if near is a else eq.left
                assert rel.columns is not None
                wanted_name = graph.nodes[rel.columns[0]].name
                parent = graph.parent_of(near)
                rewrite = None
                if parent is not None and graph.column(parent.id, wanted_name) is not None:
                    rewrite = self.rejoin(scope, eq, near_alias, wanted_name, far_alias, far.name)
                fix = f'Join on {rewrite}.' if rewrite else f"Join on the relationship's columns: {rel.via}."
                self.add(
                    Finding(
                        'join-off-relationship',
                        'warning',
                        f'the join {eq.node.sql(dialect=checker.sqlglot)} matches {graph.label(near)} to '
                        f'the key of {rel.target}, but {rel.source} refers to {rel.target} through {rel.via}',
                        fix,
                        rewrite=f'join on {rewrite}' if rewrite else None,
                        evidence=[f'relationship {rel.source} -> {rel.target}: {", ".join(rel.evidence)}'],
                    ),
                    eq.node.sql(),
                )
                continue
            mismatch = self.mismatched(a, b)
            if mismatch is not None:
                self.add(mismatch, a.id, b.id)
                continue
            left, right = frozenset({eq.left_column}), frozenset({eq.right_column})
            if left in unique.get(eq.left, []) or right in unique.get(eq.right, []):
                continue  # rows of the other side cannot repeat
            self.add(
                Finding(
                    'unknown-join',
                    'info',
                    f'neither {graph.label(a)} nor {graph.label(b)} is known to be unique and no relationship '
                    'links them, so the join may repeat rows on both sides',
                    'Check the join columns, or add a unique or relationships test that says how they relate.',
                ),
                a.id,
                b.id,
            )

    def mismatched(self, a: Node, b: Node) -> Finding | None:
        """A join of one entity's key to a column named for another entity, which no relationship
        links: ``payments.payment_id = customers.customer_id``."""
        checker, graph = self.checker, self.graph
        entities = {e.name for e in checker.ontology.entities}
        for near, far in ((a, b), (b, a)):
            key = checker.keys.get(far.id)
            named = entity_name(near.name) if id_like(near.name) else None
            if key is None or named is None or named == key or named not in entities:
                continue
            return Finding(
                'unknown-join',
                'warning',
                f'the join matches {graph.label(near)}, which identifies a {named}, to {graph.label(far)}, '
                f'the key of {key}, and no relationship links {named} to {key}',
                f'Join {graph.label(far)} to a column that refers to {key}.',
            )
        return None

    # Rewrites

    def original(self, select: exp.Expr) -> exp.Select | None:
        return self.mapped.get(id(select))

    def rejoin(
        self, scope: Scope, eq: _Join, near_alias: str, near_column: str, far_alias: str, far_column: str
    ) -> str | None:
        """Put a join back on its relationship's columns, in the query as written."""
        original = self.original(scope.expression)
        if original is None or eq.join is None:
            return None
        joins = original.args.get('joins') or []
        if eq.join >= len(joins) or not isinstance(joins[eq.join].args.get('on'), exp.Expr):
            return None
        parts = split_conjuncts(joins[eq.join].args['on'])
        if eq.conjunct >= len(parts) or not isinstance(parts[eq.conjunct], exp.EQ):
            return None
        replacement = exp.EQ(
            this=exp.column(near_column, table=near_alias), expression=exp.column(far_column, table=far_alias)
        )
        parts[eq.conjunct].replace(replacement)
        self.rewritten = True
        return replacement.sql(dialect=self.checker.sqlglot)

    def alone(self, scope: Scope, site: _Site, depth: int = 0) -> bool:
        """Whether filtering the rows of ``scope`` changes the measure ``site`` and nothing else:
        every aggregate those rows reach is the measure's, and none reach the result unaggregated."""
        select = scope.expression
        if depth > 20:
            return False
        if isinstance(select, exp.Select) and (
            select.args.get('group') is not None or any(_aggregates(select))
        ):
            mine = {id(a) for a in site.aggregates}
            return all(id(a) in mine for a in _aggregates(select))
        readers = self.readers.get(id(scope), [])
        return bool(readers) and all(self.alone(r, site, depth + 1) for r in readers)

    def ranks(self, scope: Scope, site: _Site, depth: int = 0, names: set[str] | None = None) -> bool:
        """Whether the measure decides which rows the result keeps or how it orders them: through
        HAVING, QUALIFY, ORDER BY with LIMIT or a window over it, in its scope, or a filter or join
        on it in a scope that reads it. A correction would change those rows too, so none is made."""
        select = scope.expression
        if depth > 20:
            return True
        if names is None:
            # The measure's own output, by the name the query gives it and the one it has here.
            names = {site.output}
            if isinstance(select, exp.Select):
                names |= {
                    e.alias_or_name.lower()
                    for e in select.expressions
                    if any(n is site.core for n in e.walk())
                }
        if isinstance(select, exp.Select):
            mine = {a.sql() for a in site.aggregates}
            windows = [w for w in select.find_all(exp.Window) if _owner(w) is select]
            clauses = [select.args.get('having')]
            if depth:
                clauses += [
                    select.args.get('where'),
                    *(j.args.get('on') for j in select.args.get('joins') or []),
                ]
            read = [
                n for c in clauses if isinstance(c, exp.Expr) for n in c.find_all(exp.AggFunc, exp.Column)
            ]
            if (
                select.args.get('qualify') is not None
                or (select.args.get('order') is not None and select.args.get('limit') is not None)
                or any(a.sql() in mine for w in windows for a in w.find_all(exp.AggFunc))
                or any(c.name.lower() in names for w in windows for c in w.find_all(exp.Column))
                or any(
                    (isinstance(n, exp.AggFunc) and n.sql() in mine)
                    or (isinstance(n, exp.Column) and n.name.lower() in names)
                    for n in read
                )
            ):
                return True
        return any(self.ranks(r, site, depth + 1, names) for r in self.readers.get(id(scope), []))

    def visible(self, scope: Scope, wanted: set[str]) -> dict[str, tuple[str, str]] | None:
        """For each column in ``wanted`` (by the column it copies), the one source of ``scope`` and
        the column of it that holds it, or None if one is not there exactly once."""
        checker, graph = self.checker, self.graph
        found: dict[str, list[tuple[str, str]]] = defaultdict(list)
        original = self.original(scope.expression)
        for alias, (_, source) in scope.selected_sources.items():
            shown = self.written_alias(original, alias)
            relation = self.relation_of(source)
            if relation is not None:
                for column in graph.columns_of(relation.id):
                    if checker.origin(column.id) in wanted:
                        found[checker.origin(column.id)].append((shown, column.name))
            elif isinstance(source, Scope) and id(source) in self.infos:
                for name, node in self.infos[id(source)].physical.items():
                    if checker.origin(node.id) in wanted:
                        found[checker.origin(node.id)].append((shown, name))
        if set(found) != wanted or any(len(v) != 1 for v in found.values()):
            return None
        return {k: v[0] for k, v in found.items()}

    def written_alias(self, original: exp.Select | None, alias: str) -> str:
        """``alias`` as the query writes it: qualify names an unaliased table by its name, and in
        Snowflake upper-cases it."""
        tables = [] if original is None else [*original.find_all(exp.Table)]
        return next((t.alias_or_name for t in tables if t.alias_or_name.lower() == alias.lower()), alias)

    def place(self, condition: _Condition, site: _Site) -> str | None:
        """Add a missing filter in the query as written. Into the WHERE clause (or the ON clause of
        the LEFT JOIN) of the scope that reads the relation it filters when the measure's aggregate
        has no GROUP BY, whose groups the filter would remove, and only the measure reads the rows
        it removes; otherwise into the measure's aggregate, when its scope can read the filtered
        columns; otherwise not at all."""
        checker = self.checker
        wanted = {checker.origin(n.id) for n in condition.columns.values()}
        select = site.scope.expression
        assert isinstance(select, exp.Select)
        if self.ranks(site.scope, site):
            return None
        if select.args.get('group') is None:
            targets = [
                (scope, alias)
                for scope, alias, relation in site.rows.tables
                if wanted <= {checker.origin(c.id) for c in self.graph.columns_of(relation.id)}
            ]
            unique = list({(id(s), a): (s, a) for s, a in targets}.values())
            if len(unique) == 1 and self.alone(unique[0][0], site):
                scope, alias = unique[0]
                original = self.original(scope.expression)
                mapping = self.visible(scope, wanted)
                if original is not None and mapping is not None:
                    added = self._condition_in(condition, mapping)
                    return self._where(original, alias, added)
        mapping = self.visible(site.scope, wanted)
        if mapping is None:
            return None
        return self.wrap(site, self._condition_in(condition, mapping))

    def _condition_in(self, condition: _Condition, mapping: dict[str, tuple[str, str]]) -> exp.Expr:
        checker = self.checker

        def swap(item: exp.Expr) -> exp.Expr:
            if isinstance(item, exp.Column):
                node = condition.columns.get(item.sql(dialect=checker.sqlglot))
                if node is not None:
                    alias, name = mapping[checker.origin(node.id)]
                    return exp.column(name, table=alias)
            return item

        return condition.tree.copy().transform(swap, copy=False)

    def _where(self, original: exp.Select, alias: str, added: exp.Expr) -> str | None:
        text = added.sql(dialect=self.checker.sqlglot)
        joins = original.args.get('joins') or []
        if any(str(j.side).upper() in ('RIGHT', 'FULL') for j in joins):
            return None
        left = [
            j
            for j in joins
            if str(j.side).upper() == 'LEFT' and j.this.alias_or_name.lower() == alias.lower()
        ]
        if left:
            if not isinstance(left[0].args.get('on'), exp.Expr):
                return None
            left[0].set('on', exp.and_(left[0].args['on'], added))
            self.rewritten = True
            return f'{text} added to the ON clause of the join to {left[0].this.alias_or_name}'
        original.where(added, append=True, copy=False)
        self.rewritten = True
        return f'{text} added to the WHERE clause'

    def wrap(self, site: _Site, added: exp.Expr) -> str | None:
        """Write the filter into the measure's one aggregate, so the other columns and the groups
        keep their rows: ``FILTER (WHERE ...)``, written as ``IFF`` or ``IF`` in a dialect without it."""
        select = site.scope.expression
        if len(site.aggregates) != 1 or not isinstance(select, exp.Select):
            return None
        original = self.original(select)
        if original is None:
            return None
        mine = list(_aggregates(select))
        theirs = list(_aggregates(original))
        if len(mine) != len(theirs):
            return None
        agg = theirs[[id(a) for a in mine].index(id(site.aggregates[0]))]
        if type(agg) is not type(site.aggregates[0]):
            return None
        holder = agg.parent
        if isinstance(holder, exp.Filter):
            where = holder.expression
            if not isinstance(where, exp.Where):
                return None
            where.set('this', exp.and_(where.this, added))
        else:
            agg.replace(exp.Filter(this=agg.copy(), expression=exp.Where(this=added)))
        self.rewritten = True
        text = added.sql(dialect=self.checker.sqlglot)
        return f'{text} applied to {agg.sql(dialect=self.checker.sqlglot)} alone'

    def _qualified(self, query: exp.Query) -> exp.Expr:
        """A copy of ``query`` with every column qualified by its source, as the build qualifies."""
        checker = self.checker
        kwargs: dict[str, Any] = {
            'dialect': checker.sqlglot,
            'schema': self.schema,
            'validate_qualify_columns': False,
            'identify': False,
        }
        if checker.default_db:
            kwargs['db'] = checker.default_db
            if checker.default_catalog:
                kwargs['catalog'] = checker.default_catalog
        return qualify(query.copy(), **kwargs)

    def rename(self, query: exp.Query, old: Node, new: Node) -> str | None:
        """Swap a column for another of the same relation in the query as written, keeping the
        output's name, and leaving a clause that refers to that name as it is; not when the column
        passes out of a CTE or subquery, or the replacement is read in the same SELECT."""
        relation = self.graph.parent_of(old)
        if relation is None or relation.relation is None:
            return None
        edits: list[exp.Column] = []
        for select in query.find_all(exp.Select):
            tables = [
                *(j.this for j in select.args.get('joins') or []),
                getattr(select.args.get('from_'), 'this', None),
            ]
            aliases = {t.alias_or_name: t for t in tables if isinstance(t, exp.Table)}
            mine = {
                a
                for a, t in aliases.items()
                if table_key(t, self.checker.default_catalog, self.checker.default_db) == relation.relation
            }
            if not mine:
                continue
            nested = select.find_ancestor(exp.CTE, exp.Subquery) is not None

            single = len(aliases) == 1

            def ours(
                column: exp.Column,
                name: str,
                select: exp.Select = select,
                mine: set[str] = mine,
                single: bool = single,
            ) -> bool:
                return (
                    _owner(column) is select
                    and column.name.lower() == name
                    and (column.table in mine or (not column.table and single))
                )

            outputs = [_output(e) for e in select.expressions]
            if any(ours(c, new.name.lower()) for c in select.find_all(exp.Column)):
                return None  # the replacement is read beside it, so the swap would merge the two
            named = {e.alias_or_name.lower() for e in select.expressions}
            for column in select.find_all(exp.Column):
                if not ours(column, old.name.lower()):
                    continue
                clause = column.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify, exp.Select)
                if not column.table and column.name.lower() in named and not isinstance(clause, exp.Select):
                    continue  # the output's name, which the swap keeps
                if nested and any(column is o for o in outputs):
                    return None  # the column passes out under its name to the query around it
                edits.append(column)
        if not edits:
            return None
        for column in edits:
            renamed = column.copy()
            renamed.set('this', exp.to_identifier(new.name))
            parent = column.parent
            if isinstance(parent, exp.Select) and any(column is e for e in parent.expressions):
                column.replace(exp.alias_(renamed, old.name))
            else:
                column.replace(renamed)
        self.rewritten = True
        return f'{old.name} read from {new.name}'


def _output_name(node: exp.Expr) -> str | None:
    """The output column an expression in a SELECT list belongs to."""
    current: exp.Expr | None = node
    while current is not None and not isinstance(current.parent, exp.Select):
        current = current.parent
    if current is None:
        return None
    return _shown(current.alias_or_name.lower())


def _shown(name: str | None) -> str | None:
    """An output column's name, or None for one the query left unnamed (sqlglot's ``_col_0``)."""
    return None if not name or re.fullmatch(r'_col_\d+', name) else name


def _with_queries(trees: list[exp.Expr]) -> Iterator[exp.Query | None]:
    """The query each statement runs: itself, or the SELECT of a CREATE ... AS or INSERT."""
    for tree in trees:
        if isinstance(tree, exp.Query):
            yield tree
        elif isinstance(tree, (exp.Create, exp.Insert)) and isinstance(tree.expression, exp.Query):
            yield tree.expression
        else:
            yield None


def check_sql(graph: Graph, sql: str) -> SqlCheck:
    """Check ``sql`` against the project ``graph`` describes, without running it."""
    return Checker(graph).check(sql)


# Query history


@dataclass
class ShadowMetric:
    """One way that queries in history compute a measure differently from its definition."""

    term: str
    """The measure, or the output name it was computed under when no term holds it."""
    status: str | None
    """The term's status, or None for an idea with no term."""
    rule: str
    """``missing-filter`` or ``formula-differs``."""
    detail: str
    """What they do differently: the filter left out or added, or the formula used."""
    fix: str
    """What to do instead, from the first query found."""
    queries: int = 0
    """Distinct query texts that compute it this way."""
    runs: int = 0
    """Runs of those queries."""
    users: int = 0
    """Distinct people who ran them."""
    people: list[str] = field(default_factory=list)
    """Who ran them, by name; empty when the check pseudonymises."""
    example: str = ''
    """The text of the query run most often; with its literals left out when the check
    pseudonymises, since they can identify a customer or a person."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Adoption:
    """How many queries read a term through its own columns."""

    term: str
    """The business term."""
    queries: int = 0
    """Distinct query texts that read it."""
    runs: int = 0
    """Runs of those queries."""
    users: int = 0
    """Distinct people who ran them."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoryCheck:
    """The checks run over query history: shadow metrics, and how widely each term's own columns are read."""

    queries: int = 0
    """Distinct query texts checked."""
    runs: int = 0
    """Runs of those queries."""
    unparsed: int = 0
    """Query texts that could not be parsed."""
    redacted: int = 0
    """Runs whose text the warehouse withheld, which could not be checked."""
    shadow: list[ShadowMetric] = field(default_factory=list)
    """Measures computed differently from their definitions, approved terms first, then by people."""
    adoption: list[Adoption] = field(default_factory=list)
    """Terms read through their own columns."""
    rules: dict[str, int] = field(default_factory=dict)
    """Queries with a finding of each rule."""
    notes: list[str] = field(default_factory=list)
    """What the warehouse withheld, and what would show it."""

    @property
    def approved_shadow(self) -> list[ShadowMetric]:
        """The shadow metrics of approved terms: queries that compute an approved measure
        differently from its definition."""
        return [s for s in self.shadow if s.status == 'approved']

    def summary(self) -> str:
        approved = len(self.approved_shadow)
        return (
            f'{_plural(self.queries, "query")} checked ({_plural(self.runs, "run")}); '
            f'{_plural(len(self.shadow), "shadow metric")}, {approved} of an approved measure.'
        )

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import history_check_lines, show

        return show(history_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            'summary': self.summary(),
            'queries': self.queries,
            'runs': self.runs,
            'unparsed': self.unparsed,
            'redacted': self.redacted,
            'shadow': [s.to_dict() for s in self.shadow],
            'adoption': [a.to_dict() for a in self.adoption],
            'rules': self.rules,
            'notes': self.notes,
        }


def shadow_text(item: ShadowMetric) -> str:
    """How a shadow metric computes its measure: without a filter, with one, or as a formula."""
    if item.rule == 'missing-filter' or item.detail.startswith('with '):
        return item.detail
    return f'as {item.detail}'


def pseudonym(user: str, key: bytes) -> str:
    """A stand-in for a user name, the same for one ``key`` and unrelated across keys, so a report
    can tell people apart without naming them and two reports cannot be matched."""
    return 'user-' + hmac.new(key, user.encode(), hashlib.sha256).hexdigest()[:16]


def _without_literals(text: str, dialect: Dialect) -> str:
    """A query with every literal replaced by ``?``, or nothing if it cannot be read."""
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except PARSE_ERRORS:
        return ''
    blank = tree.transform(lambda n: exp.Placeholder() if isinstance(n, exp.Literal) else n)
    return blank.sql(dialect=dialect)


def check_history(
    graph: Graph,
    history: QueryHistory,
    *,
    pseudonymise: bool = False,
    key: bytes | None = None,
    limit: int | None = None,
) -> HistoryCheck:
    """Run the checks over query history and gather the queries that compute a measure differently
    from its definition (shadow metrics), with counts of queries, runs and people.

    Args:
        graph: The project's graph.
        history: Distinct query texts with their user and how often they ran
            (:func:`konstruct.inputs.usage.project_history`).
        pseudonymise: Count people without naming them, and leave the literals out of examples.
        key: The key pseudonyms are made with; by default a new random key for each check, so
            pseudonyms cannot be matched across reports.
        limit: The most distinct query texts to check, those run most often first.
    """
    checker = Checker(graph)
    secret = key if key is not None else secrets.token_bytes(32)
    result = HistoryCheck(redacted=history.redacted, notes=list(history.notes))
    grouped: dict[str, list[HistoryQuery]] = defaultdict(list)
    for item in history.queries:
        if item.text and item.text.strip():
            grouped[item.text].append(item)
    texts = sorted(grouped, key=lambda t: (-sum(q.runs for q in grouped[t]), t))
    if limit is not None:
        texts = texts[:limit]
    shadow: dict[tuple[str, str, str], ShadowMetric] = {}
    users: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    best: dict[tuple[str, str, str], int] = {}
    adoption: dict[str, Adoption] = {}
    adopters: dict[str, set[str]] = defaultdict(set)
    rules: dict[str, int] = defaultdict(int)
    for text in texts:
        runs = sum(q.runs for q in grouped[text])
        people = {pseudonym(q.user, secret) if pseudonymise else q.user for q in grouped[text] if q.user}
        result.queries += 1
        result.runs += runs
        try:
            checked = checker.check(text)
        except _QUALIFY_ERRORS:  # SQL that sqlglot reads but cannot analyse counts as unparsed
            result.unparsed += 1
            continue
        if any(f.rule == 'unparsed' for f in checked.findings):
            result.unparsed += 1
            continue
        for rule in {f.rule for f in checked.findings}:
            rules[rule] += 1
        broken: set[str] = set()
        for finding in checked.findings:
            if finding.rule not in SHADOW_RULES or finding.detail is None:
                continue
            idea = finding.term or finding.column or ''
            entry_key = (idea, finding.rule, finding.detail)
            if finding.term:
                broken.add(finding.term)
            entry = shadow.get(entry_key)
            if entry is None:
                held = next((t for t in checker.terms if t.name == finding.term), None)
                entry = shadow[entry_key] = ShadowMetric(
                    idea, held.status if held else None, finding.rule, finding.detail, finding.fix
                )
            entry.queries += 1
            entry.runs += runs
            users[entry_key] |= people
            if runs > best.get(entry_key, -1):
                best[entry_key] = runs
                entry.example = _one_line(_without_literals(text, checker.sqlglot) if pseudonymise else text)
        for name in checked.terms:
            if name in broken:
                continue
            read = adoption.setdefault(name, Adoption(name))
            read.queries += 1
            read.runs += runs
            adopters[name] |= people
    order = {'approved': 0, 'agreed': 1, 'draft': 2}
    for entry_key, entry in shadow.items():
        entry.users = len(users[entry_key])
        entry.people = [] if pseudonymise else sorted(users[entry_key])
    result.shadow = sorted(
        shadow.values(),
        key=lambda s: (order.get(s.status or '', 3), -s.users, -s.queries, s.term, s.detail),
    )
    for name, read in adoption.items():
        read.users = len(adopters[name])
    result.adoption = sorted(adoption.values(), key=lambda a: (-a.queries, a.term))
    result.rules = dict(sorted(rules.items()))
    return result


def _one_line(text: str, width: int = 160) -> str:
    flat = ' '.join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + '...'
