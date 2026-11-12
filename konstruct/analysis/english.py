"""A column's formula in plain English: ``konstruct explain COLUMN --english``.

The sentence is made from the formula by a small grammar, without a language model, so it says
what the SQL says and nothing more. Each kind of expression has one wording: ``a * b`` is always
``a times b``, ``SUM(x)`` is always ``the sum of x``, ``COALESCE(a, b)`` is always ``a, or b when a
has no value``. Columns are named in square brackets (``[customer id]``), so no column name is read
as part of the grammar. Where the SQL's grouping matters the sentence keeps it in brackets: an
operand that is itself an operation is bracketed unless it continues a chain of plus and minus, of
times, or of followed by, and a phrase such as ``the sum of x`` is bracketed as an operand of
arithmetic, so two formulas that compute different things never read alike. Anything the grammar
has no wording for, such as a subquery, a function it does not know or a dialect's own date
arithmetic, is quoted as SQL between backticks, and the result says so.

``NULL`` is called *no value* throughout: ``x IS NULL`` is ``x has no value``, ``COUNT(DISTINCT x)``
is ``the number of distinct values of x``. A test holds only where it is true, as in ``WHERE`` and
``CASE``, so a negated test says what it needs: ``x <> 'a'`` is ``x has a value other than 'a'``,
which a row with no value does not meet.

Columns are named by their words (``customer_id`` is ``[customer id]``), qualified by their relation
when two columns in the result would read alike; ``names`` maps each wording back to the column.
The joins, filters and grouping of every relation on the way are worded the same way, nearest
first, and so are the required filters of a business term bound to the column.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from konstruct.analysis.explain import Explainer, joined_reading, split_condition
from konstruct.core.graph import Graph, Node, split_reading
from konstruct.core.sql import PARSE_ERRORS
from konstruct.semantics.ontology import id_like

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style
    from konstruct.semantics.ontology import Ontology

ATOM, ARITHMETIC, PREDICATE, CONNECTIVE, CLAUSE = range(5)
"""How loosely a phrase binds. An operand is bracketed when its phrase binds more loosely than the
place it goes allows: an atom (``[x]``) goes anywhere, arithmetic (``a plus b``) goes unbracketed
only into a comparison, a predicate (``a is 1``) only beside ``and`` or ``or``, and a clause (``a if
b, else c``), which has commas of its own, only at the top or inside brackets."""

NO_VALUE = 'no value'
UNITS = frozenset(
    {'year', 'quarter', 'month', 'week', 'day', 'hour', 'minute', 'second', 'millisecond', 'microsecond'}
)
"""The date and time units worded; any other (``epoch``, ``dow``) is quoted as SQL."""


@dataclass
class Phrase:
    """English for one expression: its text, how loosely it binds, for arithmetic the chain it is
    (``add`` for plus and minus, ``times``, ``text``), which an operand of the same chain may
    continue, and whether it is a phrase such as ``the sum of x`` that ends in an operand, which
    a reader could take to run on into what follows. ``test`` marks a true-or-false test, and
    ``decided`` one that is always true or false, never without a value (``x has no value``)."""

    text: str
    level: int = ATOM
    chain: str | None = None
    prefix: bool = False
    test: bool = False
    decided: bool = False


_COMPARISONS: dict[type[exp.Expr], str] = {
    exp.EQ: 'is',
    exp.GT: 'is greater than',
    exp.GTE: 'is at least',
    exp.LT: 'is less than',
    exp.LTE: 'is at most',
}
# NOT of a comparison is the opposite comparison, in SQL's three-valued logic too: both are unknown
# where an operand has no value.
OPPOSITES: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.GT: exp.LTE,
    exp.LTE: exp.GT,
    exp.LT: exp.GTE,
    exp.GTE: exp.LT,
}
_ARITHMETIC: dict[type[exp.Expr], tuple[str, str | None]] = {
    exp.Add: ('plus', 'add'),
    exp.Sub: ('minus', 'add'),
    exp.Mul: ('times', 'times'),
    exp.Mod: ('modulo', None),
    exp.DPipe: ('followed by', 'text'),
}
_AGGREGATES: dict[type[exp.Expr], str] = {
    exp.Sum: 'the sum of',
    exp.Avg: 'the average of',
    exp.Min: 'the minimum of',
    exp.Max: 'the maximum of',
}
_PREFIX: dict[type[exp.Expr], str] = {
    exp.Abs: 'the absolute value of',
    exp.Length: 'the length of',
}
_POSTFIX: dict[type[exp.Expr], str] = {
    exp.Lower: 'in lower case',
    exp.Upper: 'in upper case',
    exp.Floor: 'rounded down to a whole number',
    exp.Ceil: 'rounded up to a whole number',
}
_TRUNCATIONS = (exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc, exp.TimeTrunc)


def fit(phrase: Phrase, loosest: int) -> str:
    """``phrase`` as an operand where phrases binding up to ``loosest`` need no brackets."""
    return phrase.text if phrase.level <= loosest else f'({phrase.text})'


def bare(node: exp.Expr) -> exp.Expr:
    """``node`` without the brackets around it."""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def is_null(node: exp.Expr) -> bool:
    """Whether ``node`` is NULL, bracketed or cast: ``(NULL)``, ``CAST(NULL AS INT)``."""
    node = bare(node)
    while isinstance(node, exp.Cast):
        node = bare(node.this)
    return isinstance(node, exp.Null)


def _value(node: exp.Expr) -> bool:
    """Whether ``node`` is a literal value other than NULL, which ``x is 'returned'`` names, rather
    than another column or computation, which ``x equals y`` compares."""
    node = bare(node)
    return isinstance(node, (exp.Literal, exp.Boolean, exp.Neg)) and not node.find(exp.Column, exp.Null)


def _whole(node: exp.Expr | None, smallest: int = 0) -> str | None:
    """The digits of a whole-number literal at least ``smallest``, else None."""
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    text = str(node.this)
    return text if text.isdigit() and int(text) >= smallest else None


def _unit(node: exp.Expr | None) -> str | None:
    """A known date or time unit in lower case, else None."""
    name = node.name.lower() if isinstance(node, (exp.Var, exp.Literal)) else None
    return name if name in UNITS else None


def _extra(node: exp.Expr, allowed: tuple[str, ...]) -> bool:
    """Whether ``node`` sets an argument beyond ``allowed``, which its wording would leave out."""
    return any(value not in (None, False, []) for key, value in node.args.items() if key not in allowed)


class Wording:
    """Turns SQL expressions into English, naming each column with ``name``.

    ``verbatim`` collects the SQL of every part that was quoted rather than worded."""

    def __init__(self, dialect: str | None, name: Callable[[exp.Column], str]) -> None:
        self.dialect = Dialect.get_or_raise(dialect)
        self.name = name
        self.verbatim: list[str] = []

    # Entry points

    def sentence(self, tree: exp.Expr) -> str:
        """``tree`` in English, with no brackets around the whole."""
        return self.phrase(tree).text

    def phrase(self, node: exp.Expr) -> Phrase:
        node = bare(node)
        handler = _HANDLERS.get(type(node))
        found = handler(self, node) if handler is not None else None
        return found if found is not None else self.quoted(node)

    def operand(self, node: exp.Expr, loosest: int) -> str:
        """``node``'s phrase as an operand where phrases up to ``loosest`` need no brackets."""
        return fit(self.phrase(node), loosest)

    def term(self, phrase: Phrase) -> str:
        """``phrase`` as an operand of arithmetic: an atom, and not a phrase that ends in an
        operand, which a reader could take to run on (``(the sum of x) plus 1``)."""
        return f'({phrase.text})' if phrase.prefix else fit(phrase, ATOM)

    def quoted(self, node: exp.Expr) -> Phrase:
        text = node.sql(dialect=self.dialect)
        # A part quoted whole stands for the parts inside it that were quoted on the way.
        self.verbatim = [v for v in self.verbatim if v not in text] + [text]
        return Phrase(f'`{text}`')

    def sql(self, node: exp.Expr) -> str:
        return node.sql(dialect=self.dialect)

    def listed(self, nodes: Iterable[exp.Expr]) -> str:
        """Several expressions in brackets, separated by commas: ``(a, b, c)``."""
        return '(' + ', '.join(self.operand(n, CONNECTIVE) for n in nodes) + ')'

    # Values

    def _column(self, node: exp.Column) -> Phrase | None:
        if isinstance(node.this, exp.Star):
            return None
        return Phrase(self.name(node))

    def _literal(self, node: exp.Literal) -> Phrase:
        return Phrase(self.sql(node) if node.is_string else str(node.this))

    def _null(self, node: exp.Null) -> Phrase:
        return Phrase(NO_VALUE)

    def _boolean(self, node: exp.Boolean) -> Phrase:
        return Phrase('true' if node.this else 'false')

    def _interval(self, node: exp.Interval) -> Phrase | None:
        value, unit = node.this, _unit(node.args.get('unit'))
        amount = str(value.this) if isinstance(value, exp.Literal) else ''
        if not amount.isdigit() or unit is None or _extra(node, ('this', 'unit')):
            return None
        return Phrase(f'{amount} {unit}' + ('' if amount == '1' else 's'))

    def _today(self, node: exp.Expr) -> Phrase | None:
        if _extra(node, ()):
            return None  # a time zone, a precision or SYSDATE's own clock: quoted
        return Phrase("today's date" if isinstance(node, exp.CurrentDate) else 'the current time')

    # Operators

    def _arithmetic(self, node: exp.Binary) -> Phrase:
        word, chain = _ARITHMETIC[type(node)]
        left = self.phrase(node.this)
        # A chain of plus and minus, of times or of followed by reads left to right, as SQL groups it.
        continues = chain is not None and left.level == ARITHMETIC and left.chain == chain and not left.prefix
        first = left.text if continues else self.term(left)
        return Phrase(f'{first} {word} {self.term(self.phrase(node.expression))}', ARITHMETIC, chain)

    def _divide(self, node: exp.Div | exp.SafeDivide) -> Phrase | None:
        if isinstance(node, exp.Div) and _extra(node, ('this', 'expression', 'typed', 'safe')):
            return None
        divisor = self.term(self.phrase(node.expression))
        text = f'{self.term(self.phrase(node.this))} divided by {divisor}'
        notes = []
        if node.args.get('typed'):
            # The dialect divides whole numbers as whole numbers (Postgres, T-SQL): 7 / 2 is 3.
            notes.append('rounded towards zero when both are whole numbers')
        if isinstance(node, exp.SafeDivide) or node.args.get('safe'):
            notes.append(f'with {NO_VALUE} where {divisor} is 0')
        if notes:
            return Phrase(f'{text}, ' + ' and '.join(notes), CLAUSE)
        return Phrase(text, ARITHMETIC)

    def _negative(self, node: exp.Neg) -> Phrase:
        if isinstance(node.this, exp.Literal) and not node.this.is_string:
            return Phrase(f'-{node.this.this}')
        return Phrase(f'the negative of {self.term(self.phrase(node.this))}', prefix=True)

    def _comparison(self, node: exp.Binary) -> Phrase | None:
        left, right = node.this, node.expression
        if is_null(left) or is_null(right):
            return None  # x = NULL is never true: quoted, not worded as a test for no value
        if isinstance(node, exp.NEQ):
            if _value(left) and not _value(right):
                left, right = right, left
            subject = self.operand(left, ARITHMETIC)
            other = self.operand(right, ARITHMETIC)
            if _value(right):
                return Phrase(f'{subject} has a value other than {other}', PREDICATE, test=True)
            return Phrase(f'{subject} and {other} both have values and differ', CLAUSE, test=True)
        word = _COMPARISONS[type(node)]
        subject, other = self.operand(left, ARITHMETIC), self.operand(right, ARITHMETIC)
        if isinstance(node, exp.EQ) and not _value(left) and not _value(right):
            # Two columns or computations, either of which may have no value, and then they never match.
            return Phrase(f'{subject} and {other} both have values and are equal', CLAUSE, test=True)
        return Phrase(f'{subject} {word} {other}', PREDICATE, test=True)

    def _is(self, node: exp.Is) -> Phrase | None:
        if is_null(node.expression):
            return Phrase(
                f'{self.operand(node.this, ARITHMETIC)} has no value', PREDICATE, test=True, decided=True
            )
        return None

    def _in(self, node: exp.In, negated: bool = False) -> Phrase | None:
        if (
            node.args.get('query')
            or node.args.get('unnest')
            or node.args.get('field')
            or not node.expressions
        ):
            return None
        if any(is_null(e) for e in node.expressions):
            return None  # a list with NULL in it: x NOT IN (1, NULL) is never true
        negated = negated != bool(node.args.get('negate'))
        subject = self.operand(node.this, ARITHMETIC)
        if not negated:
            return Phrase(f'{subject} is one of {self.listed(node.expressions)}', PREDICATE, test=True)
        if not all(_value(e) for e in node.expressions):
            return None  # a column in the list may have no value, and then no row is kept
        return Phrase(
            f'{subject} has a value that is not one of {self.listed(node.expressions)}', PREDICATE, test=True
        )

    def _between(self, node: exp.Between, negated: bool = False) -> Phrase | None:
        low, high = node.args['low'], node.args['high']
        if node.args.get('symmetric') or is_null(low) or is_null(high):
            return None
        if negated and not (_value(low) and _value(high)):
            return None
        bounds = f'{self.operand(low, ARITHMETIC)} and {self.operand(high, ARITHMETIC)} inclusive'
        subject = self.operand(node.this, ARITHMETIC)
        if negated:
            return Phrase(f'{subject} has a value that is not between {bounds}', PREDICATE, test=True)
        return Phrase(f'{subject} is between {bounds}', PREDICATE, test=True)

    def _like(self, node: exp.Like | exp.ILike, negated: bool = False) -> Phrase | None:
        if node.args.get('escape') or not _value(node.expression):
            return None
        negated = negated != bool(node.args.get('negate'))
        case = ', ignoring case' if isinstance(node, exp.ILike) else ''
        pattern = self.operand(node.expression, ARITHMETIC)
        subject = self.operand(node.this, ARITHMETIC)
        if negated:
            return Phrase(
                f'{subject} has a value that does not match the pattern {pattern}{case}', PREDICATE, test=True
            )
        return Phrase(f'{subject} matches the pattern {pattern}{case}', PREDICATE, test=True)

    def _not(self, node: exp.Not) -> Phrase | None:
        inner = bare(node.this)
        if isinstance(inner, exp.Is) and is_null(inner.expression):
            return Phrase(
                f'{self.operand(inner.this, ARITHMETIC)} has a value', PREDICATE, test=True, decided=True
            )
        if isinstance(inner, exp.In):
            return self._in(inner, negated=True)
        if isinstance(inner, exp.Between):
            return self._between(inner, negated=True)
        if isinstance(inner, (exp.Like, exp.ILike)):
            return self._like(inner, negated=True)
        opposite = OPPOSITES.get(type(inner))
        if opposite is not None:
            return self.phrase(opposite(this=inner.this.copy(), expression=inner.expression.copy()))
        if isinstance(inner, exp.Not):
            return self.phrase(inner.this)
        # Any other NOT holds where its operand is false, not where it has no value, and the
        # wording of the operand cannot say which: quoted.
        return None

    def _connective(self, node: exp.Connector) -> Phrase:
        word = 'and' if isinstance(node, exp.And) else 'or'
        left = self.phrase(node.this)
        same = left.level == CONNECTIVE and left.chain == word
        first = left.text if same else fit(left, PREDICATE)
        right = self.phrase(node.expression)
        decided = left.decided and right.decided
        return Phrase(f'{first} {word} {fit(right, PREDICATE)}', CONNECTIVE, word, test=True, decided=decided)

    # Choices

    def _case(self, node: exp.Case) -> Phrase | None:
        subject = node.this
        branches = []
        for branch in node.args.get('ifs') or []:
            condition = branch.this
            if subject is not None:
                condition = exp.EQ(this=subject.copy(), expression=condition.copy())
            result = self.operand(branch.args['true'], ARITHMETIC)
            branches.append(f'{result} if {self.operand(condition, CONNECTIVE)}')
        if not branches:
            return None
        default = node.args.get('default')
        otherwise = self.operand(default, ARITHMETIC) if default is not None else NO_VALUE
        return Phrase(', else '.join(branches) + f', else {otherwise}', CLAUSE)

    def _if(self, node: exp.If) -> Phrase:
        result = self.operand(node.args['true'], ARITHMETIC)
        default = node.args.get('false')
        otherwise = self.operand(default, ARITHMETIC) if default is not None else NO_VALUE
        return Phrase(f'{result} if {self.operand(node.this, CONNECTIVE)}, else {otherwise}', CLAUSE)

    def _coalesce(self, node: exp.Coalesce) -> Phrase | None:
        if _extra(node, ('this', 'expressions')):
            return None
        values = [node.this, *node.expressions]
        if len(values) == 2 and isinstance(bare(values[0]), (exp.Column, exp.Literal)):
            first = self.phrase(values[0]).text
            fallback = self.operand(values[1], ARITHMETIC)
            return Phrase(f'{first}, or {fallback} when {first} has no value', CLAUSE)
        return Phrase(f'the first of {self.listed(values)} that has a value')

    def _anonymous(self, node: exp.Anonymous) -> Phrase | None:
        if str(node.this).upper() != 'UNION' or len(node.expressions) < 2:
            return None
        # A set operation's column: the value of whichever branch the row comes from.
        first, *others = (self.operand(e, ATOM) for e in node.expressions)
        joiner = ' or, in the other branch, ' if len(others) == 1 else ' or, in another branch, '
        return Phrase(first + ''.join(joiner + o for o in others), CLAUSE)

    def _nullif(self, node: exp.Nullif) -> Phrase:
        value = self.operand(node.this, ARITHMETIC)
        same = self.operand(exp.EQ(this=node.this.copy(), expression=node.expression.copy()), CONNECTIVE)
        return Phrase(f'{value}, or {NO_VALUE} when {same}', CLAUSE)

    # Functions

    def _aggregate(self, node: exp.AggFunc) -> Phrase | None:
        word = _AGGREGATES[type(node)]
        inner = node.this
        if _extra(node, ('this',)):
            return None
        if isinstance(inner, exp.Distinct):
            if len(inner.expressions) != 1:
                return None
            return Phrase(
                f'{word} the distinct values of {self.term(self.phrase(inner.expressions[0]))}', prefix=True
            )
        if inner is None or isinstance(inner, exp.Star):
            return None
        return Phrase(f'{word} {self.term(self.phrase(inner))}', prefix=True)

    def _count(self, node: exp.Count) -> Phrase | None:
        inner = node.this
        if node.expressions or _extra(node, ('this', 'big_int')):
            return None
        if inner is None or isinstance(inner, exp.Star):
            return Phrase('the number of rows')
        if isinstance(inner, exp.Distinct):
            if len(inner.expressions) != 1:
                return None
            return Phrase(
                f'the number of distinct values of {self.term(self.phrase(inner.expressions[0]))}',
                prefix=True,
            )
        return Phrase(f'the number of rows in which {self.operand(inner, ARITHMETIC)} has a value', CLAUSE)

    def _count_if(self, node: exp.CountIf) -> Phrase:
        return Phrase(f'the number of rows where {self.operand(node.this, CONNECTIVE)}', CLAUSE)

    def _filter(self, node: exp.Filter) -> Phrase | None:
        where = node.expression
        if not isinstance(where, exp.Where) or isinstance(node.this, exp.Window):
            return None
        condition = self.operand(where.this, CONNECTIVE)
        if isinstance(node.this, exp.Count) and (
            node.this.this is None or isinstance(node.this.this, exp.Star)
        ):
            return Phrase(f'the number of rows where {condition}', CLAUSE)
        found = self.phrase(node.this)
        if found.level not in (ATOM, CLAUSE) or found.text.startswith('`'):
            return None
        return Phrase(f'{fit(found, ATOM)}, counting only the rows where {condition}', CLAUSE)

    def _prefix(self, node: exp.Func) -> Phrase | None:
        if _extra(node, ('this',)):
            return None
        return Phrase(f'{_PREFIX[type(node)]} {self.term(self.phrase(node.this))}', prefix=True)

    def _postfix(self, node: exp.Func) -> Phrase | None:
        if _extra(node, ('this',)):
            return None
        return Phrase(f'{self.term(self.phrase(node.this))} {_POSTFIX[type(node)]}', ARITHMETIC, 'postfix')

    def _round(self, node: exp.Round) -> Phrase | None:
        if _extra(node, ('this', 'decimals')):
            return None
        decimals = node.args.get('decimals')
        value = self.term(self.phrase(node.this))
        if decimals is None:
            return Phrase(f'{value} rounded to a whole number', ARITHMETIC, 'postfix')
        places = _whole(decimals)
        if places is None:
            return None  # ROUND(x, 1.5), ROUND(x, -1) or a computed number of places: quoted
        unit = 'decimal place' if places == '1' else 'decimal places'
        return Phrase(f'{value} rounded to {places} {unit}', ARITHMETIC, 'postfix')

    def _cast(self, node: exp.Cast) -> Phrase | None:
        # Snowflake's TRY_CAST takes only text (requires_string), which changes no result it gives.
        if _extra(node, ('this', 'to', 'safe', '_type', 'requires_string')):
            return None
        target = self.sql(node.to)
        value = self.term(self.phrase(node.this))
        if isinstance(node, exp.TryCast) or node.args.get('safe'):
            return Phrase(f'{value} converted to {target}, or {NO_VALUE} where it cannot be', CLAUSE)
        return Phrase(f'{value} converted to {target}', ARITHMETIC, 'postfix')

    def _truncation(self, node: exp.Func) -> Phrase | None:
        unit = _unit(node.args.get('unit'))
        if unit is None or _extra(node, ('this', 'unit')):
            return None
        return Phrase(f'the start of the {unit} of {self.term(self.phrase(node.this))}', prefix=True)

    def _extract(self, node: exp.Extract) -> Phrase | None:
        unit = _unit(node.this)
        if unit is None:
            return None
        return Phrase(f'the {unit} of {self.term(self.phrase(node.expression))}', prefix=True)

    # Windows

    def _window(self, node: exp.Window) -> Phrase | None:
        if (
            node.args.get('alias')
            or not isinstance(node.this, exp.Func)
            or _extra(node, ('this', 'partition_by', 'order', 'spec', 'over'))
        ):
            return None
        function = node.this
        partition = node.args.get('partition_by') or []
        if len(partition) == 1:
            rows = f'the rows with the same {self.operand(partition[0], ARITHMETIC)}'
        elif partition:
            rows = f'the rows with the same {self.listed(partition)}'
        else:
            rows = 'all rows'
        order = node.args.get('order')
        keys = order.expressions if isinstance(order, exp.Order) else []
        ordered = ''
        if keys:
            shown = [self.ordered(k) for k in keys]
            if any(s is None for s in shown):
                return None
            joined = shown[0] if len(shown) == 1 else '(' + ', '.join(s or '' for s in shown) + ')'
            ordered = f' in order of {joined}'
        among = f'{rows}{ordered}'
        spec = node.args.get('spec')
        if isinstance(function, exp.RowNumber):
            return (
                None
                if spec
                else Phrase(f'the position of this row, counting from one, among {among}', CLAUSE)
            )
        if isinstance(function, (exp.Rank, exp.DenseRank)) and not function.expressions:
            if spec or not keys:
                return None
            gaps = (
                'and the next rank skips the tied rows' if isinstance(function, exp.Rank) else 'with no gaps'
            )
            return Phrase(f'the rank of this row among {among}, where tied rows share a rank {gaps}', CLAUSE)
        if isinstance(function, (exp.Lag, exp.Lead)):
            return None if spec else self._offset(function, among)
        frame = self._frame(spec, bool(keys))
        if frame is None:
            return None
        within = f'{among}{frame}'
        if isinstance(function, (exp.FirstValue, exp.LastValue)):
            if _extra(function, ('this',)):
                return None
            which = 'first' if isinstance(function, exp.FirstValue) else 'last'
            return Phrase(
                f'the value of {self.term(self.phrase(function.this))} in the {which} of {within}', CLAUSE
            )
        if type(function) in _AGGREGATES or isinstance(function, exp.Count):
            found = self.phrase(function)
            if found.level != ATOM or found.text.startswith('`'):
                return None
            return Phrase(f'{found.text} over {within}', CLAUSE)
        return None

    def ordered(self, key: exp.Expr) -> str | None:
        """An ORDER BY key: the expression, ``descending`` where it is, and where the rows with no
        value go, which dialects place differently when the SQL does not say."""
        if not isinstance(key, exp.Ordered):
            return None
        nulls = key.args.get('nulls_first')
        if nulls is None or _extra(key, ('this', 'desc', 'nulls_first')):
            return None
        text = self.operand(key.this, ARITHMETIC)
        direction = ' descending' if key.args.get('desc') else ''
        return f'{text}{direction} ({NO_VALUE} {"first" if nulls else "last"})'

    def _offset(self, function: exp.Lag | exp.Lead, among: str) -> Phrase | None:
        offset, default = function.args.get('offset'), function.args.get('default')
        count = _whole(offset, 1) if offset is not None else '1'
        if count is None or _extra(function, ('this', 'offset', 'default')):
            return None
        direction = 'before' if isinstance(function, exp.Lag) else 'after'
        if count == '1':
            place = 'the previous row' if direction == 'before' else 'the next row'
        else:
            place = f'the row {count} rows {direction} this one'
        text = f'the value of {self.term(self.phrase(function.this))} in {place} among {among}'
        if default is not None:
            text += f', or {self.operand(default, ARITHMETIC)} where there is no such row'
        return Phrase(text, CLAUSE)

    def _frame(self, spec: exp.Expr | None, ordered: bool) -> str | None:
        """The rows a window function reads, after the rows and order: for an ordered window with
        no frame, SQL's default, from the first row to this one and the rows tied with it."""
        if spec is None:
            return ', from the first row to this row and the rows tied with it' if ordered else ''
        if not isinstance(spec, exp.WindowSpec) or spec.args.get('exclude'):
            return None
        kind = str(spec.args.get('kind') or '').upper()
        if kind not in ('ROWS', 'RANGE'):
            return None
        start = self._bound(spec.args.get('start'), spec.args.get('start_side'), kind)
        end = self._bound(spec.args.get('end') or 'CURRENT ROW', spec.args.get('end_side'), kind)
        if start is None or end is None:
            return None
        return f', from {start} to {end}'

    def _bound(self, value: Any, side: Any, kind: str) -> str | None:
        text = value.sql(dialect=self.dialect) if isinstance(value, exp.Expr) else str(value or '')
        side = str(side or '').upper()
        if text.upper() == 'UNBOUNDED':
            return 'the first row' if side == 'PRECEDING' else 'the last row' if side == 'FOLLOWING' else None
        if text.upper() == 'CURRENT ROW':
            return 'this row' if kind == 'ROWS' else 'this row and the rows tied with it'
        if kind == 'ROWS' and text.isdigit() and side in ('PRECEDING', 'FOLLOWING'):
            word = 'row' if text == '1' else 'rows'
            return f'{text} {word} {"before" if side == "PRECEDING" else "after"} this row'
        return None


