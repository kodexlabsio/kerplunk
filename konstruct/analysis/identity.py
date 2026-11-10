"""Whether two formulas compute the same thing, however differently they are written.

``SUM(price * quantity)`` and ``SUM(quantity * price)`` are the same measure; ``SUM(a * b)``
and ``SUM(a) * SUM(b)`` are not. This module translates formulas into a program for the native
core (``konstruct._core.FormulaSet``), which evaluates them at random points modulo three random
primes: formulas that are the same agree at every point, and formulas that differ agree at a
random one with negligible probability (the Schwartz-Zippel lemma), so formulas found the same
are the same but for a stated bound on the error. Formulas not found the same are not shown to
be the same, which is not a proof that they differ: a witness of values is one when it sets
source columns alone.

What "the same" means is set out in the Rust module ``identity::eval``. In short: the same
result, a number or NULL, for every assignment of numbers or NULL to the source columns and
over every table of rows (an empty one included), wherever no divisor is zero. Arithmetic is
exact, so floating-point rounding is not modelled; it is NULL when an operand is. Every other
function is an arbitrary fixed function of its arguments (so ``ROUND(a + b)`` equals
``ROUND(b + a)`` but not ``a + b``), CASE and COALESCE work as SQL says, and SUM is linear over
the rows where its argument is not NULL, under the conditions it runs under. A column with a
``not_null`` test is never NULL, except where it is read through an outer join, which may find
no matching row. A volatile function (``RANDOM()``, ``UUID()``) is a different function at each
place it is called. Each verdict also says whether two formulas are the same when
no source value is NULL and every group has rows: ``SUM(a) - SUM(b)`` and ``SUM(a - b)`` are
only then.

Each formula also gets a 128-bit fingerprint, equal for formulas that are the same, so any
number of them can be grouped with a dictionary. Fingerprints use one fixed key, so that they
agree between builds; a comparison draws a fresh random key, because a formula can be built,
with knowledge of a fixed key, to collide with another. Two formulas that differ come with a
:class:`Witness`: values at which they give different results, or the function they apply
differently.

In a graph, a column's formula is its own expression with each input replaced by the input's
formula, as :mod:`konstruct.analysis.explain` composes it, but written once per column and
referred to: a column's whole lineage costs one translation per column.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from konstruct import _core
from konstruct.analysis.explain import Explainer, _composed_from, short_sql
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS

SCHEME = 3
"""The version of the fingerprints: two fingerprints compare only under the same scheme."""

_COMPARISONS: dict[type[exp.Expr], tuple[str, bool]] = {
    # Each comparison as one of GT and GTE, and whether its operands swap: a < b is b > a.
    exp.GT: ('GT', False),
    exp.GTE: ('GTE', False),
    exp.LT: ('GT', True),
    exp.LTE: ('GTE', True),
}
# NOT of a comparison is the opposite comparison, in three-valued logic too.
_NEGATED: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.GT: exp.LTE,
    exp.GTE: exp.LT,
    exp.LT: exp.GTE,
    exp.LTE: exp.GT,
}
_SYMMETRIC: dict[type[exp.Expr], str] = {
    exp.EQ: 'EQ',
    exp.NEQ: 'NEQ',
    exp.NullSafeEQ: 'IS NOT DISTINCT FROM',
    exp.NullSafeNEQ: 'IS DISTINCT FROM',
    exp.And: 'AND',
    exp.Or: 'OR',
    exp.Greatest: 'GREATEST',
    exp.Least: 'LEAST',
}
# Functions that give another value at each call: each place one is called is a function of its
# own, so that RANDOM() + RANDOM() is not 2 * RANDOM().
_VOLATILE_TYPES: tuple[type[exp.Expr], ...] = (exp.Rand, exp.Randn, exp.Uuid)
_VOLATILE_NAMES = frozenset({'random', 'uuid', 'newid', 'gen_random_uuid', 'uuid_string'})
# What NOT NOT cancels around: an expression that is true, false or NULL. Elsewhere NOT is a
# function like any other (in MySQL, NOT NOT 5 is 1).
_PREDICATES: tuple[type[exp.Expr], ...] = (exp.Predicate, exp.And, exp.Or, exp.Not, exp.Boolean)
_OUTER_SIDES = frozenset({'LEFT', 'RIGHT', 'FULL', 'OUTER'})
_LARGEST_EXPONENT = 64
_NULL = '(lit "null" "")'
_HEAD = re.compile(r'\(\w+')
_ONE = '(num "1" "1")'


def _quote(text: str) -> str:
    # The slot marker never appears raw in a definition's text: in a literal it is escaped.
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace(_SLOT, '\\1') + '"'


@functools.lru_cache(maxsize=4096)
def _number(text: str) -> str | None:
    """A numeric literal as an exact rational, or None for one that is not a plain number."""
    lowered = text.lower().replace('_', '')
    mantissa, _, exponent = lowered.partition('e')
    if len(mantissa) > 60 or (
        exponent and not (exponent.lstrip('+-').isdigit() and abs(int(exponent)) <= 60)
    ):
        return None
    try:
        value = Fraction(lowered)
    except (ValueError, ZeroDivisionError):
        return None
    return f'(num "{value.numerator}" "{value.denominator}")'


def _display(name: str) -> str:
    """A function's name as written in SQL, without the signature that keeps variants apart."""
    return name.split('/', 1)[0]


