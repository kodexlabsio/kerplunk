"""Semantic mutants of a SQL query, for mutation testing a model's data tests.

A mutant is the query with one small change of the kind a real bug makes: an aggregate swapped, a
filter or join condition dropped or negated, an inner join made a left join (or back), a comparison
boundary moved or a comparison flipped, a date literal shifted by a day, a COALESCE removed or a
CASE branch changed. :func:`mutants` lists every such
change in a query, deterministically, and returns a seeded sample of them as SQL.

This module only rewrites SQL with sqlglot; running mutants is the job of
:mod:`konstruct.analysis.mutation`, inside ``konstruct verify``.
"""

from __future__ import annotations

import datetime
import random
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import sqlglot
from sqlglot import exp

from konstruct.core.sql import PARSE_ERRORS

OPERATORS = (
    'aggregate',
    'filter',
    'join_condition',
    'join_type',
    'boundary',
    'comparison',
    'date',
    'coalesce',
    'case',
)
"""The kinds of mutation, in the order a sample draws from them."""

_AGGREGATES: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.Sum: exp.Max,
    exp.Max: exp.Min,
    exp.Min: exp.Max,
    exp.Avg: exp.Max,
}
_BOUNDARIES: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.GTE,
    exp.GTE: exp.GT,
    exp.LT: exp.LTE,
    exp.LTE: exp.LT,
}
# A flipped comparison selects the rows the original leaves out (or, for an ordering, the other side).
_FLIPS: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
}
_DATE = re.compile(r'^(\d{4}-\d{2}-\d{2})([ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$')
_SNIPPET = 100


@dataclass(frozen=True)
class Mutant:
    """One mutation of a query: what changed, where, and the whole mutated SQL."""

    id: str
    """Stable within one query: the operator and the mutation's position in the query."""
    operator: str
    """One of :data:`OPERATORS`."""
    description: str
    """What changed, in words."""
    before: str
    """The SQL fragment that changed."""
    after: str
    """What it became (empty when it was removed)."""
    sql: str
    """The whole mutated query, in the dialect it was read in."""
    scope: str | None = None
    """The CTE the change is in, or None for the outer query."""
    column: str | None = None
    """The outer query's output column the change sits in, when it is inside one."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop('sql')
        return data


@dataclass
class MutantSet:
    """The mutants drawn from a query, with how many mutations it admits."""

    mutants: list[Mutant] = field(default_factory=list)
    """The sampled mutants, in a deterministic order."""
    sites: int = 0
    """Every mutation the query admits, before sampling."""
    duplicates: int = 0
    """Mutations dropped because their SQL was the original's or another mutant's."""


@dataclass(frozen=True)
class _Site:
    index: int
    operator: str
    description: str
    build: Callable[[exp.Expr], exp.Expr | None]
    """The replacement for the node at ``index`` (a fresh tree), or None to remove the node."""
    variant: int = 0
    """Which of the node's mutations this is."""


def _text(node: exp.Expr | None, dialect: str) -> str:
    if node is None:
        return ''
    text = ' '.join(node.sql(dialect=dialect).split())
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 3] + '...'


def _conjuncts(condition: exp.Expr) -> list[exp.Expr]:
    return list(condition.flatten()) if isinstance(condition, exp.And) else [condition]


def _and(parts: list[exp.Expr]) -> exp.Expr:
    return exp.and_(*(p.copy() for p in parts), copy=False)


def _negate(condition: exp.Expr) -> exp.Expr:
    return exp.Not(this=exp.Paren(this=condition.copy()))


def _clause_sites(node: exp.Where | exp.Having, index: int, dialect: str) -> list[_Site]:
    keyword = 'WHERE' if isinstance(node, exp.Where) else 'HAVING'
    condition = node.this
    shown = _text(condition, dialect)
    sites = [
        _Site(index, 'filter', f'drop the {keyword} condition `{shown}`', lambda n: None),
        _Site(
            index,
            'filter',
            f'negate the {keyword} condition `{shown}`',
            lambda n: type(n)(this=_negate(n.this)),
        ),
    ]
    parts = _conjuncts(condition)
    if len(parts) > 1:
        for position, part in enumerate(parts):

            def without(n: exp.Expr, position: int = position) -> exp.Expr:
                kept = [p for i, p in enumerate(_conjuncts(n.this)) if i != position]
                return type(n)(this=_and(kept))

            sites.append(_Site(index, 'filter', f'drop `{_text(part, dialect)}` from the {keyword}', without))
    return sites