_HANDLERS: dict[type[exp.Expr], Callable[[Wording, Any], Phrase | None]] = {
    exp.Column: Wording._column,
    exp.Literal: Wording._literal,
    exp.Null: Wording._null,
    exp.Boolean: Wording._boolean,
    exp.Interval: Wording._interval,
    exp.CurrentDate: Wording._today,
    exp.CurrentTimestamp: Wording._today,
    exp.Neg: Wording._negative,
    exp.Div: Wording._divide,
    exp.SafeDivide: Wording._divide,
    exp.Is: Wording._is,
    exp.In: Wording._in,
    exp.Between: Wording._between,
    exp.Like: Wording._like,
    exp.ILike: Wording._like,
    exp.Not: Wording._not,
    exp.And: Wording._connective,
    exp.Or: Wording._connective,
    exp.Case: Wording._case,
    exp.If: Wording._if,
    exp.Coalesce: Wording._coalesce,
    exp.Nullif: Wording._nullif,
    exp.Anonymous: Wording._anonymous,
    exp.Count: Wording._count,
    exp.CountIf: Wording._count_if,
    exp.Filter: Wording._filter,
    exp.Round: Wording._round,
    exp.Cast: Wording._cast,
    exp.TryCast: Wording._cast,
    exp.Extract: Wording._extract,
    exp.Window: Wording._window,
    exp.NEQ: Wording._comparison,
    **dict.fromkeys(_ARITHMETIC, Wording._arithmetic),
    **dict.fromkeys(_COMPARISONS, Wording._comparison),
    **dict.fromkeys(_AGGREGATES, Wording._aggregate),
    **dict.fromkeys(_PREFIX, Wording._prefix),
    **dict.fromkeys(_POSTFIX, Wording._postfix),
    **dict.fromkeys(_TRUNCATIONS, Wording._truncation),
}