_Resolved = tuple[Node, str, Node | None] | None
_SLOT = '\x01'
# A qualified column in SQL with no quoted text: replaced by a numbered slot, so that every
# expression of one shape (``COALESCE(a.x, b.x)``) is parsed and written once.
_QUALIFIED = re.compile(r'(?<![\w.$])[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){1,3}(?![\w.$(])')
_QUOTES = frozenset('\'"`[\x01')
_PLACEHOLDER = re.compile(r'__k(\d+)')


@dataclass
class _Shape:
    """An expression written as a definition with its columns left as numbered slots."""

    pieces: list[str]
    """The definition's text, split at the slots: every other piece is a slot's number."""
    labels: list[exp.Expr]
    """The nodes a witness may show, in which a slot's column is ``__k<n>``."""


@dataclass
class _Plan:
    """A column's expression as a shape, and the columns that fill its slots."""

    shape: _Shape
    columns: list[exp.Column]
    resolved: list[_Resolved]


class _Emitter:
    """Writes one expression as a definition of the program (see the Rust module
    ``identity::program`` for the notation), keeping the nodes worth showing in a witness."""

    def __init__(self, column: Callable[[exp.Column], str], volatile: Callable[[], str]) -> None:
        self.column = column
        # What tells apart each place a volatile function is called, written into its name.
        self.volatile = volatile
        self.out: list[str] = []
        self.labels: list[exp.Expr] = []

    def text(self, node: exp.Expr) -> str:
        self.emit(node)
        return ''.join(self.out)

    def label(self, node: exp.Expr) -> str:
        self.labels.append(node)
        return f'#{len(self.labels) - 1}'

    def emit(self, node: exp.Expr, window: bool = False) -> None:
        out = self.out
        while isinstance(node, (exp.Paren, exp.Alias)):
            node = node.this
        kind = type(node)
        if isinstance(node, exp.Column):
            out.append(self.column(node))
        elif kind is exp.Literal:
            number = None if node.is_string else _number(str(node.this))
            if number is not None:
                out.append(number)
            else:
                out.append(
                    f'(lit{self.label(node)} "{"str" if node.is_string else "number"}" {_quote(str(node.this))})'
                )
        elif kind is exp.Null:
            out.append(_NULL)
        elif kind is exp.Boolean:
            out.append(f'(lit "bool" "{"true" if node.this else "false"}")')
        elif kind in (exp.Add, exp.Sub):
            self.sums(node, window)
        elif kind is exp.Mul:
            out.append('(mul')
            for factor in self.chain(node, exp.Mul):
                out.append(' ')
                self.emit(factor, window)
            out.append(')')
        elif kind is exp.Div and not node.args.get('typed'):
            # Integer division in a dialect that truncates (``typed``) is a function of its own.
            self.call('div', [node.this, node.expression], window)
        elif kind is exp.Neg:
            self.call('neg', [node.this], window)
        elif kind is exp.Pow and self.small_exponent(node.expression) is not None:
            out.append('(pow ')
            self.emit(node.this, window)
            out.append(f' {self.small_exponent(node.expression)})')
        elif isinstance(node, exp.Case):
            self.case(node, window)
        elif kind is exp.If:
            self.choice([(node.this, node.args['true'])], node.args.get('false'), window)
        elif kind in _COMPARISONS:
            name, swap = _COMPARISONS[kind]
            operands = [node.expression, node.this] if swap else [node.this, node.expression]
            self.function('fn', name, operands, node, window)
        elif kind in _SYMMETRIC:
            operands = self.chain(node, kind) if kind in (exp.And, exp.Or) else self.operands(node)
            self.function('sym', _SYMMETRIC[kind], operands, node, window)
        elif isinstance(node, exp.Not):
            self.negation(node, window)
        elif kind is exp.Between:
            low, high = node.args['low'], node.args['high']
            within = self.within(node.this, low, high)
            if node.args.get('symmetric'):
                # BETWEEN SYMMETRIC holds with the bounds either way round.
                self.function('sym', 'OR', [within, self.within(node.this, high, low)], node, window)
            else:
                self.emit(within, window)
        elif kind is exp.Coalesce:
            self.call('coalesce', [node.this, *node.expressions], window)
        elif (
            kind is exp.In and node.expressions and not node.args.get('query') and not node.args.get('unnest')
        ):
            choices = [exp.EQ(this=node.this, expression=e) for e in node.expressions]
            self.function('sym', 'OR', choices, node, window)
        elif isinstance(node, exp.AggFunc) and not window:
            self.aggregate(node)
        elif kind is exp.Filter and isinstance(node.this, exp.AggFunc) and not window:
            self.aggregate(self.filtered(node.this, node.expression.this))
        elif kind is exp.Window:
            # A window reads the rows of its query: keyed by their conditions, as aggregates.
            self.generic(node, 'OVER', window=True, op='win')
        elif isinstance(node, exp.Query):
            # A scalar subquery is NULL when it finds no row: a function of its text that may be.
            out.append(f'(fn{self.label(node)} "QUERY" (lit "query" {_quote(node.sql())}))')
        elif kind in (exp.DataType, exp.Var, exp.Identifier, exp.Star):
            out.append(f'(lit "{node.key}" {_quote(node.sql())})')
        else:
            self.generic(node, node.name.upper() if kind is exp.Anonymous else node.key.upper(), window)

    @staticmethod
    def within(value: exp.Expr, low: exp.Expr, high: exp.Expr) -> exp.Expr:
        """``value BETWEEN low AND high``, as the two comparisons it stands for."""
        return exp.And(
            this=exp.GTE(this=value, expression=low), expression=exp.LTE(this=value, expression=high)
        )

    @staticmethod
    def chain(node: exp.Expr, kind: type[exp.Expr]) -> list[exp.Expr]:
        """The operands of a chain of one associative operator, without recursion: a wide sum
        or a long OR is thousands of levels deep."""
        found: list[exp.Expr] = []
        stack = [node]
        while stack:
            item = stack.pop()
            while isinstance(item, exp.Paren):
                item = item.this
            if type(item) is kind:
                stack += [item.expression, item.this]
            else:
                found.append(item)
        return found

    def sums(self, node: exp.Expr, window: bool) -> None:
        """A chain of additions and subtractions, as one sum of signed terms."""
        terms: list[tuple[exp.Expr, bool]] = []
        stack = [(node, False)]
        while stack:
            item, negative = stack.pop()
            while isinstance(item, exp.Paren):
                item = item.this
            if isinstance(item, exp.Add):
                stack += [(item.expression, negative), (item.this, negative)]
            elif isinstance(item, exp.Sub):
                stack += [(item.expression, not negative), (item.this, negative)]
            else:
                terms.append((item, negative))
        self.out.append('(add')
        for term, negative in terms:
            self.out.append(' (neg ' if negative else ' ')
            self.emit(term, window)
            if negative:
                self.out.append(')')
        self.out.append(')')

    @staticmethod
    def small_exponent(node: exp.Expr) -> int | None:
        if isinstance(node, exp.Literal) and not node.is_string and str(node.this).isdigit():
            value = int(node.this)
            return value if value <= _LARGEST_EXPONENT else None
        return None

    @staticmethod
    def operands(node: exp.Expr) -> list[exp.Expr]:
        found = [node.this] if isinstance(node.this, exp.Expr) else []
        if isinstance(node.args.get('expression'), exp.Expr):
            found.append(node.args['expression'])
        found += [e for e in node.args.get('expressions') or [] if isinstance(e, exp.Expr)]
        return found

    def call(self, op: str, operands: Sequence[exp.Expr], window: bool) -> None:
        self.out.append(f'({op}')
        for operand in operands:
            self.out.append(' ')
            self.emit(operand, window)
        self.out.append(')')

    def function(
        self,
        op: str,
        name: str,
        operands: Sequence[exp.Expr],
        node: exp.Expr,
        window: bool,
        occurrence: str = '',
    ) -> None:
        self.out.append(f'({op}{self.label(node)} {_quote(name)[:-1]}{occurrence}"')
        for operand in operands:
            self.out.append(' ')
            self.emit(operand, window)
        self.out.append(')')

    def generic(self, node: exp.Expr, name: str, window: bool, op: str = 'fn') -> None:
        """Any other function, uninterpreted: its arguments in order, and the names of the
        arguments given (and any flags) in its name, so that variants stay apart."""
        signature: list[str] = []
        operands: list[exp.Expr] = []
        for key in node.arg_types:
            value = node.args.get(key)
            if key.startswith('_') or value is None or value is False or value == []:
                continue
            if isinstance(value, exp.Expr):
                signature.append(key)
                operands.append(value)
            elif isinstance(value, list):
                items = [v for v in value if isinstance(v, exp.Expr)]
                signature.append(f'{key}*{len(items)}')
                operands += items
            else:
                signature.append(f'{key}={value}')
        volatile = isinstance(node, _VOLATILE_TYPES) or (
            isinstance(node, exp.Anonymous) and node.name.lower() in _VOLATILE_NAMES
        )
        occurrence = f'@{self.volatile()}' if volatile else ''
        self.function(op, f'{name}/{",".join(signature)}', operands, node, window, occurrence)

    def negation(self, node: exp.Not, window: bool) -> None:
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        twice = inner.this if isinstance(inner, exp.Not) else None
        while isinstance(twice, exp.Paren):
            twice = twice.this
        if isinstance(twice, _PREDICATES):
            self.emit(twice, window)
        elif type(inner) in _NEGATED:
            self.emit(_NEGATED[type(inner)](this=inner.this, expression=inner.expression), window)
        else:
            self.function('fn', 'NOT', [inner], node, window)

    def case(self, node: exp.Case, window: bool) -> None:
        operand = node.this
        branches = []
        for branch in node.args.get('ifs') or []:
            condition = branch.this
            if operand is not None:
                condition = exp.EQ(this=operand, expression=condition)
            branches.append((condition, branch.args['true']))
        self.choice(branches, node.args.get('default'), window)

    def choice(
        self, branches: list[tuple[exp.Expr, exp.Expr]], default: exp.Expr | None, window: bool
    ) -> None:
        if not branches:
            self.otherwise(default, window)
            return
        self.out.append('(case')
        for condition, value in branches:
            self.out.append(' ')
            self.labelled(condition, window)
            self.out.append(' ')
            self.emit(value, window)
        self.out.append(' ')
        self.otherwise(default, window)
        self.out.append(')')

    def otherwise(self, default: exp.Expr | None, window: bool) -> None:
        """A CASE's ELSE: NULL when there is none."""
        if default is None:
            self.out.append(_NULL)
        else:
            self.emit(default, window)

    def labelled(self, node: exp.Expr, window: bool) -> None:
        """``node``, with its label on the node written for it, so that a witness can show it."""
        start = len(self.out)
        self.emit(node, window)
        head = self.out[start]
        op = _HEAD.match(head)
        if op is not None and not head[op.end() :].startswith('#'):
            self.out[start] = head[: op.end()] + self.label(node) + head[op.end() :]

    def aggregate(self, node: exp.AggFunc) -> None:
        argument = node.this
        distinct = isinstance(argument, exp.Distinct)
        if type(node) in (exp.Sum, exp.Avg) and not distinct and isinstance(argument, exp.Expr):
            self.out.append('(sum ' if type(node) is exp.Sum else '(avg ')
            self.emit(argument)
            self.out.append(')')
        elif type(node) is exp.Count and not distinct and not node.expressions:
            if argument is None or isinstance(argument, (exp.Star, exp.Literal)):
                self.out.append('(count)')
            else:
                self.out.append(f'(countof{self.label(node)} ')
                self.emit(argument)
                self.out.append(')')
        else:
            name = f'{node.key.upper()} DISTINCT' if distinct else node.key.upper()
            counts = type(node) is exp.Count
            if counts:
                # A count is never NULL, of distinct values too.
                self.out.append('(present ')
            self.generic(node, name, window=False, op='agg')
            if counts:
                self.out.append(')')

    @staticmethod
    def filtered(node: exp.AggFunc, condition: exp.Expr) -> exp.AggFunc:
        """``AGG(x) FILTER (WHERE c)`` as ``AGG(CASE WHEN c THEN x END)``, which is what it
        means: the aggregate skips the rows the condition leaves out."""
        copy = node.copy()
        argument = copy.this
        if argument is None or isinstance(argument, exp.Star):
            argument = exp.Literal.number(1)
        if isinstance(argument, exp.Distinct):
            argument.set(
                'expressions',
                [exp.Case(ifs=[exp.If(this=condition.copy(), true=e)]) for e in argument.expressions],
            )
        else:
            copy.set('this', exp.Case(ifs=[exp.If(this=condition.copy(), true=argument)]))
        return copy