def _join_sites(node: exp.Join, index: int, dialect: str) -> list[_Site]:
    sites: list[_Site] = []
    side = (node.args.get('side') or '').upper()
    kind = (node.args.get('kind') or '').upper()
    on = node.args.get('on')
    if kind in ('', 'INNER', 'OUTER') and (on is not None or node.args.get('using')):
        target = {'LEFT': '', 'RIGHT': '', '': 'LEFT'}.get(side)
        if target is not None:
            before = f'{side or "INNER"} JOIN'
            after = f'{target or "INNER"} JOIN'

            def rejoin(n: exp.Expr, target: str = target) -> exp.Expr:
                joined = n.copy()
                joined.set('side', target or None)
                joined.set('kind', None)
                return joined

            name = _text(node.this, dialect)
            sites.append(_Site(index, 'join_type', f'{before} {name} to {after}', rejoin))
    if on is None:
        return sites
    shown = _text(on, dialect)

    def replace_on(n: exp.Expr, condition: Callable[[exp.Expr], exp.Expr]) -> exp.Expr:
        joined = n.copy()
        joined.set('on', condition(n.args['on']))
        return joined

    sites.append(
        _Site(
            index,
            'join_condition',
            f'negate the join condition `{shown}`',
            lambda n: replace_on(n, _negate),
        )
    )
    parts = _conjuncts(on)
    if len(parts) == 1:
        sites.append(
            _Site(
                index,
                'join_condition',
                f'drop the join condition `{shown}`',
                lambda n: replace_on(n, lambda c: exp.true()),
            )
        )
    for position, part in enumerate(parts if len(parts) > 1 else []):

        def without(n: exp.Expr, position: int = position) -> exp.Expr:
            kept = [p for i, p in enumerate(_conjuncts(n.args['on'])) if i != position]
            return replace_on(n, lambda c: _and(kept))

        sites.append(
            _Site(index, 'join_condition', f'drop `{_text(part, dialect)}` from the join condition', without)
        )
    return sites


def _aggregate_sites(node: exp.Expr, index: int, dialect: str) -> list[_Site]:
    swap = _AGGREGATES.get(type(node))
    if swap is not None:
        target = swap

        def swapped(n: exp.Expr, target: type[exp.Expr] = target) -> exp.Expr:
            return target(this=n.this.copy())

        built = swapped(node)
        return [_Site(index, 'aggregate', f'{_text(node, dialect)} to {_text(built, dialect)}', swapped)]
    if not isinstance(node, exp.Count) or node.this is None or isinstance(node.this, exp.Star):
        return []
    if isinstance(node.this, exp.Distinct):
        values = node.this.expressions
        if len(values) != 1:
            return []

        def plain(n: exp.Expr) -> exp.Expr:
            return exp.Count(this=n.this.expressions[0].copy())

        return [_Site(index, 'aggregate', f'{_text(node, dialect)} to {_text(plain(node), dialect)}', plain)]

    def distinct(n: exp.Expr) -> exp.Expr:
        return exp.Count(this=exp.Distinct(expressions=[n.this.copy()]))

    return [
        _Site(index, 'aggregate', f'{_text(node, dialect)} to {_text(distinct(node), dialect)}', distinct)
    ]


def _comparison_sites(node: exp.Expr, index: int, dialect: str) -> list[_Site]:
    sites: list[_Site] = []
    for operator, table in (('boundary', _BOUNDARIES), ('comparison', _FLIPS)):
        target = table.get(type(node))
        if target is None:
            continue

        def moved(n: exp.Expr, target: type[exp.Expr] = target) -> exp.Expr:
            return target(this=n.this.copy(), expression=n.expression.copy())

        sites.append(
            _Site(index, operator, f'{_text(node, dialect)} to {_text(moved(node), dialect)}', moved)
        )
    return sites


def _date_sites(node: exp.Literal, index: int, dialect: str) -> list[_Site]:
    """A date or timestamp literal a day later: a filter or bucket boundary one day off."""
    match = _DATE.match(node.this)
    if match is None:
        return []
    try:
        # The last date (9999-12-31, the usual open-ended sentinel) has no next day.
        shifted = (datetime.date.fromisoformat(match[1]) + datetime.timedelta(days=1)).isoformat()
    except (ValueError, OverflowError):
        return []
    shifted += match[2] or ''

    def later(n: exp.Expr) -> exp.Expr:
        return exp.Literal.string(shifted)

    return [_Site(index, 'date', f"shift the date '{node.this}' to '{shifted}'", later)]


def _case_sites(node: exp.Case, index: int, dialect: str) -> list[_Site]:
    sites: list[_Site] = []
    branches = node.args.get('ifs') or []
    default = node.args.get('default')
    if len(branches) > 1 or default is not None:
        for position, branch in enumerate(branches):

            def without(n: exp.Expr, position: int = position) -> exp.Expr:
                changed = n.copy()
                kept = [b for i, b in enumerate(changed.args['ifs']) if i != position]
                if not kept:  # the only branch: every row takes the default
                    default: exp.Expr = changed.args['default']
                    return default
                changed.set('ifs', kept)
                return changed

            shown = f'WHEN {_text(branch.this, dialect)} THEN {_text(branch.args.get("true"), dialect)}'
            sites.append(_Site(index, 'case', f'drop the CASE branch `{shown}`', without))
    if default is not None and branches:

        def no_default(n: exp.Expr) -> exp.Expr:
            changed = n.copy()
            changed.set('default', None)
            return changed

        sites.append(
            _Site(index, 'case', f'drop the CASE default `ELSE {_text(default, dialect)}`', no_default)
        )
    return sites