# Names


def words(name: str) -> str:
    """A column name as words: ``customer_id`` is ``customer id``."""
    return ' '.join(name.replace('_', ' ').split()) or name


_ORDINALS = ('first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth')
_READING = re.compile(r'(\d+)?((?:/\d+)*)(?:\((.+)\))?')


def _ordinal(number: str) -> str:
    value = int(number)
    if 0 < value <= len(_ORDINALS):
        return _ORDINALS[value - 1]
    suffix = 'th' if 10 <= value % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(value % 10, 'th')
    return f'{value}{suffix}'


def reading_words(name: str) -> str:
    """A relation's name as words, where it names one reading of a relation a query reads more
    than once (see :func:`konstruct.core.graph.split_reading`): ``orders#2`` is ``the second
    reading of orders``, ``orders#2/1`` the second reading through the first reference to the CTE
    that reads it, ``orders#2/1/3`` through the first reference to one CTE and then the third to
    another that reads that one, and ``orders#(base#1)`` orders as read through the first reading
    of base."""
    relation, reading = split_reading(name)
    if not reading:
        return relation
    match = _READING.fullmatch(reading)
    if match is None or not any(match.groups()):
        return f'{relation} (reading {reading})'
    number, path, through = match.groups()
    text = f'the {_ordinal(number)} reading of {relation}' if number else relation
    references = [_ordinal(r) for r in path.split('/') if r]
    if len(references) == 1:
        text += f', through the {references[0]} reference to its CTE'
    elif references:
        text += f', through the {references[0]} reference to one CTE' + ''.join(
            f', then the {r} to another' for r in references[1:]
        )
    if through:
        text += f', as read through {reading_words(through)}'
    return text