@dataclass
class Witness:
    """Why two formulas differ."""

    kind: str
    """``values`` (inputs at which they give different results), ``functions`` (they apply an
    uninterpreted function differently) or ``conditions`` (the same formula, aggregated under
    different conditions)."""
    inputs: dict[str, str] = field(default_factory=dict)
    """The value of each input outside aggregates, ``NULL`` for NULL: columns, and the
    expressions the formulas share (a CASE condition as true or false)."""
    rows: list[dict[str, str]] | None = None
    """When the formulas aggregate, the rows they read (perhaps none), each with its inputs."""
    assumed: list[str] = field(default_factory=list)
    """The shared expressions among the inputs. Their values are chosen as freely as a column's,
    though the expression may never take them (``x > 0`` true while ``x`` is -1), so a witness
    of values that sets any shows a difference only if they can take those values; one that
    sets source columns alone is a proof."""
    a: str | None = None
    """The first formula's result at the inputs, ``NULL`` for NULL."""
    b: str | None = None
    """The second formula's result."""
    functions: list[str] = field(default_factory=list)
    """The functions the difference comes from."""
    only_a: list[str] = field(default_factory=list)
    """The innermost expressions only the first formula has."""
    only_b: list[str] = field(default_factory=list)
    """The innermost expressions only the second formula has."""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}
        if self.rows is not None:
            data['rows'] = self.rows
        return data

    def __str__(self) -> str:
        if self.kind == 'conditions':
            return 'the same formula, aggregated over rows chosen by different conditions'
        if self.kind == 'functions':
            parts = []
            if self.only_a:
                parts.append(f'{", ".join(self.only_a)} only in the first')
            if self.only_b:
                parts.append(f'{", ".join(self.only_b)} only in the second')
            return f'{", ".join(self.functions)} applied differently' + (
                f': {"; ".join(parts)}' if parts else ''
            )

        def assignment(values: dict[str, str]) -> str:
            return ', '.join(f'{k} = {v}' for k, v in values.items())

        where = []
        if self.inputs:
            where.append(f'where {assignment(self.inputs)}')
        if self.rows == []:
            where.append('over no rows')
        elif self.rows:
            count = len(self.rows)
            shown = '; '.join(f'({assignment(row)})' if row else '()' for row in self.rows)
            where.append(f'over {count} row{"s" if count != 1 else ""} {shown}')
        shown = f'{" ".join(where) or "always"}: {self.a} against {self.b}'
        if self.assumed:
            return f'they differ if {", ".join(self.assumed)} take these values: {shown}'
        return shown