def _sites(tree: exp.Expr, dialect: str, operators: tuple[str, ...]) -> tuple[list[exp.Expr], list[_Site]]:
    nodes = list(tree.walk())
    sites: list[_Site] = []
    for index, node in enumerate(nodes):
        found: list[_Site] = []
        if isinstance(node, (exp.Where, exp.Having)) and node.this is not None:
            found = _clause_sites(node, index, dialect)
        elif isinstance(node, exp.Join):
            found = _join_sites(node, index, dialect)
        elif isinstance(node, (exp.Sum, exp.Max, exp.Min, exp.Avg, exp.Count)):
            found = _aggregate_sites(node, index, dialect)
        elif type(node) in _FLIPS:
            found = _comparison_sites(node, index, dialect)
        elif isinstance(node, exp.Literal) and node.is_string:
            found = _date_sites(node, index, dialect)
        elif isinstance(node, exp.Coalesce) and node.this is not None:

            def first(n: exp.Expr) -> exp.Expr:
                kept: exp.Expr = n.this.copy()
                return kept

            found = [_Site(index, 'coalesce', f'remove the COALESCE from `{_text(node, dialect)}`', first)]
        elif isinstance(node, exp.Case):
            found = _case_sites(node, index, dialect)
        sites.extend(replace(s, variant=v) for v, s in enumerate(found) if s.operator in operators)
    return nodes, sites


def _scope(node: exp.Expr) -> str | None:
    cte = node.find_ancestor(exp.CTE)
    return cte.alias_or_name if cte is not None else None


def _column(node: exp.Expr, tree: exp.Expr) -> str | None:
    """The outer query's projection the node sits in, by its output name."""
    if not isinstance(tree, exp.Select):
        return None
    current: exp.Expr | None = node
    while current is not None and current.parent is not tree:
        current = current.parent
    if current is None or current.arg_key != 'expressions':
        return None
    return current.alias_or_name or None


def _order(sites: list[_Site], rng: random.Random) -> list[_Site]:
    """Every site in a seeded order that draws from each operator in turn, so a small sample
    still covers every kind of mutation the query admits."""
    groups = {op: [s for s in sites if s.operator == op] for op in OPERATORS}
    for group in groups.values():
        rng.shuffle(group)
    ordered: list[_Site] = []
    while any(groups.values()):
        for op in OPERATORS:
            if groups[op]:
                ordered.append(groups[op].pop(0))
    return ordered


def _swapped_sql(tree: exp.Expr, node: exp.Expr, built: exp.Expr | None, dialect: str) -> str:
    """The SQL of ``tree`` with ``node`` replaced by ``built`` (or removed when it is None). The
    change is made in place and undone, which is far cheaper than copying the tree per mutant."""
    if node is tree:
        return tree.sql(dialect=dialect) if built is None else built.sql(dialect=dialect)
    parent, key, index = node.parent, node.arg_key, node.index
    assert parent is not None and key is not None
    if built is None:
        node.pop()
    else:
        node.replace(built)
    try:
        return tree.sql(dialect=dialect)
    finally:
        if index is None:
            parent.set(key, node)
        else:
            items = list(parent.args[key])
            if built is None:
                items.insert(index, node)
            else:
                items[index] = node
            parent.set(key, items)


def parse_query(sql: str, dialect: str) -> exp.Query:
    """The query ``sql`` holds, or a ValueError when it is not exactly one SELECT (or set
    operation). A second statement after a semicolon is refused, not carried into every mutant."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except PARSE_ERRORS as error:
        raise ValueError(f'the SQL does not parse: {str(error).strip().splitlines()[0][:160]}') from error
    if len(statements) != 1:
        raise ValueError(f'the SQL is {len(statements)} statements, not one query')
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise ValueError(f'the SQL is a {type(tree).__name__}, not a query')
    return tree


def mutants(
    sql: str,
    dialect: str = 'duckdb',
    *,
    operators: tuple[str, ...] = OPERATORS,
    limit: int | None = None,
    seed: int | str = 0,
) -> MutantSet:
    """The mutants of the query ``sql``: all of them, or a seeded sample of at most ``limit``.

    The sample is reproducible for a given ``seed`` and draws from each operator in turn. A
    mutation whose SQL is the original's, or another mutant's, is dropped as a duplicate.
    """
    unknown = sorted(set(operators) - set(OPERATORS))
    if unknown:
        raise ValueError(f'unknown mutation operators: {", ".join(unknown)}')
    tree = parse_query(sql, dialect)
    original = tree.sql(dialect=dialect)
    nodes, sites = _sites(tree, dialect, operators)
    result = MutantSet(sites=len(sites))
    seen = {original}
    for site in _order(sites, random.Random(f'{seed}')):
        if limit is not None and len(result.mutants) >= limit:
            break
        node = nodes[site.index]
        built = site.build(node)
        text = _swapped_sql(tree, node, built, dialect)
        if text in seen:
            result.duplicates += 1
            continue
        seen.add(text)
        result.mutants.append(
            Mutant(
                id=f'{site.operator}-{site.index}.{site.variant}',
                operator=site.operator,
                description=site.description,
                before=_text(node, dialect),
                after=_text(built, dialect),
                sql=text,
                scope=_scope(node),
                column=_column(node, tree),
            )
        )
    return result