@dataclass
class Name:
    """How a sentence names one column."""

    wording: str
    """The name in the sentences, in square brackets (``[customer id]``)."""
    column: str
    """The column it names, as ``relation.column``."""

    def to_dict(self) -> dict[str, Any]:
        return {'wording': self.wording, 'column': self.column}


class Names:
    """The wording of each column in one result: its name as words in square brackets, with its
    relation where two columns of the result would otherwise read alike, and its name as written
    where even that would not tell them apart."""

    def __init__(self, columns: Iterable[tuple[str, str]]) -> None:
        by_words: dict[str, set[tuple[str, str]]] = {}
        for table, name in columns:
            by_words.setdefault(words(name).lower(), set()).add((table, name))
        shown: dict[tuple[str, str], str] = {}
        for found in by_words.values():
            for table, name in found:
                plain = words(name)
                # A column of one reading of a relation read more than once says which, always.
                qualified = table and (len(found) > 1 or split_reading(table)[1])
                shown[table, name] = f'{plain} ({reading_words(table)})' if qualified else plain
        # A wording can still meet another, qualified or not (a column named "x (t)" and t.x): those
        # read as written, and as SQL where even that is shared, which no two columns are.
        for fallback in (_written, _as_sql):
            seen: dict[str, int] = {}
            for text in shown.values():
                seen[text.lower()] = seen.get(text.lower(), 0) + 1
            shown = {key: fallback(*key) if seen[text.lower()] > 1 else text for key, text in shown.items()}
        self.shown = {key: '[' + text.replace(']', ']]') + ']' for key, text in shown.items()}

    def __call__(self, column: exp.Column) -> str:
        key = column_key(column)
        return self.shown.get(key) or '[' + words(key[1]).replace(']', ']]') + ']'

    def legend(self) -> list[Name]:
        """Each wording and the column it names."""
        return [
            Name(shown, f'{table}.{name}' if table else name)
            for (table, name), shown in sorted(self.shown.items(), key=lambda item: item[1].lower())
        ]