@dataclass
class Verdict:
    """Whether two formulas compute the same thing."""

    equal: bool
    """Whether they are shown to give the same result, NULL included, for every input and
    table. False means not shown to be the same, which is not a proof that they differ: a
    witness of values that sets source columns alone is one."""
    error_bound: float
    """A bound on the probability that ``equal`` is true by chance although the formulas are not
    the same: negligible (far below 1e-30) when it is true, 0 when it is false, and 1 when a
    divisor was zero at a sampled point, which leaves no guarantee either way."""
    equal_without_nulls: bool
    """Whether they are shown to be the same when no source value is NULL and every group has
    rows: true whenever ``equal`` is."""
    witness: Witness | None = None
    """Why they are not the same, when a reason was found."""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            'equal': self.equal,
            'error_bound': self.error_bound,
            'equal_without_nulls': self.equal_without_nulls,
        }
        if self.witness is not None:
            data['witness'] = self.witness.to_dict()
        return data

    def __str__(self) -> str:
        if self.equal:
            return f'The same (error at most {self.error_bound:.1e}).'
        # Only values of source columns, worked out exactly, show that the two differ.
        head = (
            'Different'
            if self.witness is not None and self.witness.kind == 'values' and not self.witness.assumed
            else 'Not shown to be the same'
        )
        if self.equal_without_nulls:
            head += ', but the same unless a source value is NULL or there are no rows'
        if self.witness is None:
            return f'{head}.'
        return f'{head}: {self.witness}.'