def _written(table: str, name: str) -> str:
    return f'{name} ({table})' if table else name


def _as_sql(table: str, name: str) -> str:
    return exp.column(exp.to_identifier(name, quoted=True), table or None).sql()


def column_key(column: exp.Column) -> tuple[str, str]:
    """A column reference as its relation (the qualifier as written) and its name."""
    parts = [p.name for p in column.parts]
    return '.'.join(parts[:-1]), parts[-1]


def column_keys(trees: Iterable[exp.Expr]) -> list[tuple[str, str]]:
    """Every column the expressions ``trees`` read, as :func:`column_key` gives it."""
    return [
        column_key(c) for tree in trees for c in tree.find_all(exp.Column) if not isinstance(c.this, exp.Star)
    ]


# Conditions

_JOINS = {
    'JOIN ON': 'rows are joined where {c}, and a row with no match on the other side is left out',
    'INNER JOIN ON': 'rows are joined where {c}, and a row with no match on the other side is left out',
    'LEFT JOIN ON': 'rows are joined where {c}, keeping every row of the left side, with no values from '
    'the right side where it has no match',
    'LEFT OUTER JOIN ON': 'rows are joined where {c}, keeping every row of the left side, with no values '
    'from the right side where it has no match',
    'RIGHT JOIN ON': 'rows are joined where {c}, keeping every row of the right side, with no values from '
    'the left side where it has no match',
    'RIGHT OUTER JOIN ON': 'rows are joined where {c}, keeping every row of the right side, with no values '
    'from the left side where it has no match',
    'FULL JOIN ON': 'rows are joined where {c}, keeping every row of both sides, with no values from the '
    'other side where a row has no match',
    'FULL OUTER JOIN ON': 'rows are joined where {c}, keeping every row of both sides, with no values '
    'from the other side where a row has no match',
}
_STEPS = {'WINDOW': 'the window functions', 'LIMIT': 'the limit'}