class Formulas:
    """Formulas to fingerprint and compare, as definitions of one program in the native core.

    Add expressions with :meth:`add`; with a ``graph``, add a column's composed formula with
    :meth:`column`, which writes each column on its lineage once. With ``conditions``, the
    aggregates of a graph's columns are keyed by the joins, filters and grouping on their route
    back to source, so the same SUM under another filter is another formula. A graph's columns
    with a ``not_null`` test are never NULL, except where a relation with an outer join reads
    them; without ``outer``, also there.

    The formulas are evaluated under a fresh random key, so that no formula can be built to
    collide with another; their fingerprints are then comparable only with each other. With
    ``fixed_key`` they use the key every build uses, so that fingerprints agree between builds;
    a fingerprint match is then not a guarantee against a formula built with knowledge of that
    key, and whatever a stored fingerprint gates should also keep the formula and compare it
    again under a random key."""

    def __init__(
        self,
        graph: Graph | None = None,
        *,
        dialect: str | None = None,
        conditions: bool = True,
        fixed_key: bool = False,
        outer: bool = True,
    ) -> None:
        self.graph = graph
        if graph is not None and dialect is None:
            found = graph.meta.get('dialect')
            dialect = found if isinstance(found, str) else None
        self.dialect = Dialect.get_or_raise(dialect)
        self.conditions = conditions
        self.read_outer = outer
        self.core = _core.FormulaSet(None if fixed_key else os.urandom(32))
        self.explainer = Explainer(graph, qualified=True) if graph is not None else None
        # For each definition, the nodes a witness may show, and the columns of their slots.
        self.labels: list[tuple[list[exp.Expr], list[exp.Column]]] = []
        self.names: dict[str, str] = {}
        self.defined: dict[str, int] = {}
        self.contexts: dict[str, str] = {}
        self.shapes: dict[str, _Shape | None] = {}
        # How many places volatile functions are called in the expressions added, and what the
        # key of each column read through an outer join shows in a witness.
        self.calls = 0
        self.joins: dict[str, str] = {}
        self.outer: dict[str, bool] = {}

    def _add(self, text: str, context: str, labels: list[exp.Expr], columns: list[exp.Column]) -> int | None:
        try:
            definition = self.core.add(text, context)
        except ValueError:
            # Nested beyond what the core evaluates: no formula, as for SQL that does not parse.
            return None
        self.labels.append((labels, columns))
        return definition

    def add(
        self, expression: str | exp.Expr, *, context: Iterable[str] = (), not_null: Iterable[str] = ()
    ) -> int:
        """Adds an expression over columns named as written, whose aggregates run under the
        conditions ``context``, and returns its index; the columns named in ``not_null`` are
        never NULL. Raises ValueError for SQL that does not parse or nests too deeply."""
        if isinstance(expression, str):
            try:
                tree = sqlglot.parse_one(expression, read=self.dialect)
            except PARSE_ERRORS as error:
                raise ValueError(f'cannot read {expression[:200]!r}') from error
        else:
            tree = expression

        present = set(not_null)

        def column(node: exp.Column) -> str:
            name = '.'.join(part.name for part in node.parts)
            self.names[name] = name
            variable = f'(col {_quote(name)})'
            return f'(present {variable})' if name in present else variable

        def volatile() -> str:
            self.calls += 1
            return str(self.calls)

        emitter = _Emitter(column, volatile)
        definition = self._add(emitter.text(tree), '\n'.join(sorted(set(context))), emitter.labels, [])
        if definition is None:
            raise ValueError(f'{tree.sql()[:200]!r} is nested too deeply to compare')
        return definition

    def _variable(self, name: str, display: str, not_null: bool = False) -> str:
        self.names[name] = display
        variable = f'(col {_quote(name)})'
        return f'(present {variable})' if not_null else variable

    def _not_null(self, column: Node) -> bool:
        """Whether a ``not_null`` test covers the column, in its own relation."""
        assert self.graph is not None
        return any(
            self.graph.nodes[e.source].attrs.get('test') == 'not_null'
            for e in self.graph.in_edges(column.id, 'tests')
        )

    def column(self, column: Node) -> int:
        """The definition of ``column``'s composed formula, adding it and every column on its
        lineage that is not defined yet. A column nothing in the graph derives, or whose
        expression could not be read, is a source: a variable."""
        assert self.graph is not None, 'Formulas made without a graph'
        found = self.defined.get(column.id)
        if found is not None:
            return found
        plans: dict[str, _Plan | None] = {}
        # Depth first without recursion, each column defined after its inputs; a column met
        # again while its own inputs are being defined closes a cycle and stays a variable.
        stack: list[tuple[Node, bool]] = [(column, False)]
        path: set[str] = set()
        while stack:
            node, expanded = stack.pop()
            if node.id in self.defined:
                continue
            if expanded:
                path.discard(node.id)
                self.defined[node.id] = self._define(node, plans[node.id], path)
                continue
            if node.id in path:
                continue
            plan = plans[node.id] = self._plan(node)
            path.add(node.id)
            stack.append((node, True))
            if plan is not None:
                for resolved in plan.resolved:
                    target = resolved[2] if resolved is not None else None
                    if target is not None and target.id not in self.defined and target.id not in path:
                        stack.append((target, False))
        return self.defined[column.id]

    @staticmethod
    def _shape(tree: exp.Expr, slot: Callable[[exp.Column], int | None]) -> _Shape | None:
        """``tree`` written with each column as the slot ``slot`` numbers it, and each place a
        volatile function is called as a slot ``v<n>``, which the column it defines fills;
        None if a column has no slot."""
        missing = False
        calls = 0

        def column(item: exp.Column) -> str:
            nonlocal missing
            number = slot(item)
            missing = missing or number is None
            return f'{_SLOT}{number}{_SLOT}'

        def volatile() -> str:
            nonlocal calls
            calls += 1
            return f'{_SLOT}v{calls}{_SLOT}'

        emitter = _Emitter(column, volatile)
        text = emitter.text(tree)
        return None if missing else _Shape(text.split(_SLOT), emitter.labels)

    def _template(self, expression: str) -> tuple[_Shape, list[str]] | None:
        """The shape of an expression with no quoted text, from the expression with its
        qualified columns replaced by placeholders, and the columns' text in order."""
        if not _QUOTES.isdisjoint(expression):
            return None
        texts: list[str] = []

        def placeholder(match: re.Match[str]) -> str:
            texts.append(match.group(0))
            return f'__k{len(texts) - 1}'

        template = _QUALIFIED.sub(placeholder, expression)
        if template not in self.shapes:
            assert self.explainer is not None
            tree = self.explainer.namer.parse(template)

            def slot(item: exp.Column) -> int | None:
                found = _PLACEHOLDER.fullmatch(item.sql()) if not item.table else None
                return int(found.group(1)) if found else None

            self.shapes[template] = self._shape(tree, slot) if tree is not None else None
        shape = self.shapes[template]
        return (shape, texts) if shape is not None else None

    def _plan(self, node: Node) -> _Plan | None:
        """``node``'s expression as a shape and the columns that fill it, or None for a
        source."""
        assert self.explainer is not None
        expression = node.attrs.get('expression')
        if not isinstance(expression, str):
            return None
        namer = self.explainer.namer
        columns: list[exp.Column] = []
        template = self._template(expression)
        if template is not None:
            shape, texts = template
            for text in texts:
                column = namer.parse(text)
                assert isinstance(column, exp.Column)
                columns.append(column)
        else:
            tree = namer.parse(expression)
            if tree is None:
                return None

            def slot(item: exp.Column) -> int:
                columns.append(item)
                return len(columns) - 1

            found = self._shape(tree, slot)
            assert found is not None
            shape = found
        resolve = self.explainer.inputs(node, _composed_from(self.explainer.graph, node))
        return _Plan(shape, columns, [resolve(c) for c in columns])

    def _define(self, node: Node, plan: _Plan | None, path: set[str]) -> int:
        assert self.graph is not None
        graph = self.graph
        context = self._context(node) if self.conditions else ''
        if plan is not None:
            owner = graph.parent_of(node)
            outer = self.read_outer and owner is not None and self._outer(owner)
            filled = []
            for column, resolved in zip(plan.columns, plan.resolved, strict=True):
                if resolved is None:
                    text = column.sql()
                    filled.append(self._variable(text, text))
                    continue
                relation, name, target = resolved
                if target is None:
                    text = self._variable(f'{relation.id}#{name}', f'{graph.label(relation)}.{name}')
                elif target.id in path or target.id not in self.defined:
                    # A cycle in the lineage: the column stays a variable.
                    text = self._variable(target.id, graph.label(target), self._not_null(target))
                else:
                    text = f'(ref {self.defined[target.id]})'
                if outer and owner is not None and relation.id != owner.id:
                    # Read through an outer join, a column is NULL also where its side has no
                    # matching row, whatever its own tests say.
                    key = f'{owner.id}\x00{relation.id}#{name}'
                    self.joins[key] = f'{graph.label(relation)}.{name} matched in {graph.label(owner)}'
                    text = f'(outer {_quote(key)} {text})'
                filled.append(text)

            def fill(i: int, piece: str) -> str:
                if i % 2 == 0:
                    return piece
                if piece.startswith('v'):
                    # A volatile function: one of its own at each place in each column.
                    return _quote(f'{node.id}/{piece[1:]}')[1:-1]
                return filled[int(piece)]

            text = ''.join(fill(i, piece) for i, piece in enumerate(plan.shape.pieces))
            if self._not_null(node):
                text = f'(present {text})'
            definition = self._add(text, context, plan.shape.labels, plan.columns)
            if definition is not None:
                return definition
        self.labels.append(([], []))
        return self.core.add(self._variable(node.id, graph.label(node), self._not_null(node)), context)

    def _outer(self, relation: Node) -> bool:
        """Whether the relation has an outer join, so that a column it reads may be NULL where
        the input's own tests say it is not. Which side may have no match is not recorded, so
        every input counts."""
        if relation.id not in self.outer:
            self.outer[relation.id] = any(
                not _OUTER_SIDES.isdisjoint(str(c).split(':', 1)[0].split()[:-2])
                for c in relation.attrs.get('conditions', [])
            )
        return self.outer[relation.id]

    def _context(self, column: Node) -> str:
        """The conditions on the rows of ``column``'s relation and of every relation it reads,
        transitively, as one digest: what its aggregates run under."""
        assert self.graph is not None
        graph = self.graph
        owner = graph.parent_of(column)
        if owner is None:
            return ''
        if owner.id in self.contexts:
            return self.contexts[owner.id]
        stack: list[tuple[Node, bool]] = [(owner, False)]
        path: set[str] = set()
        while stack:
            relation, expanded = stack.pop()
            if relation.id in self.contexts:
                continue
            inputs = [
                graph.nodes[e.source]
                for e in graph.in_edges(relation.id, 'feeds')
                if graph.nodes[e.source].kind in RELATION_KINDS and e.source != relation.id
            ]
            if not expanded:
                if relation.id in path:
                    continue
                path.add(relation.id)
                stack.append((relation, True))
                stack += [(r, False) for r in inputs if r.id not in self.contexts and r.id not in path]
                continue
            path.discard(relation.id)
            parts = sorted(str(c) for c in relation.attrs.get('conditions', []))
            parts += sorted(self.contexts.get(r.id, 'cycle') for r in inputs)
            self.contexts[relation.id] = hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()
        return self.contexts[owner.id]

    def fingerprints(self, definitions: Sequence[int]) -> list[str]:
        """The 128-bit fingerprint of each definition, in hexadecimal."""
        return [f'{value:032x}' for value in self.core.fingerprints(list(definitions))]

    def fingerprint(self, definition: int) -> str:
        return self.fingerprints([definition])[0]

    def compare(self, a: int, b: int) -> Verdict:
        """Whether definitions ``a`` and ``b`` compute the same thing."""
        equal, bound, without_nulls, found = self.core.compare(a, b)
        if found is None:
            return Verdict(equal, bound, without_nulls)
        kind, inputs, rows, results, functions, only_a, only_b = found
        # The inputs that are expressions, not columns: given a definition.
        assumed = {
            self._mention(name, definition, label)
            for name, definition, label, _ in [*inputs, *(i for row in rows or [] for i in row)]
            if definition is not None
        }
        return Verdict(
            equal,
            bound,
            without_nulls,
            Witness(
                kind=kind,
                inputs=self._inputs(inputs),
                rows=[self._inputs(row) for row in rows] if rows is not None else None,
                assumed=sorted(assumed),
                a=_shown(results[0]) if results else None,
                b=_shown(results[1]) if results else None,
                # Distinct functions can show the same name: each place RANDOM() is called.
                functions=list(dict.fromkeys(self.joins.get(f) or _display(f) for f in functions)),
                only_a=[self._mention(*m) for m in only_a],
                only_b=[self._mention(*m) for m in only_b],
            ),
        )

    def _render(self, definition: int, label: int | None) -> str | None:
        if label is None:
            return None
        labels, columns = self.labels[definition]

        def fill(item: exp.Expr) -> exp.Expr:
            if isinstance(item, exp.Column) and not item.table:
                found = _PLACEHOLDER.fullmatch(item.name)
                if found is not None:
                    return columns[int(found.group(1))].copy()
            return item

        node = labels[label].copy().transform(fill, copy=False)
        if self.graph is not None:
            # Shown with the short names a person reads, as the columns are.
            return short_sql(self.graph, node.sql(dialect=self.dialect))
        return node.sql(dialect=self.dialect)

    def _mention(self, name: str, definition: int, label: int | None) -> str:
        return self._render(definition, label) or self.joins.get(name) or _display(name)

    def _inputs(self, inputs: list[tuple[str, int | None, int | None, str | None]]) -> dict[str, str]:
        found = {}
        for name, definition, label, value in inputs:
            if definition is None:
                found[self.names.get(name, name)] = _shown(value)
            elif name == 'CASE' or name in self.joins:
                found[self._mention(name, definition, label)] = 'true' if value != '0' else 'false'
            else:
                found[self._mention(name, definition, label)] = _shown(value)
        return found