class Condition:
    """One recorded condition (``WHERE: a > 0``), parsed: its clause, the steps it applies before,
    and the expressions it holds."""

    def __init__(self, text: str, dialect: Dialect) -> None:
        self.text = text
        label, rest = split_condition(text)
        label, self.joined = joined_reading(label)
        base, _, before = label.partition(' BEFORE ')
        self.clause = base
        self.before = [s for s in before.split(', ') if s]
        self.rest = rest
        self.trees: list[exp.Expr] = []
        self.ordered: list[exp.Ordered] = []
        try:
            if base in ('GROUP BY', 'DISTINCT', 'DISTINCT ON'):
                select = sqlglot.parse_one(f'SELECT {rest}', read=dialect)
                self.trees = list(select.selects) if isinstance(select, exp.Select) else []
            elif base == 'ORDER BY':
                select = sqlglot.parse_one(f'SELECT 1 ORDER BY {rest}', read=dialect)
                order = select.args.get('order')
                self.ordered = list(order.expressions) if isinstance(order, exp.Order) else []
                self.trees = [o.this for o in self.ordered]
            elif base.endswith(('JOIN ON', 'JOIN WHERE')) or base in ('WHERE', 'HAVING', 'QUALIFY'):
                tree: exp.Expr = sqlglot.parse_one(rest, read=dialect)
                tree.parent = None
                self.trees = [tree]
        except PARSE_ERRORS:
            self.trees = []


def unrepeated(texts: Iterable[str]) -> list[str]:
    """``texts``, recorded conditions, without each join that another of them repeats naming the
    reading it joins (``LEFT JOIN ON: TRUE`` beside ``LEFT JOIN db.main."orders#2" ON: TRUE``,
    see :func:`konstruct.analysis.explain.joined_reading`): worded, the two would say the same."""
    listed = list(texts)
    named = set()
    for text in listed:
        label, rest = split_condition(text)
        base, reading = joined_reading(label)
        if reading is not None:
            named.add((base, rest))
    return [t for t in listed if split_condition(t) not in named]


def word_condition(condition: Condition, wording: Wording) -> str:
    """A condition in English, or its SQL between backticks where the grammar has no wording."""
    clause, trees = condition.clause, condition.trees

    def one() -> str:
        return wording.sentence(trees[0])

    text: str | None = None
    if clause == 'WHERE' and trees:
        text = f'rows are kept only where {one()}'
    elif clause == 'HAVING' and trees:
        text = f'groups are kept only where {one()}'
    elif clause == 'QUALIFY' and trees:
        text = f'rows are kept only where {one()}, after the window functions'
    elif clause in _JOINS and trees:
        text = _JOINS[clause].format(c=one())
        if condition.joined is not None:
            # A join whose ON does not name the reading it joins, named for it.
            named = reading_words(condition.joined.replace('"', '').split('.')[-1])
            text = text.replace('rows are joined', f'the rows of {named} are joined', 1)
    elif clause in ('LEFT JOIN', 'FULL JOIN') and condition.rest:
        # The relation read on the side of an outer join that may find no match.
        side = clause.removesuffix(' JOIN').lower()
        text = (
            f'{reading_words(condition.rest.replace(chr(34), ""))} is on the side of a {side} join that '
            'may find no match, so a row with no match in it is kept, with no values from it'
        )
    elif clause.endswith(' JOIN WHERE') and trees:
        side = clause.removesuffix(' JOIN WHERE').lower()
        text = f'on the side a {side} join may find no match in, rows are joined only where {one()}'
    elif clause == 'GROUP BY' and trees:
        text = (
            f'rows are grouped by {wording.operand(trees[0], ARITHMETIC)}, one row for each distinct value'
            if len(trees) == 1
            else f'rows are grouped by {wording.listed(trees)}, one row for each distinct combination'
        )
    elif clause == 'DISTINCT' and condition.rest:
        text = 'repeated rows are kept once'
    elif clause == 'DISTINCT ON' and trees:
        distinct = wording.listed(trees) if len(trees) > 1 else wording.operand(trees[0], ARITHMETIC)
        text = f'one row is kept for each distinct {distinct}, the first in the order given'
    elif clause == 'ORDER BY' and condition.ordered:
        keys = [wording.ordered(o) or '' for o in condition.ordered]
        text = f'rows are taken in order of {keys[0] if len(keys) == 1 else "(" + ", ".join(keys) + ")"}'
    elif clause == 'LIMIT' and condition.rest.isdigit():
        text = f'at most {condition.rest} rows'
    elif clause == 'OFFSET' and condition.rest.isdigit():
        text = f'the first {condition.rest} rows are skipped'
    elif clause == 'UNION' and condition.rest == 'DISTINCT':
        text = 'the rows of each part combined, with repeated rows kept once'
    if text is None:
        wording.verbatim.append(condition.text)
        return f'`{condition.text}`'
    if condition.before:
        steps = [_STEPS.get(s, s.lower()) for s in condition.before]
        text += f' (before {" and ".join(steps)})'
    return text


# The result


@dataclass
class Rows:
    """The joins, filters and grouping of one relation on the way, in SQL and in English."""

    relation: str
    """The relation, by label."""
    conditions: list[str] = field(default_factory=list)
    """Each condition as ``explain`` shows it (``WHERE: stg_orders.status <> 'returned'``)."""
    english: list[str] = field(default_factory=list)
    """Each condition in English, in the same order."""

    def to_dict(self) -> dict[str, Any]:
        return {'relation': self.relation, 'conditions': self.conditions, 'english': self.english}