def _shown(value: str | None) -> str:
    return 'NULL' if value is None else value


def equivalent(
    a: str | exp.Expr, b: str | exp.Expr, *, dialect: str | None = None, not_null: Iterable[str] = ()
) -> Verdict:
    """Whether two SQL expressions over the same columns compute the same thing; the columns
    named in ``not_null`` are never NULL."""
    formulas = Formulas(dialect=dialect)
    columns = list(not_null)
    return formulas.compare(formulas.add(a, not_null=columns), formulas.add(b, not_null=columns))


def fingerprint(
    formula: str | exp.Expr,
    *,
    dialect: str | None = None,
    context: Iterable[str] = (),
    not_null: Iterable[str] = (),
) -> str:
    """A 128-bit fingerprint, in hexadecimal, equal for formulas that compute the same thing:
    group formulas by it with a dictionary. ``context`` lists the conditions the formula's
    aggregates run under, and ``not_null`` the columns that are never NULL. It is reproducible
    across builds, which makes it no guarantee against a formula built to collide: see
    :class:`Formulas`."""
    formulas = Formulas(dialect=dialect, fixed_key=True)
    return formulas.fingerprint(formulas.add(formula, context=context, not_null=not_null))


def column_fingerprints(graph: Graph, *, conditions: bool = True) -> dict[str, str]:
    """The fingerprint of every column's composed formula, by column id. Columns with equal
    fingerprints compute the same thing; with ``conditions``, also under the same joins,
    filters and grouping. They are reproducible across builds; see :class:`Formulas` for what a
    match guarantees."""
    formulas = Formulas(graph, conditions=conditions, fixed_key=True)
    columns = [n for n in graph.nodes.values() if n.kind == 'column']
    definitions = [formulas.column(n) for n in columns]
    return dict(zip((n.id for n in columns), formulas.fingerprints(definitions), strict=True))