@dataclass
class Term:
    """A business term bound to the column, with what its definition adds to the formula."""

    name: str
    """The term's name."""
    status: str | None = None
    """``draft``, ``agreed``, ``approved`` or ``retired``."""
    kind: str | None = None
    """``entity``, ``measure`` or ``term``, where the definition says."""
    owner: str | None = None
    """Who owns the definition."""
    definition: str | None = None
    """The agreed wording."""
    grain: str | None = None
    """The term's grain as written (``one row per customer``)."""
    unit: str | None = None
    """Its unit, such as ``currency``."""
    currency: str | None = None
    """Its currency, for a monetary measure."""
    filters: list[str] = field(default_factory=list)
    """The filters any answer must apply, as SQL."""
    english: list[str] = field(default_factory=list)
    """The same filters in English."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


@dataclass
class English:
    """A column's formula, the rows it is computed over and its business terms, in English."""

    column: str
    """The column id."""
    label: str
    """Its short name."""
    formula: str | None
    """The SQL the sentence renders: the formula composed down to sources where it could be
    composed, else the column's own expression; None for a source column."""
    english: str | None
    """``formula`` in English."""
    sentence: str | None
    """The column and its formula as one sentence (``customers.ltv is the sum of amount.``)."""
    composed: bool = False
    """Whether ``formula`` is in terms of source columns."""
    complete: bool = True
    """False when some column on the way has no recorded expression, so the formula stops short."""
    rows: list[Rows] = field(default_factory=list)
    """The joins, filters and grouping of each relation on the way, nearest first."""
    grain: str | None = None
    """What one row of the column's relation is, and the evidence: a bound term's grain, or the
    key of the entity the relation holds."""
    terms: list[Term] = field(default_factory=list)
    """The business terms bound to the column, approved first."""
    names: list[Name] = field(default_factory=list)
    """Each column's wording in the sentences and the column it names."""
    verbatim: list[str] = field(default_factory=list)
    """The SQL of each part the grammar has no wording for, quoted between backticks instead."""

    @property
    def exact(self) -> bool:
        """Whether every part is worded, with nothing quoted as SQL."""
        return not self.verbatim

    def to_dict(self) -> dict[str, Any]:
        return {
            'column': self.column,
            'label': self.label,
            'formula': self.formula,
            'english': self.english,
            'sentence': self.sentence,
            'composed': self.composed,
            'complete': self.complete,
            'rows': [r.to_dict() for r in self.rows],
            'grain': self.grain,
            'terms': [t.to_dict() for t in self.terms],
            'names': [n.to_dict() for n in self.names],
            'verbatim': self.verbatim,
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import english_lines, show

        return show(english_lines, self, style)

    def __str__(self) -> str:
        return self.text()


_STATUS_ORDER = {'approved': 0, 'agreed': 1, 'draft': 2, 'retired': 3}


def parse(sql: str, dialect: Dialect) -> exp.Expr | None:
    """One expression from ``sql``, detached, or None where it does not parse."""
    try:
        tree: exp.Expr = sqlglot.parse_one(sql, read=dialect)
    except PARSE_ERRORS:
        return None
    tree.parent = None
    return tree


def subject_sentence(subject: str, phrase: Phrase) -> str:
    """``subject is <phrase>.``, or for a true-or-false formula ``subject is whether <phrase>``,
    with, unless the test is always decided, what it gives where a value it tests has none."""
    if not phrase.test:
        return f'{subject} is {phrase.text}.'
    if phrase.decided:
        return f'{subject} is whether {phrase.text}.'
    return (
        f'{subject} is whether {phrase.text}; where a value it tests has none, it can have no value '
        'rather than be true or false.'
    )


def english(graph: Graph, column: Node, *, ontology: Ontology | None = None) -> English:
    """``column``'s formula in English, with the joins, filters and grouping of each relation on
    the way, the grain of its relation and the business terms bound to it.

    ``ontology``, when given, supplies the grain of a relation keyed by an entity."""
    explainer = Explainer(graph)
    explanation = explainer.explain(column)
    dialect = Dialect.get_or_raise(
        graph.meta.get('dialect') if isinstance(graph.meta.get('dialect'), str) else None
    )
    formula = explanation.composed or explanation.expression
    composed = explanation.composed is not None and explanation.composed != explanation.expression
    tree = parse(formula, dialect) if formula else None
    relations: list[tuple[str, list[Condition]]] = []
    seen: set[str] = set()
    for step in explanation.steps:
        owner = graph.parent_of(graph.nodes[step.column])
        if owner is None or owner.id in seen or not step.conditions:
            continue
        seen.add(owner.id)
        relations.append((graph.label(owner), [Condition(c, dialect) for c in step.conditions]))
    terms = _terms(graph, column)
    filters = {f: parse(f, dialect) for t in terms for f in t.filters}
    trees = [t for t in [tree, *filters.values()] if t is not None]
    trees += [t for _, conditions in relations for c in conditions for t in c.trees]
    names = Names(column_keys(trees))
    wording = Wording(
        graph.meta.get('dialect') if isinstance(graph.meta.get('dialect'), str) else None, names
    )
    result = English(
        column=column.id,
        label=graph.label(column),
        formula=formula,
        english=None,
        sentence=None,
        composed=composed,
        complete=explanation.complete,
    )
    if tree is not None:
        phrase = wording.phrase(tree)
        result.english = phrase.text
        # The approved term that names what the column measures; an entity's term names its key.
        named = next(
            (
                t.name
                for t in terms
                if t.status == 'approved'
                and t.kind != 'entity'
                and not (t.kind is None and id_like(column.name))
            ),
            None,
        )
        subject = f'{named} ({result.label})' if named else result.label
        result.sentence = subject_sentence(subject, phrase)
    elif formula:
        result.english = f'`{formula}`'
        wording.verbatim.append(formula)
    for label, conditions in relations:
        result.rows.append(
            Rows(label, [c.text for c in conditions], [word_condition(c, wording) for c in conditions])
        )
    for term in terms:
        for text in term.filters:
            parsed = filters.get(text)
            term.english.append(wording.sentence(parsed) if parsed is not None else f'`{text}`')
            if parsed is None:
                wording.verbatim.append(text)
    result.terms = terms
    result.grain = _grain(graph, column, terms, ontology)
    result.names = names.legend()
    result.verbatim = list(dict.fromkeys(wording.verbatim))
    return result


def _terms(graph: Graph, column: Node) -> list[Term]:
    found = []
    for edge in graph.in_edges(column.id, 'defines'):
        node = graph.nodes.get(edge.source)
        if node is None or node.kind != 'term' or edge.attrs.get('role') == 'input':
            continue
        attrs = node.attrs
        owner = attrs.get('owner')
        found.append(
            Term(
                name=node.name,
                status=attrs.get('status'),
                kind=attrs.get('kind'),
                owner=owner.get('name') if isinstance(owner, dict) else None,
                definition=node.description,
                grain=attrs.get('grain'),
                unit=attrs.get('unit'),
                currency=attrs.get('currency'),
                filters=[str(f) for f in attrs.get('filters') or []],
            )
        )
    found.sort(key=lambda t: (_STATUS_ORDER.get(t.status or '', 9), t.name.lower()))
    return found


def _grain(graph: Graph, column: Node, terms: list[Term], ontology: Ontology | None) -> str | None:
    for term in terms:
        if term.grain:
            return f'{term.grain}, as the term {term.name!r} states'
    owner = graph.parent_of(column)
    if owner is None or ontology is None:
        return None
    label = graph.label(owner)
    for entity in ontology.entities:
        if label in entity.relations:
            key = next((k for k in entity.keys if k.startswith(f'{label}.')), entity.key)
            evidence = next((e for e in entity.evidence if key in e), None)
            because = f', by the {evidence}' if evidence else ''
            return f'one row per {entity.name}: {key} is its key{because}'
    return None
