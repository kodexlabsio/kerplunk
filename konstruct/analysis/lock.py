"""`konstruct lock`: a lockfile for meaning, owned by the project that reads upstream data.

A consumer of another team's data (a dashboard repository, a downstream dbt project, a feature
pipeline, an agent) pins exactly what it relies on in ``konstruct.lock``: for each upstream column
it reads, a fingerprint of the column's formula composed down to source columns, with the row
conditions of every relation on the way (including those of the relations it is joined to,
filtered or grouped by), the source columns it reaches, its type, the business terms bound to it
(with their status and a version of their meaning) and its verification state. A relation read
on the side of an outer join that may find no row of it (the joined side of a ``LEFT JOIN``) is
under that join, with every relation upstream of it: its filters are part of the join's ``ON``,
as they decide which rows join rather than which rows there are.

``konstruct lock --check`` then fails when a pinned column's formula, conditions, sources or type,
or an approved term bound to it, is not what was locked. The failure says what changed (the
locked and current formula side by side, composed down to source), who changed it (from git
history, as ``konstruct history`` reads it), which of the consumer's own columns read it, and the
command to accept the change, or the commit to revert to keep the locked meaning.

The fingerprints are built from the canonical expressions the build records (CTEs, subqueries and
aliases inlined), composed as ``konstruct explain`` composes them and printed in a canonical form:
the operands of a comparison in a fixed order (``10 <= a`` is ``a >= 10``), those of ``AND`` and
``OR`` flattened through brackets and in a fixed order, the items of an ``IN`` list in a fixed
order and each once, the conjuncts of a condition one by one, and no trailing zeros on decimals.
So reformatting, renamed CTEs, reordered columns, passthrough models and a filter moved into a
model upstream, or into the ``ON`` of the outer join that reads it, are not changes.
The canonical form depends on Konstruct and on the sqlglot release that prints it, so the file
records it (``canonical``): checked by a Konstruct that prints another, a lockfile whose pins all
read the same passes, and one whose formulas or conditions do not fails once, asking to re-lock
after review, rather than pin by pin; a removed column, a new type or a changed definition fails as
it would in the same form.

A seed is pinned by a checksum of its file, as its rows are data. A column with no recorded
expression (a Python model, SQL that could not be read) is pinned by a checksum of the file that
builds it, and marked incomplete.

The file is TOML, sorted and with one list item per line, so a change to it reads well in review.
Each relation's conditions are stored once, in a ``[[relation]]`` table, and each pin names the
relations it relies on, with the outer join it reaches a relation through, if any::

    # Konstruct lockfile: the upstream columns this project reads, pinned by meaning.
    format = 1
    canonical = "konstruct-2/sqlglot-30.30"
    reads = []

    [[pin]]
    column = "main.ltv.total"
    id = "table.main.ltv#total"
    repository = "finance"
    type = "DOUBLE"
    fingerprint = "sha256:5c0d2b8e1f3a4c69..."
    formula = "SUM(raw.orders.amount)"
    relations = [
      "LEFT JOIN main.customers",
      "main.ltv",
    ]
    sources = [
      "raw.orders.amount",
    ]
    used_by = [
      "report.doubled",
    ]

    [[pin.terms]]
    name = "Lifetime value"
    status = "approved"
    approved = "2026-10-30"
    version = "4be1f0a27c3d"
    definition = "What a customer has paid."

    [[relation]]
    name = "main.customers"
    conditions = [
      "WHERE: raw.customers.active",
    ]

    [[relation]]
    name = "main.ltv"
    conditions = [
      "GROUP BY: raw.orders.customer_id",
      "LEFT JOIN ON: raw.customers.id = raw.orders.customer_id",
    ]

A pin kept as locked may rely on conditions a relation no longer has; they are stored beside the
current ones, as ``name~2`` and so on.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tomllib
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, TypeVar

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError

from konstruct.analysis.diff import run_git
from konstruct.analysis.explain import Explainer, composed_from, condition_terms
from konstruct.analysis.history import defining_file, history, utc
from konstruct.analysis.query import QueryError, resolve_one
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import OUTER_SIDES, in_context

FILE_NAME = 'konstruct.lock'
FORMAT = 1
"""The layout of the file: which keys it has and what they hold."""
CANONICAL_FORM = 2
"""How Konstruct prints the formulas and conditions it fingerprints. Raise it with any change
that prints one differently (a new normalisation, a core that is not sqlglot's printer)."""
CANONICAL = f'konstruct-{CANONICAL_FORM}/sqlglot-{".".join(package_version("sqlglot").split(".")[:2])}'
"""The canonical form this Konstruct writes, with the sqlglot release whose printer it uses."""
MAX_ATTRIBUTED_STEPS = 12
"""The most columns on a changed formula's path whose git history is read to say who changed it."""

_HEADER = (
    '# Konstruct lockfile: the upstream columns this project reads, pinned by meaning.\n'
    '# `konstruct lock --check` fails when one changes; `konstruct lock --update` accepts a change.\n'
)
_MEANING = ('definition', 'grain', 'columns', 'filters', 'unit', 'currency', 'exceptions')
_FINGERPRINT = re.compile(r'sha256:[0-9a-f]{64}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_VERSION = re.compile(r'~\d+$')
# The reasons a canonical form can give, as they compare printed text.
_PRINTED = ('formula', 'conditions', 'upstream')
# Where the canonical form can differ from the composed text: a comparison, an IN list, AND and
# OR, a decimal.
_NORMALISED = re.compile(r'=|<|>|\bIN\b|\bOR\b|\bAND\b|\d\.\d*0\b', re.IGNORECASE)
_DECIMAL = re.compile(r'(\d*)\.(\d*?)0+')


class LockError(QueryError):
    """A lockfile that cannot be read or written, or a selection that names nothing to pin."""


# The file


class Malformed(ValueError):
    """A stored pin, term or record that is not in the shape its format gives."""


def _text(data: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = data.get(key)
    if value is None:
        if required:
            raise Malformed(f'{key!r} is missing')
        return None
    if not isinstance(value, str):
        raise Malformed(f'{key!r} must be a string')
    return value or None


def _known(data: dict[str, Any], keys: Iterable[str], what: str) -> None:
    """Refuse a key the format does not have, which Konstruct would otherwise drop when it next
    writes the file."""
    unknown = sorted(set(data) - set(keys))
    if unknown:
        raise Malformed(f'{what} has a key the format does not have: {unknown[0]!r}')


def _texts(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Malformed(f'{key!r} must be a list of strings')
    return list(value)


@dataclass
class PinnedTerm:
    """A business term bound to a pinned column, as it was when the column was locked."""

    name: str
    status: str
    """``draft``, ``agreed``, ``approved`` or ``retired``."""
    version: str
    """A hash of the term's meaning: its definition, grain, columns, filters, unit, currency,
    exceptions and, for a measure with one, its formula. Rewording any of them is a new version;
    a new approval date alone is not."""
    approved: str | None = None
    """The approval date."""
    owner: str | None = None
    definition: str | None = None
    grain: str | None = None
    filters: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    unit: str | None = None
    currency: str | None = None
    exceptions: str | None = None
    formula: str | None = None
    """For a measure defined by a formula (an Ossie metric), the formula."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], '')}

    @classmethod
    def from_dict(cls, data: Any) -> PinnedTerm:
        if not isinstance(data, dict):
            raise Malformed('a term must be a table')
        _known(data, cls.__dataclass_fields__, 'a term')
        return cls(
            name=str(_text(data, 'name', required=True)),
            status=str(_text(data, 'status', required=True)),
            version=str(_text(data, 'version', required=True)),
            approved=_text(data, 'approved'),
            owner=_text(data, 'owner'),
            definition=_text(data, 'definition'),
            grain=_text(data, 'grain'),
            filters=_texts(data, 'filters'),
            columns=_texts(data, 'columns'),
            unit=_text(data, 'unit'),
            currency=_text(data, 'currency'),
            exceptions=_text(data, 'exceptions'),
            formula=_text(data, 'formula'),
        )


@dataclass
class Rows:
    """The conditions on one relation's rows that a pin relies on, and where they apply."""

    relation: str
    """The relation's warehouse name."""
    conditions: list[str]
    """Its joins, filters and grouping, composed down to source columns in canonical form."""
    side: str | None = None
    """``LEFT``, ``RIGHT`` or ``FULL`` when the pin reaches the relation through the side of an
    outer join that may find no row of it: its filters then decide which rows join."""
    before: list[str] = field(default_factory=list)
    """The window functions (``WINDOW``) and LIMITs (``LIMIT``) between the relation and the
    pinned column, nearest first: its conditions change what they see."""

    @property
    def effective(self) -> list[str]:
        """The conditions as they apply to the pinned column (see :func:`in_context`)."""
        if not self.side and not self.before:
            return self.conditions
        return [in_context(c, self.side, self.before) for c in self.conditions]

    @property
    def reference(self) -> str:
        return _reference(self.relation, self.side, self.before)


def _reference(name: str, side: str | None, before: list[str]) -> str:
    """How a pin names the relation ``name`` it relies on: ``LEFT JOIN main.cus BEFORE WINDOW``."""
    return (f'{side} JOIN ' if side else '') + name + (f' BEFORE {", ".join(before)}' if before else '')


@dataclass
class Pin:
    """One upstream column, pinned by what it means."""

    column: str
    """The column as ``relation.column`` with the relation's warehouse name: the lock's key."""
    id: str
    """The graph node, which finds the column again in a build from this project."""
    fingerprint: str
    """``sha256:`` and the 64 hex digits of a hash over the formula, conditions, sources and
    files, and, for a formula cut short, the expression of every column on its way."""
    formula: str | None = None
    """The formula composed down to source columns, with full relation names, in canonical form;
    None for a column read straight from a source."""
    rows: list[Rows] = field(default_factory=list)
    """The conditions of every relation on the formula's way and of the relations they join,
    filter or group on, relation by relation."""
    sources: list[str] = field(default_factory=list)
    """The source columns the formula reaches."""
    files: list[str] = field(default_factory=list)
    """For each relation on the way whose columns have no recorded expression (a seed, a Python
    model, SQL that could not be read), the relation and a checksum of the file that builds it."""
    type: str | None = None
    """The column's type, when the build knows it."""
    complete: bool = True
    """False when some column on the way, other than a seed's, has no recorded expression."""
    verified: str | None = None
    """``confirmed``, ``partly confirmed`` or ``not exercised`` once ``konstruct verify`` has run."""
    repository: str | None = None
    """The repository that builds the column, when it is one listed in ``repositories``."""
    used_by: list[str] = field(default_factory=list)
    """This project's columns and exposures that read the column directly."""
    terms: list[PinnedTerm] = field(default_factory=list)

    @property
    def conditions(self) -> list[str]:
        """The joins, filters and grouping that decide which rows feed the column, composed down
        to source columns the same way as the formula, each as it applies to the column."""
        return sorted({c for rows in self.rows for c in rows.effective})

    def to_dict(self, references: list[str] | None = None) -> dict[str, Any]:
        """The pin as data. With ``references``, as the lockfile stores it: the relations whose
        conditions it relies on, by their names in the file, rather than the conditions."""
        data: dict[str, Any] = {
            'column': self.column,
            'id': self.id,
            'repository': self.repository,
            'type': self.type,
            'fingerprint': self.fingerprint,
            'formula': self.formula,
            'conditions': None if references is not None else self.conditions,
            'relations': references if references is not None else [r.reference for r in self.rows],
            'sources': self.sources,
            'files': self.files,
            'complete': None if self.complete else False,
            'verified': self.verified,
            'used_by': self.used_by,
            'terms': [t.to_dict() for t in self.terms],
        }
        return {k: v for k, v in data.items() if v not in (None, [])}

    @classmethod
    def from_dict(cls, data: Any, relations: dict[str, list[str]]) -> Pin:
        """A pin as the lockfile stores it, with ``relations`` the file's relations by name."""
        if not isinstance(data, dict):
            raise Malformed('a pin must be a table')
        _known(data, [k for k in cls.__dataclass_fields__ if k != 'rows'] + ['relations'], 'a pin')
        fingerprint = str(_text(data, 'fingerprint', required=True))
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise Malformed("'fingerprint' must be sha256: and 64 hex digits")
        complete = data.get('complete', True)
        if not isinstance(complete, bool):
            raise Malformed("'complete' must be true or false")
        terms = data.get('terms', [])
        if not isinstance(terms, list):
            raise Malformed("'terms' must be a list of tables")
        return cls(
            column=str(_text(data, 'column', required=True)),
            id=str(_text(data, 'id', required=True)),
            fingerprint=fingerprint,
            formula=_text(data, 'formula'),
            rows=_sorted([_rows(reference, relations) for reference in _texts(data, 'relations')]),
            sources=_texts(data, 'sources'),
            files=_texts(data, 'files'),
            type=_text(data, 'type'),
            complete=complete,
            verified=_text(data, 'verified'),
            repository=_text(data, 'repository'),
            used_by=_texts(data, 'used_by'),
            terms=[PinnedTerm.from_dict(t) for t in terms],
        )


def _parts(reference: str) -> tuple[str, str, str]:
    """A pin's reference to a relation as (side, name in the file, steps)."""
    named, _, steps = reference.partition(' BEFORE ')
    side, sep, name = named.partition(' JOIN ')
    if not sep or side not in OUTER_SIDES:
        side, name = '', named
    return side, name, steps


def _rows(reference: str, relations: dict[str, list[str]]) -> Rows:
    side, name, steps = _parts(reference)
    if name not in relations:
        raise Malformed(f"'relations' names {name!r}, which no [[relation]] defines")
    before = [s for s in steps.split(', ') if s]
    if any(s not in ('WINDOW', 'LIMIT') for s in before):
        raise Malformed(f"'relations': {reference!r} names a step other than WINDOW or LIMIT")
    return Rows(_VERSION.sub('', name), relations[name], side or None, before)


def _sorted(rows: Iterable[Rows]) -> list[Rows]:
    return sorted(rows, key=lambda r: (r.relation, r.side or '', r.before, r.conditions))


@dataclass
class Lockfile:
    """The contents of ``konstruct.lock``."""

    pins: list[Pin] = field(default_factory=list)
    """Sorted by column."""
    reads: list[str] = field(default_factory=list)
    """What was locked: columns, relations, exposures or terms, as given to ``konstruct lock``;
    empty for the default, every upstream column this project's own models and exposures read."""
    format: int = FORMAT
    canonical: str = CANONICAL
    """The canonical form the pins were printed in (see :data:`CANONICAL`)."""
    _index: dict[str, Pin] = field(default_factory=dict, repr=False, compare=False)

    def pin(self, column: str) -> Pin | None:
        if len(self._index) != len(self.pins):
            self._index = {p.column: p for p in self.pins}
        return self._index.get(column)

    def stored(self) -> tuple[list[tuple[Pin, list[str]]], dict[str, list[str]]]:
        """The pins, sorted, each with the names of the relations it relies on, and those
        relations' conditions by name: once per relation, so pins that read the same relations
        share them. Pins kept as locked may rely on conditions a relation no longer has, which are
        stored beside the current ones as ``name~2`` and so on."""
        names: dict[tuple[str, tuple[str, ...]], str] = {}
        relations: dict[str, list[str]] = {}
        stored = []
        for pin in sorted(self.pins, key=lambda p: p.column):
            references = []
            for rows in pin.rows:
                key = (rows.relation, tuple(rows.conditions))
                if key not in names:
                    versions = sum(1 for relation, _ in names if relation == rows.relation)
                    names[key] = rows.relation + (f'~{versions + 1}' if versions else '')
                    relations[names[key]] = rows.conditions
                references.append(_reference(names[key], rows.side, rows.before))
            stored.append((pin, sorted(references)))
        return stored, relations

    def to_dict(self) -> dict[str, Any]:
        stored, relations = self.stored()
        return {
            'format': self.format,
            'canonical': self.canonical,
            'reads': self.reads,
            'pins': [{**p.to_dict(), 'relations': refs} for p, refs in stored],
            'relations': [{'name': n, 'conditions': c} for n, c in sorted(relations.items())],
        }

    def dumps(self) -> str:
        """The lockfile as TOML: keys in a fixed order, pins sorted, then the relations whose
        conditions they rely on, one list item per line."""
        lines = [
            _HEADER + f'format = {self.format}',
            f'canonical = {_toml_string(self.canonical)}',
            f'reads = {_toml_list(self.reads)}',
        ]
        stored, relations = self.stored()
        for pin, references in stored:
            lines += ['', '[[pin]]']
            data = pin.to_dict(references)
            terms = data.pop('terms', [])
            lines += [f'{key} = {_toml_value(value)}' for key, value in data.items()]
            for term in terms:
                lines += ['', '[[pin.terms]]', *(f'{k} = {_toml_value(v)}' for k, v in term.items())]
        for name, conditions in sorted(relations.items()):
            lines += ['', '[[relation]]', f'name = {_toml_string(name)}']
            lines.append(f'conditions = {_toml_list(conditions)}')
        return '\n'.join(lines) + '\n'

    @classmethod
    def loads(cls, text: str, where: str = FILE_NAME) -> Lockfile:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise LockError(f'{where} is not valid TOML: {error}') from None
        layout = data.get('format')
        if type(layout) is not int or layout != FORMAT:
            raise LockError(f'{where}: unsupported format {layout!r}; expected {FORMAT}')
        pins = data.get('pin', [])
        stored = data.get('relation', [])
        try:
            _known(data, ('format', 'canonical', 'reads', 'pin', 'relation'), 'the file')
            canonical = str(_text(data, 'canonical', required=True))
            reads = _texts(data, 'reads')
            if not isinstance(pins, list):
                raise Malformed("'pin' must be a list of tables")
            if not isinstance(stored, list) or not all(isinstance(r, dict) for r in stored):
                raise Malformed("'relation' must be a list of tables")
            relations: dict[str, list[str]] = {}
            for relation in stored:
                _known(relation, ('name', 'conditions'), 'a [[relation]]')
                name = str(_text(relation, 'name', required=True))
                if name in relations:
                    raise Malformed(f'[[relation]] {name!r} is given twice')
                relations[name] = _texts(relation, 'conditions')
        except Malformed as error:
            raise LockError(f'{where}: {error}') from None
        try:
            loaded = [Pin.from_dict(p, relations) for p in pins]
        except Malformed as error:
            raise LockError(f'{where}: a pin is malformed: {error}') from None
        twice = sorted(c for c, n in Counter(p.column for p in loaded).items() if n > 1)
        if twice:
            raise LockError(f'{where}: {twice[0]!r} is pinned twice')
        used = {_parts(str(r))[1] for p in pins for r in p.get('relations', [])}
        unused = sorted(set(relations) - used)
        if unused:
            raise LockError(f'{where}: [[relation]] {unused[0]!r} is not named by any pin')
        return cls(pins=sorted(loaded, key=lambda p: p.column), reads=reads, canonical=canonical)

    @classmethod
    def read(cls, path: Path) -> Lockfile:
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            raise LockError(f'no lockfile at {path}; run `konstruct lock` first') from None
        return cls.loads(text, path.name)

    def write(self, path: Path) -> None:
        path.write_text(self.dumps(), encoding='utf-8')


def optional_text(value: Any) -> str | None:
    """``value`` as text, or None when it is missing or empty."""
    return None if value in (None, '') else str(value)


def _toml_string(text: str) -> str:
    escaped = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\t': '\\t', '\r': '\\r'}
    out = []
    for char in text:
        if char in escaped:
            out.append(escaped[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f'\\u{ord(char):04X}')
        else:
            out.append(char)
    return '"' + ''.join(out) + '"'


def _toml_list(items: Iterable[str]) -> str:
    values = list(items)
    if not values:
        return '[]'
    return '[\n' + ''.join(f'  {_toml_string(v)},\n' for v in values) + ']'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return _toml_list(str(v) for v in value)
    return _toml_string(str(value))


# The canonical form


def _fingerprint(
    formula: str | None, conditions: list[str], sources: list[str], files: list[str], steps: list[str]
) -> str:
    data: dict[str, Any] = {'formula': formula, 'conditions': sorted(conditions), 'sources': sorted(sources)}
    if files:
        data['files'] = sorted(files)
    if steps:
        data['steps'] = sorted(steps)
    payload = json.dumps(data, sort_keys=True)
    return 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


# Each comparison, and the one that says the same with its operands the other way round.
_MIRRORED: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
}


def _canonical(tree: exp.Expr) -> exp.Expr:
    """``tree`` without the differences in how it was written that do not change what it means:
    the operands of a comparison in order (a constant, such as ``1`` or ``-1``, last, as it is
    usually written; otherwise by their text), with the
    comparison mirrored to match (``10 <= a`` is ``a >= 10``); the operands of ``AND`` and ``OR``
    flattened through brackets, in order and each once; the items of an ``IN`` list in order and
    each once; and decimals without trailing zeros (``1.10`` is ``1.1``; ``1.0`` stays a decimal).
    Rewritten in place: the result is ``tree`` or what replaced it."""
    # Reversed pre-order puts every node after its descendants, so each is ordered by the final
    # text of its operands.
    for node in reversed(list(tree.dfs())):
        mirrored = _MIRRORED.get(type(node))
        if mirrored is not None:
            left, right = node.this, node.expression
            if _order(right) < _order(left):
                tree = _replace(tree, node, mirrored(this=right, expression=left))
        elif isinstance(node, (exp.And, exp.Or)):
            if not isinstance(node.parent, type(node)):
                operands = {o.sql(): o for o in _operands(node)}
                combine = exp.and_ if isinstance(node, exp.And) else exp.or_
                tree = _replace(tree, node, combine(*(operands[k] for k in sorted(operands)), copy=False))
        elif isinstance(node, exp.In) and node.expressions:
            items = {e.sql(): e for e in node.expressions}
            node.set('expressions', [items[k] for k in sorted(items)])
        elif isinstance(node, exp.Literal) and not node.is_string:
            match = _DECIMAL.fullmatch(node.this)
            if match:
                node.set('this', f'{match[1]}.{match[2] or "0"}')
    return tree


def _operands(node: exp.Expr) -> list[exp.Expr]:
    """The operands of an ``AND`` (or an ``OR``), and of those it holds, in brackets or not."""
    found: list[exp.Expr] = []
    for operand in (node.this, node.expression):
        operand = operand.unnest()
        found += _operands(operand) if type(operand) is type(node) else [operand]
    return found


def _replace(tree: exp.Expr, node: exp.Expr, new: exp.Expr) -> exp.Expr:
    if node is tree:
        return new
    node.replace(new)
    return tree


def _order(operand: exp.Expr) -> tuple[bool, str]:
    """Constants (a literal, negated or not, NULL, TRUE, FALSE) sort last."""
    constant = operand.this if isinstance(operand, exp.Neg) else operand
    return isinstance(constant, (exp.Literal, exp.Null, exp.Boolean)), operand.sql()


def _label(label: str) -> str:
    """A condition's label without the words that do not change it: ``LEFT OUTER JOIN ON`` is
    ``LEFT JOIN ON``, ``INNER JOIN ON`` is ``JOIN ON``, and a RIGHT JOIN is the LEFT JOIN of its
    sides the other way round, which the relation's nullable relations say."""
    words = [w for w in label.split() if w not in ('OUTER', 'INNER')]
    return ' '.join(['LEFT', *words[1:]] if words[:1] == ['RIGHT'] else words)


def column_key(graph: Graph, node: Node) -> str:
    """``relation.column`` with the relation's warehouse name, or the node id without one."""
    relation = graph.parent_of(node)
    if relation is not None and relation.relation:
        return f'{relation.relation}.{node.name.lower()}'
    return node.id


def columns_named(graph: Graph, column: str) -> list[Node]:
    """Every column node for ``relation.column``, the preferred relation node's first."""
    parts = column.split('.')
    found: list[Node] = []
    for cut in range(len(parts) - 1, 0, -1):
        relation, name = '.'.join(parts[:cut]), '.'.join(parts[cut:])
        for owner in graph.nodes_for_relation(relation):
            node = graph.column(owner.id, name)
            if node is not None and node not in found:
                found.append(node)
    return found


def _relations_named(graph: Graph, column: str) -> list[Node]:
    parts = column.split('.')
    for cut in range(len(parts) - 1, 0, -1):
        found = graph.nodes_for_relation('.'.join(parts[:cut]))
        if found:
            return found
    return []


def _term_version(attrs: dict[str, Any], description: str | None) -> str:
    meaning = {k: attrs.get(k) for k in _MEANING if k != 'definition'}
    meaning['definition'] = description
    # A measure's formula (an Ossie metric's) is part of what it means; a term without one keeps
    # the version it had before formulas counted.
    if attrs.get('formula'):
        meaning['formula'] = str(attrs['formula'])
    payload = json.dumps(meaning, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]


def _unrecorded(graph: Graph, reader: Node, relation: Node) -> set[str] | None:
    """Whether ``reader`` reads columns of ``relation`` with no record of which, and if so the
    columns it leaves out: a ``SELECT *`` it could not expand passes on every column but those it
    excludes, and a reader none of whose columns derives from ``relation`` reads them all. A key
    it joins, filters or groups on says which rows it reads, not which columns."""
    excluded = _lookup(dict(reader.attrs.get('star_reads') or {}), relation.relation)
    if excluded is not None:
        return {str(e).lower() for e in excluded}
    derives = any(
        e.target in graph.nodes and graph.nodes[e.target].parent == relation.id
        for column in graph.columns_of(reader.id)
        for e in graph.out_edges(column.id, 'derived_from')
    )
    return None if derives else set()


def _star_columns(graph: Graph, reader: Node, relation: Node) -> list[Node]:
    """The columns of ``relation`` that ``reader`` reads with no record of which (see
    :func:`_unrecorded`)."""
    excluded = _unrecorded(graph, reader, relation)
    if excluded is None:
        return []
    return [c for c in graph.columns_of(relation.id) if c.name.lower() not in excluded]


_Place = TypeVar('_Place')


def _lookup(where: dict[str, _Place], key: str | None) -> _Place | None:
    """What ``where`` says of the relation ``key``, which it names as the SQL reading it wrote its
    name, in full or without its leading parts."""
    if not where or not key:
        return None
    if key in where:
        return where[key]
    for name, place in where.items():
        if key.endswith('.' + name) or name.endswith('.' + key):
            return place
    return None


class _Consumer:
    """Which nodes belong to the consumer: the project itself (``name`` None) or one of its
    ``repositories``, by the ``repository`` mark the build puts on each repository's nodes."""

    def __init__(self, graph: Graph, name: str | None) -> None:
        self.graph = graph
        self.name = name
        self.memo: dict[str, bool] = {}
        self.reading: dict[tuple[str, str], list[Node]] = {}

    def owns(self, node: Node) -> bool:
        return node.attrs.get('repository') == self.name

    def builds(self, relation: Node | None) -> bool:
        """Whether the consumer builds ``relation`` itself (a model, seed or SQL table of its own),
        rather than reading it from a source, the warehouse or another repository."""
        if relation is None:
            return False
        found = self.memo.get(relation.id)
        if found is None:
            found = self.memo[relation.id] = self.owns(relation) and self.graph.built(relation)
        return found

    def star_columns(self, reader: Node, relation: Node) -> list[Node]:
        """The columns of ``relation`` that ``reader``, which the consumer builds, reads with no
        record of which (see :func:`_star_columns`)."""
        key = (reader.id, relation.id)
        found = self.reading.get(key)
        if found is None:
            read = self.builds(reader) and relation.kind in RELATION_KINDS
            found = self.reading[key] = _star_columns(self.graph, reader, relation) if read else []
        return found

    def own_reader(self, node: Node) -> bool:
        """A column of a relation the consumer builds, or one of its exposures."""
        if node.kind == 'exposure':
            return self.owns(node)
        return node.kind == 'column' and self.builds(self.graph.parent_of(node))


def _read_by(graph: Graph, node: Node) -> list[Node]:
    """The columns an exposure reads: those it records, or every column of the relations it uses."""
    columns: list[Node] = []
    for edge in graph.in_edges(node.id, 'feeds'):
        source = graph.nodes.get(edge.source)
        if source is None:
            continue
        if source.kind == 'column':
            columns.append(source)
        elif source.kind in RELATION_KINDS and not graph.fed_by_columns_of(node.id, source.id):
            columns.extend(graph.columns_of(source.id))
    return columns


def expand_ref(graph: Graph, ref: str) -> list[Node]:
    """The columns ``ref`` names: a column, every column of a relation, the columns an exposure
    reads, or those a term defines."""
    node = resolve_one(graph, ref)
    if node.kind == 'column':
        return [node]
    if node.kind in RELATION_KINDS:
        return graph.columns_of(node.id)
    if node.kind == 'exposure':
        return _read_by(graph, node)
    if node.kind == 'term':
        found: list[Node] = []
        for edge in graph.out_edges(node.id, 'defines'):
            target = graph.nodes[edge.target]
            found.extend(graph.columns_of(target.id) if target.kind in RELATION_KINDS else [target])
        return found
    raise LockError(f'{ref!r} is a {node.kind}; lock columns, relations, exposures or terms')


def upstream_reads(graph: Graph, consumer: str | None = None) -> list[Node]:
    """Every upstream column the consumer reads: the columns its own models derive from, join,
    filter or group on, read with ``SELECT *``, and its exposures use, where the consumer does not
    build them itself."""
    who = _Consumer(graph, consumer)
    found: dict[str, Node] = {}

    def add(column: Node | None) -> None:
        if column is not None and column.kind == 'column' and not who.builds(graph.parent_of(column)):
            found.setdefault(column.id, column)

    for relation in list(graph.relations()):
        if not who.builds(relation):
            continue
        for column in graph.columns_of(relation.id):
            for edge in graph.out_edges(column.id, 'derived_from'):
                add(graph.nodes.get(edge.target))
        for edge in graph.in_edges(relation.id, 'feeds'):
            source = graph.nodes.get(edge.source)
            if source is None:
                continue
            if edge.attrs.get('role') == 'reference':
                add(source)
            else:
                for column in who.star_columns(relation, source):
                    add(column)
    for node in graph.nodes.values():
        if node.kind == 'exposure' and who.owns(node):
            for column in _read_by(graph, node):
                add(column)
    return sorted(found.values(), key=lambda n: column_key(graph, n))


def selection(graph: Graph, reads: list[str], consumer: str | None = None) -> list[Node]:
    """The columns ``reads`` names, or with none, :func:`upstream_reads`."""
    if not reads:
        return upstream_reads(graph, consumer)
    found: dict[str, Node] = {}
    for ref in reads:
        for column in expand_ref(graph, ref):
            found.setdefault(column.id, column)
    return sorted(found.values(), key=lambda n: column_key(graph, n))


def _boundary(graph: Graph, lockfile: Lockfile) -> set[str]:
    """The columns the pins reached as sources, wherever this build has them."""
    return {
        node.id for pin in lockfile.pins for source in pin.sources for node in columns_named(graph, source)
    }


class Pinner:
    """Pins for the columns of one graph, composing each formula and condition once.

    ``boundary`` names columns composed as sources whatever their formula: run in a producer,
    the columns the consumer's own build reached as sources, so a pin is composed as far as the
    consumer saw it and no further."""

    def __init__(self, graph: Graph, root: Path, consumer: str | None, boundary: Iterable[str] = ()) -> None:
        self.graph = graph
        self.root = root
        self.consumer = _Consumer(graph, consumer)
        self.explainer = Explainer(graph, qualified=True, boundary=boundary)
        self.namer = self.explainer.namer
        self.boundary = self.explainer.boundary
        self.conditions: dict[tuple[str, str], tuple[list[str], list[Node]]] = {}
        self.inputs: dict[str, list[Node]] = {}
        self.checksums: dict[str, str | None] = {}
        self.canonical_texts: dict[str, str] = {}
        self.built: dict[str, bool] = {}
        self.next: dict[str, tuple[str | None, list[tuple[Node, str | None, tuple[str, ...]]]]] = {}
        self.rows: dict[str, Rows] = {}
        """Each relation's conditions, composed."""
        self.read: dict[str, list[Node]] = {}
        """The columns each relation's conditions read."""
        self.nullable: dict[str, dict[str, str]] = {}
        self.before: dict[str, dict[str, list[str]]] = {}
        """For each relation, those it reads before a window function or LIMIT, with those steps."""
        """For each relation, those it reads on the nullable side of an outer join, by relation
        key, with that join's side."""

    def builds(self, relation: Node) -> bool:
        """Whether something in the graph builds ``relation``, worked out once per relation."""
        found = self.built.get(relation.id)
        if found is None:
            found = self.built[relation.id] = self.graph.built(relation)
        return found

    def defining(self, node: Node) -> Node:
        """The column whose formula ``node`` holds: itself, or for a column of a relation another
        node builds (a dbt source, a table another repository's model writes), the builder's."""
        seen = {node.id}
        while 'expression' not in node.attrs and node.id not in self.boundary:
            same = [
                self.graph.nodes[e.target]
                for e in self.graph.out_edges(node.id, 'derived_from')
                if e.attrs.get('same_relation') and e.target in self.graph.nodes
            ]
            passed = self.passed(node)
            following = same[0] if len(same) == 1 else passed
            if following is None or following.id in seen:
                break
            node = following
            seen.add(node.id)
        return node

    def passed(self, column: Node) -> Node | None:
        """For a column with no expression that its relation passes on through a ``SELECT *`` it
        could not expand, the column of the same name of the relation the star reads."""
        if 'expression' in column.attrs:
            return None
        relation = self.graph.parent_of(column)
        stars = dict(relation.attrs.get('star_reads') or {}) if relation is not None else {}
        if not stars or relation is None:
            return None
        for read in self.reads(relation):
            excluded = _lookup(stars, read.relation)
            if excluded is not None and column.name.lower() not in {str(e).lower() for e in excluded}:
                found = self.graph.column(read.id, column.name)
                if found is not None:
                    return found
        return None

    def builder(self, relation: Node | None) -> Node | None:
        """The relation node whose definition ``relation`` has: itself when something in the graph
        builds it, else the model it is the same relation as."""
        if relation is None:
            return None
        if self.builds(relation):
            return relation
        for edge in self.graph.in_edges(relation.id, 'feeds'):
            if edge.attrs.get('same_relation') and edge.source in self.graph.nodes:
                return self.graph.nodes[edge.source]
        return None

    def missing(self, column: Node) -> str | None:
        """For a column the graph has only because something reads it, on a relation something
        here builds without it (a column the producer renamed or dropped): who builds the
        relation. None when the column is built, or when what builds it is not known."""
        graph = self.graph
        defining = self.defining(column)
        if 'expression' in defining.attrs or composed_from(graph, defining) or defining.id in self.boundary:
            return None
        relation = graph.parent_of(defining)
        builder = self.builder(relation)
        if builder is None or relation is None:
            return None
        if builder is relation:
            if not defining.attrs.get('inferred'):
                return None
        elif graph.column(builder.id, defining.name) is not None:
            return None
        # A builder that reads a relation with no record of which columns (SELECT * over columns
        # its SQL did not know) may well build the column under that star.
        for edge in graph.in_edges(builder.id, 'feeds'):
            source = graph.nodes.get(edge.source)
            if (
                source is not None
                and source.kind in RELATION_KINDS
                and not edge.attrs.get('same_relation')
                and _unrecorded(graph, builder, source) is not None
            ):
                return None
        return optional_text(builder.attrs.get('repository')) or 'this project'

    def judged(self, node: Node | None, column: str) -> bool:
        """Run in a producer: whether this build builds the pinned column (or the relation it
        was on), rather than only the consumer's build seeing it."""
        if node is None:
            relations = _relations_named(self.graph, column)
        else:
            relations = [r for r in [self.graph.parent_of(self.defining(node))] if r is not None]
        for relation in relations:
            builder = self.builder(relation)
            if builder is not None and builder.attrs.get('repository') != self.consumer.name:
                return True
        return False

    def reads(self, relation: Node) -> list[Node]:
        """The relations ``relation`` is built from."""
        found = self.inputs.get(relation.id)
        if found is None:
            found = self.inputs[relation.id] = [
                self.graph.nodes[e.source]
                for e in self.graph.in_edges(relation.id, 'feeds')
                if e.source in self.graph.nodes and self.graph.nodes[e.source].kind in RELATION_KINDS
            ]
        return found

    def condition(self, owner: Node, text: str) -> tuple[list[str], list[Node]]:
        """One of ``owner``'s conditions with every column in it replaced by its formula composed
        down to source, in canonical form and split into its conjuncts (a GROUP BY into its
        keys), and the columns it reads."""
        key = (owner.id, text)
        found = self.conditions.get(key)
        if found is None:
            found = self.conditions[key] = self._condition(owner, text)
        return found

    def _condition(self, owner: Node, text: str) -> tuple[list[str], list[Node]]:
        read_terms = condition_terms(text, self.namer.parse)
        prefix = _label(read_terms.label)
        base = prefix.partition(' BEFORE ')[0]
        clause = read_terms.clause
        if read_terms.terms is None:
            return [f'{prefix}: {read_terms.rest}'], []
        parts = read_terms.terms
        if clause == 'ORDER BY' and parts:
            parts = [exp.Tuple(expressions=list(parts))]
        if not parts:
            return [text], []
        # The conjuncts of an outer join's ON stay together: moved to another join's ON, one
        # decides other rows.
        split = not (clause == 'ON' and base.split()[0] in OUTER_SIDES)
        read: list[Node] = []
        found: list[str] = []
        for part in parts:
            part.parent = None
            composed = self.explainer.compose_sql(self.qualify(owner, part), read.append)
            if isinstance(composed, exp.Tuple):
                printed = ', '.join(e.sql(dialect=self.namer.sqlglot) for e in composed.expressions)
                found.append(f'{prefix}: {printed}')
                continue
            composed = _canonical(composed).unnest()
            conjuncts = _operands(composed) if split and isinstance(composed, exp.And) else [composed]
            for conjunct in conjuncts:
                printed = conjunct.unnest().sql(dialect=self.namer.sqlglot)
                found.append(f'{prefix}: {printed}' if prefix else printed)
        return found, read

    def qualify(self, owner: Node, tree: exp.Expr) -> exp.Expr:
        """``tree`` with each bare column name qualified by the relation ``owner`` reads it from,
        when only that relation can have it: the one relation ``owner`` reads, or the one that has
        a column of that name where every other has known columns without it. The build records a
        condition over relations whose columns it did not know without their names, which would
        leave it out of the composition. A name another relation has too, or may have, stays bare:
        it is composed as it is, not as the wrong relation's."""
        reads = [r for r in self.reads(owner) if r.relation]

        def qualified(item: exp.Expr) -> exp.Expr:
            if not isinstance(item, exp.Column) or item.table:
                return item
            having = [r for r in reads if self.graph.column(r.id, item.name) is not None]
            if len(reads) == 1:
                having = reads
            elif len(having) != 1 or any(r is not having[0] and not self.known(r) for r in reads):
                return item
            return self.namer.column(item.name, having[0])

        return tree.transform(qualified, copy=False) if reads else tree

    def known(self, relation: Node) -> bool:
        """Whether every column of ``relation`` is in the graph, as the build decided when it
        analysed the SQL that reads it (see :meth:`Graph.guessed_relations`)."""
        return bool(self.graph.columns_of(relation.id)) and not relation.attrs.get('columns_guessed')

    def walk(self, column: Node) -> tuple[list[Node], list[Rows]]:
        """Every column the pin of ``column`` depends on, nearest first, and the composed conditions
        of their relations: the columns its formula is composed from, and those the joins,
        filters and grouping of their relations read, as they decide which rows feed it. A
        relation reached through the nullable side of an outer join, and every relation upstream
        of it on the way, is under that join: its conditions decide which rows join, not which
        rows the column has."""
        start: tuple[str, str | None, tuple[str, ...]] = (column.id, None, ())
        seen = {start}
        queue: deque[tuple[Node, str | None, tuple[str, ...]]] = deque([(column, None, ())])
        steps: dict[str, Node] = {}
        rows: dict[tuple[str, str | None, tuple[str, ...]], Rows] = {}
        while queue:
            node, side, before = queue.popleft()
            # A relation is reached as a whole when its rows matter but no column of it does.
            for step in [node] if node.kind == 'column' else self.graph.columns_of(node.id):
                steps.setdefault(step.id, step)
            if node.id in self.boundary:
                continue
            owner, following = self.following(node)
            if owner is not None and self.rows[owner].conditions and (owner, side, before) not in rows:
                own = self.rows[owner]
                rows[owner, side, before] = Rows(own.relation, own.conditions, side, list(before))
            for other, joined, earlier in following:
                # The other column's relation comes first, then what this relation does to it.
                reached = (other.id, side or joined, (*earlier, *before))
                if reached not in seen:
                    seen.add(reached)
                    queue.append((other, reached[1], reached[2]))
        return list(steps.values()), _sorted(rows.values())

    def following(self, node: Node) -> tuple[str | None, list[tuple[Node, str | None, tuple[str, ...]]]]:
        """For :meth:`walk`, worked out once per column or relation: its relation, and what comes
        next, each with the side of the outer join its relation is on the nullable side of, if
        any, and the window functions and LIMITs the relation applies to it. After a column, the
        columns its formula is composed from or a ``SELECT *`` passes on, and those its relation's
        conditions read; after a relation, and for every relation its relation reads that none of
        those columns is of (one it reads in a subquery, cross joins or joins on TRUE), that
        relation as a whole, as its rows decide the relation's."""
        found = self.next.get(node.id)
        if found is None:
            graph = self.graph
            owner = graph.parent_of(node) if node.kind == 'column' else node
            columns: list[Node] = []
            if node.kind == 'column':
                passed = self.passed(node)
                columns = composed_from(graph, node) + ([passed] if passed is not None else [])
            following: list[tuple[Node, str | None, tuple[str, ...]]] = []
            if owner is not None:
                if owner.id not in self.rows:
                    self.compose_rows(owner)
                columns += self.read[owner.id]
                nullable, before = self.nullable[owner.id], self.before[owner.id]
                columns = [c for c in columns if c.kind == 'column']
                covered = {c.parent for c in columns}
                targets = columns + [r for r in self.reads(owner) if r.id not in covered]
                for target in targets:
                    key = graph.parent_of(target) if target.kind == 'column' else target
                    name = key.relation if key is not None else None
                    steps: list[str] = _lookup(before, name) or []
                    following.append((target, _lookup(nullable, name), tuple(steps)))
            found = self.next[node.id] = (owner.id if owner is not None else None, following)
        return found

    def compose_rows(self, owner: Node) -> None:
        """Compose ``owner``'s conditions, and note the columns they read and the relations it
        reads on the nullable side of an outer join."""
        graph = self.graph
        composed: set[str] = set()
        read: list[Node] = []
        nullable: dict[str, str] = {}
        for relation, side in dict(owner.attrs.get('nullable') or {}).items():
            if side in OUTER_SIDES:
                # ``b RIGHT JOIN a`` is ``a LEFT JOIN b``. Which relation is the nullable one is
                # part of the relation's rows: ``o LEFT JOIN p`` is not ``p LEFT JOIN o``.
                nullable[str(relation).lower()] = 'LEFT' if side == 'RIGHT' else side
                composed.add(f'{nullable[str(relation).lower()]} JOIN: {str(relation).lower()}')
        before: dict[str, list[str]] = {}
        for relation, steps in dict(owner.attrs.get('read_before') or {}).items():
            before[str(relation).lower()] = [str(s) for s in steps if s in ('WINDOW', 'LIMIT')]
        if owner.attrs.get('materialized') == 'incremental':
            # A run adds to the rows earlier runs kept, by this key and strategy.
            composed.add('MATERIALIZED: incremental')
            for key, label in (
                ('unique_key', 'UNIQUE KEY'),
                ('incremental_strategy', 'INCREMENTAL STRATEGY'),
            ):
                value = owner.attrs.get(key)
                if value:
                    composed.add(f'{label}: {", ".join(value) if isinstance(value, list) else value}')
        for text in map(str, owner.attrs.get('conditions', [])):
            texts, columns_read = self.condition(owner, text)
            composed.update(texts)
            read += columns_read
        read += [
            graph.nodes[e.source]
            for e in graph.in_edges(owner.id, 'feeds')
            if e.attrs.get('role') == 'reference' and e.source in graph.nodes
        ]
        self.rows[owner.id] = Rows(owner.relation or owner.id, sorted(composed))
        self.read[owner.id] = read
        self.nullable[owner.id] = nullable
        self.before[owner.id] = before

    def canonical_text(self, text: str) -> str:
        """``text``, a formula as the Explainer printed it, in canonical form."""
        found = self.canonical_texts.get(text)
        if found is None:
            tree = self.namer.parse(text) if _NORMALISED.search(text) else None
            found = _canonical(tree).sql(dialect=self.namer.sqlglot) if tree is not None else text
            self.canonical_texts[text] = found
        return found

    def opaque(self, step: Node) -> str | None:
        """For a column with no expression in a relation something builds (a seed, a Python
        model, SQL that could not be read), the relation and a checksum of the file that builds
        it: a seed's rows, or code whose columns have no formula to compare."""
        if 'expression' in step.attrs or step.id in self.boundary or self.passed(step) is not None:
            return None
        relation = self.graph.parent_of(step)
        if relation is None or not self.builds(relation):
            return None
        if relation.id not in self.checksums:
            found = defining_file(self.graph, relation, [])
            checksum = relation.attrs.get('checksum')
            # dbt records a large seed's checksum as its path, which no edit changes.
            if not (isinstance(checksum, str) and _SHA256.fullmatch(checksum)):
                checksum = None
            if checksum is None and found is not None:
                try:
                    checksum = hashlib.sha256((self.root / found[0]).read_bytes()).hexdigest()
                except OSError:
                    checksum = None
            self.checksums[relation.id] = f'sha256:{checksum}' if checksum else None
        return f'{relation.relation or relation.id} {self.checksums[relation.id] or "unknown"}'

    def pin(self, node: Node) -> Pin:
        graph = self.graph
        defining = self.defining(node)
        composed = self.explainer.compose_text(defining)
        formula = (
            self.canonical_text(composed.text)
            if 'expression' in defining.attrs and composed.text is not None
            else None
        )
        # From the column itself: a column a SELECT * passes on has its own relation's conditions.
        steps, rows = self.walk(node)
        states: list[str] = []
        for step in steps:
            if 'expression' in step.attrs:
                states += [str(e.attrs.get('verified', '')) for e in graph.out_edges(step.id, 'derived_from')]
        sources = sorted(column_key(graph, graph.nodes[s]) for s in composed.sources)
        if 'expression' not in defining.attrs:
            sources = [column_key(graph, defining)]
        opaque = [(s, self.opaque(s)) for s in steps]
        files = sorted({f for _, f in opaque if f})
        # A seed's columns are its rows, not a formula cut short.
        gap = any(f and getattr(graph.parent_of(s), 'kind', None) != 'seed' for s, f in opaque)
        # A formula cut short (too long to compose in full, or with a gap) does not show every
        # column on its way, so each one's own expression counts too.
        expressions = (
            []
            if composed.complete
            else [
                f'{column_key(graph, s)} = {self.canonical_text(self.namer.text(str(s.attrs["expression"])))}'
                for s in steps
                if 'expression' in s.attrs
            ]
        )
        conditions = sorted({c for r in rows for c in r.effective})
        relation = graph.parent_of(defining)
        return Pin(
            column=column_key(graph, node),
            id=node.id,
            fingerprint=_fingerprint(formula, conditions, sources, files, expressions),
            formula=formula,
            rows=rows,
            sources=sources,
            files=files,
            type=_type(node.data_type or defining.data_type),
            complete=composed.complete and not gap,
            verified=_verified(states),
            repository=optional_text(relation.attrs.get('repository') if relation else None),
            used_by=self.used_by(node),
            terms=self.terms(node, defining),
        )

    def terms(self, *columns: Node) -> list[PinnedTerm]:
        found: dict[str, PinnedTerm] = {}
        for column in columns:
            for edge in self.graph.in_edges(column.id, 'defines'):
                term = self.graph.nodes.get(edge.source)
                if term is None or term.kind != 'term' or edge.attrs.get('role') == 'input':
                    continue
                if term.name not in found:
                    found[term.name] = pinned_term(term)
        return [found[name] for name in sorted(found)]

    def used_by(self, node: Node) -> list[str]:
        """The consumer's own columns and exposures that read ``node`` directly."""
        graph = self.graph
        readers: set[str] = set()
        for edge in graph.in_edges(node.id, 'derived_from'):
            reader = graph.nodes.get(edge.source)
            if reader is not None and self.consumer.own_reader(reader):
                readers.add(graph.label(reader))
        for edge in graph.out_edges(node.id, 'feeds'):
            target = graph.nodes.get(edge.target)
            if target is None:
                continue
            if target.kind == 'exposure' and self.consumer.owns(target):
                readers.add(f'{graph.label(target)} (exposure)')
            elif edge.attrs.get('role') == 'reference' and self.consumer.builds(target):
                readers.add(f'{graph.label(target)} (joins, filters or groups on it)')
        relation = graph.parent_of(node)
        if relation is not None:
            for edge in graph.out_edges(relation.id, 'feeds'):
                target = graph.nodes.get(edge.target)
                if target is None:
                    continue
                if (
                    target.kind == 'exposure'
                    and self.consumer.owns(target)
                    and not graph.fed_by_columns_of(target.id, relation.id)
                ):
                    readers.add(f'{graph.label(target)} (exposure)')
                elif node in self.consumer.star_columns(target, relation):
                    readers.add(f'{graph.label(target)} (reads every column)')
        return sorted(readers)

    def affected(self, node: Node) -> list[str]:
        """The consumer's own columns and exposures downstream of ``node``."""
        graph = self.graph
        # An exposure that reads the column's own relation is a direct reader, not in the walk.
        found = set(self.used_by(node))
        for step in graph.impact(node):
            if step.depth and self.consumer.own_reader(step.node):
                suffix = ' (exposure)' if step.node.kind == 'exposure' else ''
                found.add(graph.label(step.node) + suffix)
        return sorted(found)

    def compare(
        self, old: Pin, node: Node | None, display: Any, notes: list[str], *, when: str = 'locked'
    ) -> tuple[PinChange | None, Pin | None]:
        """What fails in ``old`` against this build, if anything, and the pin as it is now.
        ``display`` names columns for people (an unqualified :class:`Explainer`'s namer), and
        ``when`` says in the notes when ``old`` was made (``locked``, or a receipt's ``issued``)."""
        graph = self.graph

        def short(text: str | None, condition: bool = False) -> str | None:
            if text is None:
                return None
            return str(display.condition(text) if condition else display.text(text))

        def pair(before: str | None, after: str | None) -> tuple[str | None, str | None]:
            # Two relations can share a short name (raw.orders and main.orders): then the full
            # names say what changed.
            shown = short(before), short(after)
            return (before, after) if shown[0] == shown[1] else shown

        def lists(
            added: list[str], removed: list[str], condition: bool = False
        ) -> tuple[list[str], list[str]]:
            shown_added = [str(short(c, condition)) for c in added]
            shown_removed = [str(short(c, condition)) for c in removed]
            if set(shown_added) & set(shown_removed):
                return added, removed
            return shown_added, shown_removed

        gone = self.missing(node) if node is not None else None
        if node is None or gone is not None:
            return (
                PinChange(
                    column=old.column,
                    label=graph.label(node) if node is not None else old.column,
                    reasons=['removed'],
                    repository=old.repository,
                    removed=f'no longer built by {gone}' if gone else 'no such column in this build',
                    formula_before=short(old.formula),
                ),
                None,
            )
        new = self.pin(node)
        defining = self.defining(node)
        relation = graph.parent_of(defining)
        change = PinChange(
            column=old.column,
            label=graph.label(node),
            reasons=[],
            repository=new.repository,
            path=relation.path if relation is not None else None,
        )
        # Every stored part is compared, not only the fingerprint, which the producer controls.
        locked = (old.fingerprint, old.formula, old.conditions, old.sources, old.files)
        if locked != (new.fingerprint, new.formula, new.conditions, new.sources, new.files):
            if old.formula is not None and new.formula is None and 'expression' not in defining.attrs:
                change.reasons.append('unknown')
                change.formula_before = short(old.formula)
            else:
                if old.formula != new.formula:
                    change.reasons.append('formula')
                    change.formula_before, change.formula_after = pair(old.formula, new.formula)
                if old.conditions != new.conditions:
                    change.reasons.append('conditions')
                    change.conditions_added, change.conditions_removed = lists(
                        [c for c in new.conditions if c not in old.conditions],
                        [c for c in old.conditions if c not in new.conditions],
                        condition=True,
                    )
                if old.sources != new.sources:
                    change.reasons.append('sources')
                    change.sources_added, change.sources_removed = lists(
                        [s for s in new.sources if s not in old.sources],
                        [s for s in old.sources if s not in new.sources],
                    )
                if old.files != new.files:
                    change.reasons.append('files')
                    change.files = sorted({f.rsplit(' ', 1)[0] for f in set(old.files) ^ set(new.files)})
                if not change.reasons:
                    change.reasons.append('upstream')
                    change.formula_before = short(old.formula)
        if old.type and new.type and old.type != new.type:
            change.reasons.append('type')
            change.type_before, change.type_after = old.type, new.type
        elif old.type and not new.type:
            notes.append(
                f'{change.label}: its type ({old.type}) is not known in this build, so it was not compared'
            )
        change.terms = term_changes(old.terms, new.terms, change.label, notes, when)
        if change.terms:
            change.reasons.append('definition')
        if old.verified == 'confirmed' and new.verified != 'confirmed':
            notes.append(
                f'{change.label}: was verified when {when}, and is {new.verified or "not verified"} now'
            )
        return (change if change.reasons else None), new


def pinned_term(term: Node) -> PinnedTerm:
    """A business term node as a pin records it: its status, owner, approval and meaning."""
    attrs = term.attrs
    owner = attrs.get('owner')
    return PinnedTerm(
        name=term.name,
        status=str(attrs.get('status', 'draft')),
        version=_term_version(attrs, term.description),
        approved=optional_text(attrs.get('approved')),
        owner=optional_text(owner.get('name')) if isinstance(owner, dict) else None,
        definition=term.description,
        grain=optional_text(attrs.get('grain')),
        filters=[str(f) for f in attrs.get('filters', [])],
        columns=[str(c) for c in attrs.get('columns', [])],
        unit=optional_text(attrs.get('unit')),
        currency=optional_text(attrs.get('currency')),
        exceptions=optional_text(attrs.get('exceptions')),
        formula=optional_text(attrs.get('formula')),
    )


def _type(value: str | None) -> str | None:
    return ' '.join(value.upper().split()) if value else None


def _verified(states: list[str]) -> str | None:
    if not states or not any(states):
        return None
    if all(s == 'confirmed' for s in states):
        return 'confirmed'
    if any(s == 'confirmed' for s in states):
        return 'partly confirmed'
    return 'not exercised'


def find(graph: Graph, column: str, node_id: str = '') -> Node | None:
    """The column a pin names: the node it was locked on, when this build has it under the same
    warehouse name; else by its warehouse name, so a build from another repository finds it too;
    else by its node id."""
    node = graph.nodes.get(node_id) if node_id else None
    if node is not None and node.kind == 'column' and column_key(graph, node) == column:
        return node
    named = columns_named(graph, column)
    if named:
        return named[0]
    node = graph.nodes.get(node_id or column)
    return node if node is not None and node.kind == 'column' else None


def display_namer(graph: Graph) -> Any:
    """What names columns for people in ``graph``: an unqualified :class:`Explainer`'s namer."""
    return Explainer(graph).namer


# konstruct lock


@dataclass
class LockResult:
    """What ``konstruct lock`` wrote."""

    path: str
    """The lockfile, relative to the project."""
    lockfile: Lockfile
    added: list[str] = field(default_factory=list)
    """Columns pinned for the first time."""
    removed: list[str] = field(default_factory=list)
    """Columns no longer read, or whose removal was accepted, so no longer pinned."""
    updated: list[str] = field(default_factory=list)
    """Pinned columns whose change was accepted."""
    kept: list[str] = field(default_factory=list)
    """Pinned columns left as they were locked, or brought up to date where nothing that fails a
    check changed (an approval date, a verification, a term that was not approved)."""
    stale: list[str] = field(default_factory=list)
    """Kept columns that fail ``konstruct lock --check`` against this build."""
    missing: list[str] = field(default_factory=list)
    """Columns this project reads that the relation's builder no longer builds."""
    written: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': self.path,
            'pins': len(self.lockfile.pins),
            'added': self.added,
            'removed': self.removed,
            'updated': self.updated,
            'kept': self.kept,
            'stale': self.stale,
            'missing': self.missing,
            'written': self.written,
            'lockfile': self.lockfile.to_dict(),
        }

    def __str__(self) -> str:
        count = len(self.lockfile.pins)
        verb = 'Wrote' if self.written else 'Would write'
        parts = [
            f'{len(items)} {word}'
            for items, word in (
                (self.added, 'added'),
                (self.updated, 'accepted'),
                (self.removed, 'removed'),
                (self.kept, 'unchanged'),
            )
            if items
        ]
        lines = [f'{verb} {self.path}: {plural(count, "upstream column")} pinned ({", ".join(parts)}).']
        lines += [f'  + {c}' for c in self.added]
        lines += [f'  ~ {c} (change accepted)' for c in self.updated]
        lines += [f'  - {c}' for c in self.removed]
        lines += [f'  ! {c} is read, but no longer built where it was' for c in self.missing]
        if self.stale:
            one = len(self.stale) == 1
            lines.append(
                f'{plural(len(self.stale), "pinned column")} {"differs" if one else "differ"} from this '
                f'build and {"was" if one else "were"} kept as locked; `konstruct lock --check` shows '
                'what changed, `konstruct lock --update` accepts it.'
            )
        return '\n'.join(lines)


def lock(
    graph: Graph,
    root: Path,
    reads: list[str] | None = None,
    *,
    update: bool | list[str] = False,
    path: Path | None = None,
    write: bool = True,
) -> LockResult:
    """Pin the upstream columns this project reads in ``konstruct.lock``.

    ``reads`` names what to pin (columns, relations, exposures or terms); None keeps what the
    lockfile already names, and an empty list (or no lockfile) pins every upstream column the
    project's own models and exposures read. Columns already pinned keep their pin, as a package
    lockfile keeps its versions, unless ``update`` is True (accept every change) or names them;
    what changed without failing a check is brought up to date."""
    target = path or Path(root) / FILE_NAME
    existing = Lockfile.read(target) if target.is_file() else None
    chosen = list(reads) if reads is not None else (existing.reads if existing else [])
    accept: set[str] = set()
    if isinstance(update, list):
        if existing is None:
            raise LockError(f'no lockfile at {target}; run `konstruct lock` first')
        for ref in update:
            accept.add(_pinned_key(graph, existing, ref))
    columns = selection(graph, chosen)
    if not columns and chosen:
        raise LockError(f'nothing to lock: {", ".join(chosen)} names no columns')
    if not columns:
        raise LockError(
            'nothing to lock: this project builds every relation its models read, or reads none whose '
            'columns are known. Name what it relies on, such as `konstruct lock customers` or '
            '`konstruct lock weekly_dashboard`'
        )
    pinner = Pinner(graph, Path(root), None)
    display = display_namer(graph)
    result = LockResult(path=relative_path(target, root), lockfile=Lockfile(reads=chosen))
    printed: list[str] = []
    for node in columns:
        key = column_key(graph, node)
        old = existing.pin(key) if existing else None
        gone = pinner.missing(node) is not None
        if gone:
            result.missing.append(key)
        if old is None:
            if not gone:
                result.added.append(key)
                result.lockfile.pins.append(pinner.pin(node))
            continue
        change, fresh = pinner.compare(old, node, display, [])
        if update is True or key in accept:
            if fresh is not None:
                (result.updated if change is not None else result.kept).append(key)
                result.lockfile.pins.append(fresh)
        elif change is not None:
            result.stale.append(key)
            if set(change.reasons) & set(_PRINTED):
                printed.append(key)
            # What reads the column is this project's own code, so it is always current.
            old.id, old.used_by = node.id, pinner.used_by(node)
            result.kept.append(key)
            result.lockfile.pins.append(old)
        elif fresh is not None:
            # Nothing that fails a check changed; what did (an approval date, a verification, a
            # term that was not approved) is brought up to date, except a type this build lacks.
            fresh.type = fresh.type or old.type
            result.kept.append(key)
            result.lockfile.pins.append(fresh)
    # Only formulas and conditions are printed in the canonical form: a pin kept for its type,
    # or for a column no longer built, reads the same in either.
    if existing is not None and existing.canonical != CANONICAL and printed:
        raise LockError(
            f'{result.path} was written with canonical form {existing.canonical}, which this Konstruct '
            f'does not write ({CANONICAL}), and {plural(len(printed), "pin")} read differently: '
            're-lock after review, with `konstruct lock --update`'
        )
    kept = {p.column for p in result.lockfile.pins}
    result.removed = sorted(p.column for p in (existing.pins if existing else []) if p.column not in kept)
    result.lockfile.pins.sort(key=lambda p: p.column)
    result.written = write
    if write:
        result.lockfile.write(target)
    return result


def _pinned_key(graph: Graph, existing: Lockfile, ref: str) -> str:
    if existing.pin(ref) is not None:
        return ref
    try:
        node = resolve_one(graph, ref)
    except QueryError:
        node = None
    if node is not None and node.kind == 'column':
        key = column_key(graph, node)
        if existing.pin(key) is not None:
            return key
    raise LockError(f'{ref!r} is not pinned in {FILE_NAME}')


def relative_path(path: Path, root: Path) -> str:
    """``path`` relative to ``root``, with forward slashes, or as it is on another drive."""
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:
        return str(path)


def plural(count: int, word: str) -> str:
    """``3 pins``, ``1 pin``: a count and a word that takes an s in the plural."""
    return f'{count} {word}{"" if count == 1 else "s"}'


# konstruct lock --check


@dataclass
class Attribution:
    """A commit (or uncommitted edit) that changed something a pin relies on."""

    what: str
    """What it changed: a column's formula, a file, or a term."""
    sha: str
    """Empty for uncommitted changes."""
    author: str
    date: str
    subject: str
    path: str | None = None
    """The file changed, relative to the project."""
    repository: str | None = None
    """The git repository it is in, relative to the project (``.`` for the project's own)."""
    before: str | None = None
    after: str | None = None

    @property
    def short(self) -> str:
        return self.sha[:7] if self.sha else 'working tree'

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, '')}

    def __str__(self) -> str:
        if not self.sha:
            return f'uncommitted changes to {self.path or self.what}'
        return f'{self.author} in {self.short} on {self.date[:10]}: {self.subject}'


@dataclass
class PinChange:
    """One pinned column that is not what was locked."""

    column: str
    """The pin's key."""
    label: str
    """The column's short name in this build."""
    reasons: list[str]
    """What changed: ``removed``, ``unknown`` (the formula could not be worked out now),
    ``formula``, ``conditions``, ``sources``, ``files`` (a file that builds a column with no
    recorded expression), ``upstream`` (a column further up than a formula cut short shows),
    ``type`` or ``definition``."""
    repository: str | None = None
    path: str | None = None
    """The file that builds the column, relative to the project."""
    removed: str | None = None
    """For a removed column, why: ``no such column in this build`` or ``no longer built by ...``."""
    formula_before: str | None = None
    """The locked formula, composed down to source, with short names."""
    formula_after: str | None = None
    """The formula now."""
    conditions_added: list[str] = field(default_factory=list)
    conditions_removed: list[str] = field(default_factory=list)
    sources_added: list[str] = field(default_factory=list)
    sources_removed: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    """Relations whose file changed, for columns with no recorded expression."""
    type_before: str | None = None
    type_after: str | None = None
    terms: list[str] = field(default_factory=list)
    """One line per change to a bound approved term."""
    changed_by: list[Attribution] = field(default_factory=list)
    affected: list[str] = field(default_factory=list)
    """This project's columns and exposures that read the column, directly or further down."""

    @property
    def accept(self) -> str:
        return f'konstruct lock --update {self.column}'

    def unprint(self) -> bool:
        """Set aside what differs only as a canonical form can print it (a formula, a condition,
        a column upstream), keeping the rest; whether there was any."""
        printed = [r for r in self.reasons if r in _PRINTED]
        if printed:
            self.reasons = [r for r in self.reasons if r not in _PRINTED]
            self.formula_after, self.conditions_added, self.conditions_removed = None, [], []
            if 'unknown' not in self.reasons:
                self.formula_before = None
        return bool(printed)

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != 'changed_by' and v not in (None, [])}
        data['changed_by'] = [a.to_dict() for a in self.changed_by]
        data['accept'] = self.accept
        keep = self.keep
        if keep:
            data['keep'] = keep
        return data

    @property
    def keep(self) -> str | None:
        """How to keep the locked meaning instead: undo the change where it was made, or, for a
        commit in another team's repository, ask them to."""
        for attribution in self.changed_by:
            if attribution.sha:
                by = f' (by {attribution.author})' if attribution.author else ''
                if attribution.repository in (None, '.'):
                    return f'git revert {attribution.short}{by}'
                owner = self.repository or attribution.repository
                return f'ask {owner} to revert {attribution.short}{by}'
            if attribution.path:
                return f'undo the uncommitted change to {attribution.path}'
        return None

    def lines(self) -> list[str]:
        where = [w for w in (f'built by {self.repository}' if self.repository else None, self.path) if w]
        head = self.column + (f'  ({self.label}' + (f', {", ".join(where)}' if where else '') + ')')
        lines = [head]
        if 'removed' in self.reasons:
            lines.append(f'  removed: {self.removed or "no such column in this build"}')
            if self.formula_before:
                lines.append(f'    locked  {self.formula_before}')
        if 'unknown' in self.reasons:
            lines.append('  the formula could not be worked out in this build')
            lines.append(f'    locked  {self.formula_before}')
            lines.append('    build with the inputs it was locked with (the dbt artefacts, the repositories)')
        if 'upstream' in self.reasons:
            lines.append('  a column further upstream changed, beyond what the composed formula shows')
            lines.append(f'    formula  {self.formula_before}')
        if 'formula' in self.reasons:
            lines += [
                '  formula changed',
                f'    locked  {self.formula_before}',
                f'    now     {self.formula_after}',
            ]
            difference = _difference(self.formula_before or '', self.formula_after or '')
            if difference:
                lines.append(f'    change  {difference}')
        lines += [f'  condition added    {c}' for c in self.conditions_added]
        lines += [f'  condition removed  {c}' for c in self.conditions_removed]
        lines += [f'  source added       {s}' for s in self.sources_added]
        lines += [f'  source removed     {s}' for s in self.sources_removed]
        lines += [
            f'  the file that builds {f} changed, and its columns have no formula to compare'
            for f in self.files
        ]
        if 'type' in self.reasons:
            lines.append(f'  type               {self.type_before} -> {self.type_after}')
        lines += [f'  definition         {t}' for t in self.terms]
        for attribution in self.changed_by:
            lines.append(f'  changed by         {attribution}')
            if attribution.before is not None and attribution.after is not None:
                lines.append(f'                     {attribution.what}: was {attribution.before}')
                lines.append(f'                     {" " * len(attribution.what)}  now {attribution.after}')
        if self.affected:
            lines.append(f'  your columns       {", ".join(self.affected)}')
        lines.append(f'  accept it          {self.accept}')
        keep = self.keep
        if keep:
            lines.append(f'  or keep the old    {keep}')
        return lines


@dataclass
class LockCheck:
    """``konstruct lock --check``: whether every pinned column still means what was locked."""

    path: str
    """The lockfile, relative to the project."""
    pinned: int
    """How many columns the lockfile pins."""
    changes: list[PinChange] = field(default_factory=list)
    """Pinned columns that changed: each fails the check."""
    notes: list[str] = field(default_factory=list)
    """What changed without failing: a verification lost, a term re-approved, a new upstream read
    not yet pinned, a pin on a column this build does not build."""
    consumer: str | None = None
    """The repository whose lockfile this is, when it is not the project's own."""
    canonical: str = CANONICAL
    """The canonical form the lockfile was written with."""
    relock: int = 0
    """With a canonical form this Konstruct does not write, how many pins read differently: they
    fail the check once, as a whole, rather than one by one."""

    @property
    def passed(self) -> bool:
        return not self.changes and not self.relock

    def summary(self) -> str:
        who = f' ({self.consumer})' if self.consumer else ''
        since = 'it was' if len(self.changes) == 1 else 'they were'
        changed = (
            f'{len(self.changes)} of {plural(self.pinned, "pinned upstream column")} changed since '
            f'{since} locked.'
        )
        if self.relock:
            return (
                f'{self.path}{who}: written with canonical form {self.canonical}, which this Konstruct '
                f'does not write ({CANONICAL}); re-lock after review.'
                + (f' {changed}' if self.changes else '')
            )
        if self.passed:
            return f'{self.path}{who}: {plural(self.pinned, "pinned upstream column")}, none changed.'
        return f'{self.path}{who}: {changed}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': self.path,
            'consumer': self.consumer,
            'passed': self.passed,
            'summary': self.summary(),
            'pinned': self.pinned,
            'canonical': self.canonical,
            'relock': self.relock,
            'changes': [c.to_dict() for c in self.changes],
            'notes': self.notes,
        }

    def _relock_lines(self) -> list[str]:
        if not self.relock:
            return []
        return [
            f'{self.relock} of {plural(self.pinned, "pin")} read differently in this form, which may '
            'be the form rather than a change. Review what the project reads (`konstruct explain`), '
            'then accept it: konstruct lock --update'
        ]

    def __str__(self) -> str:
        lines = [self.summary(), *self._relock_lines()]
        for change in self.changes:
            lines += ['', *change.lines()]
        if self.notes:
            lines += ['', *(f'note: {n}' for n in self.notes)]
        if len(self.changes) > 1:
            lines += ['', 'Accept every change: konstruct lock --update']
        return '\n'.join(lines)

    def markdown(self) -> str:
        """The check for a pull request comment. Built with ``history`` False, it names no commit,
        author or subject, so a private producer's history stays out of a public pull request."""
        lines = [f'### {self.summary()}', *self._relock_lines()]
        for change in self.changes:
            lines += ['', '```', *change.lines(), '```']
        lines += [f'- {n}' for n in self.notes]
        return '\n'.join(lines) + '\n'


def check(
    graph: Graph,
    root: Path,
    *,
    path: Path | None = None,
    consumer: str | None = None,
    history: bool = True,
) -> LockCheck:
    """Compare every pin in ``konstruct.lock`` with this build of the graph.

    ``consumer`` names the repository (one of ``repositories``) whose lockfile it is, when it is
    not the project's own: then only the pins on columns this build builds are judged, each
    composed as far as the consumer's build saw it. ``history`` False skips reading git history
    to say who changed what."""
    target = path or Path(root) / FILE_NAME
    lockfile = Lockfile.read(target)
    boundary = _boundary(graph, lockfile) if consumer is not None else set()
    pinner = Pinner(graph, Path(root), consumer, boundary)
    display = display_namer(graph)
    attribute = Attributor(graph, Path(root), pinner, locked_at(target)) if history else None
    result = LockCheck(
        path=relative_path(target, root),
        pinned=len(lockfile.pins),
        consumer=consumer,
        canonical=lockfile.canonical,
    )
    pinned = set()
    unjudged: list[str] = []
    for old in lockfile.pins:
        node = find(graph, old.column, old.id)
        pinned.add(node.id if node is not None else old.column)
        if consumer is not None and not pinner.judged(node, old.column):
            unjudged.append(old.column)
            continue
        change, _ = pinner.compare(old, node, display, result.notes)
        if change is None:
            continue
        change.affected = pinner.affected(node) if node is not None else old.used_by
        if attribute is not None:
            change.changed_by = attribute(change, old, node)
        result.changes.append(change)
    if lockfile.canonical != CANONICAL:
        # Only formulas and conditions are printed in the canonical form, so only their
        # differences may be the form's: they fail once, as a whole. The rest fail as they are.
        for change in result.changes:
            if change.unprint():
                result.relock += 1
        result.changes = [c for c in result.changes if c.reasons]
        if not result.relock:
            result.notes.append(
                f'{result.path} was written with canonical form {lockfile.canonical}; this Konstruct '
                f'writes {CANONICAL} and reads every pin the same, and `konstruct lock` records it'
            )
    if unjudged:
        one = len(unjudged) == 1
        result.notes.append(
            f'{plural(len(unjudged), "pinned column")} {"is" if one else "are"} not built in this build, '
            f'so {"it was" if one else "they were"} not checked: {", ".join(unjudged)}'
        )
    if not lockfile.reads:
        unpinned = [
            n for n in upstream_reads(graph, consumer) if n.id not in pinned and pinner.missing(n) is None
        ]
        if unpinned:
            shown = ', '.join(graph.label(n) for n in unpinned[:8]) + (
                ' and more' if len(unpinned) > 8 else ''
            )
            result.notes.append(
                f'{plural(len(unpinned), "upstream column")} read but not pinned: {shown}; '
                'run `konstruct lock` to pin them'
            )
    return result


def check_consumers(graph: Graph, root: Path, *, history: bool = True) -> list[LockCheck]:
    """The lockfile of every repository in ``repositories`` that has one, checked against this
    graph: run in the producer, it names each consumer a change would break."""
    checks = []
    for relative in graph.meta.get('repositories', []):
        target = Path(root) / str(relative) / FILE_NAME
        if not target.is_file():
            continue
        name = _repository_named(graph, str(relative)) or Path(str(relative)).name
        checks.append(check(graph, root, path=target, consumer=name, history=history))
    return checks


def _repository_named(graph: Graph, relative: str) -> str | None:
    """The name the build gave the repository at ``relative``, from the nodes it built."""
    prefix = relative.rstrip('/') + '/'
    for node in graph.nodes.values():
        repository = node.attrs.get('repository')
        if isinstance(repository, str) and node.path and node.path.startswith(prefix):
            return repository
    return None


def term_changes(
    old: list[PinnedTerm], new: list[PinnedTerm], label: str, notes: list[str], when: str = 'locked'
) -> list[str]:
    """Lines for each change to an approved term bound to the column, and to the formula of any
    term (a measure's formula is what it computes, whatever its status: an Ossie metric is always
    a draft); other changes are notes."""
    lines: list[str] = []
    prefix = f'{label}: ' if label else ''
    now = {t.name: t for t in new}
    for was in old:
        current = now.get(was.name)
        relied = was.status == 'approved'
        found: list[str] = []
        if current is None:
            found.append(f'{was.name!r} is no longer bound to this column')
        else:
            if was.status != current.status:
                found.append(f'{was.name!r} status {was.status} -> {current.status}')
            if was.version != current.version:
                for key, line in _meaning_changes(was, current):
                    (lines if key == 'formula' else found).append(f'{was.name!r} {line}')
            elif was.approved != current.approved and current.status == 'approved':
                notes.append(f'{prefix}{was.name!r} re-approved ({was.approved} -> {current.approved})')
        if relied:
            lines += found
        else:
            notes += [f'{prefix}{line} (not approved when {when})' for line in found]
    for name in sorted(set(now) - {t.name for t in old}):
        notes.append(f'{prefix}now bound to {name!r} ({now[name].status}), which was not when {when}')
    return lines


def _meaning_changes(was: PinnedTerm, now: PinnedTerm) -> list[tuple[str, str]]:
    """What changed in a term's meaning, each line with the field it is about."""
    lines = []
    for key in (*_MEANING, 'formula'):
        before, after = getattr(was, key), getattr(now, key)
        if before == after:
            continue
        if isinstance(before, list):
            shown_before, shown_after = ', '.join(before) or 'none', ', '.join(after) or 'none'
        else:
            shown_before, shown_after = before or 'none', after or 'none'
        lines.append(
            (
                key,
                f'{key}: {shown_before!r} -> {shown_after!r}'
                if key == 'definition'
                else f'{key}: {shown_before} -> {shown_after}',
            )
        )
    return lines or [('meaning', 'meaning changed')]


def _difference(before: str, after: str, dialect: str | None = None) -> str | None:
    """The part of a formula that changed, such as ``added * 1.1``: what is left once the SQL
    tokens both versions start and end with are set aside. None when most of the formula
    changed, as the two versions side by side then say it better."""
    try:
        tokenizer = Dialect.get_or_raise(dialect)
        old = [(t.start, t.end + 1) for t in tokenizer.tokenize(before)]
        new = [(t.start, t.end + 1) for t in tokenizer.tokenize(after)]
    except SqlglotError:
        return None

    def same(i: int, j: int) -> bool:
        return before[old[i][0] : old[i][1]] == after[new[j][0] : new[j][1]]

    start = 0
    while start < min(len(old), len(new)) and same(start, start):
        start += 1
    end = 0
    while end < min(len(old), len(new)) - start and same(len(old) - 1 - end, len(new) - 1 - end):
        end += 1
    removed = before[old[start][0] : old[len(old) - 1 - end][1]] if len(old) - end > start else ''
    added = after[new[start][0] : new[len(new) - 1 - end][1]] if len(new) - end > start else ''
    if not removed and not added:
        return None
    if max(len(removed), len(added)) > 0.6 * max(len(before), len(after)):
        return None
    if not removed:
        return f'added {added}'
    if not added:
        return f'removed {removed}'
    return f'{removed} -> {added}'


class Attributor:
    """Who changed what a pin relies on, from git history: the changes to the formulas on its way
    (as ``konstruct history`` reads them, so a reformatting commit is not one), else the last
    commits to the files that build it, preferring those made since the lockfile was written."""

    def __init__(self, graph: Graph, root: Path, pinner: Pinner, since: tuple[str, str] | None) -> None:
        self.graph = graph
        self.root = root
        self.pinner = pinner
        self.since = since
        self.histories: dict[str, Any] = {}
        self.tops: dict[Path, Path | None] = {}

    def __call__(self, change: PinChange, old: Pin, node: Node | None) -> list[Attribution]:
        graph = self.graph
        reasons = set(change.reasons)
        defining = self.pinner.defining(node) if node is not None else None
        steps = self.pinner.walk(defining)[0] if defining is not None else []
        derived = [s for s in steps if 'expression' in s.attrs]
        found: list[Attribution] = []
        if reasons & {'formula', 'sources', 'unknown', 'upstream'}:
            candidates = []
            for step in derived[:MAX_ATTRIBUTED_STEPS]:
                formulas = self.history(step).formulas
                if formulas:
                    last = formulas[-1]
                    owner = graph.parent_of(step)
                    candidates.append(
                        self.attribution(
                            last.commit,
                            graph.label(step),
                            owner.path if owner is not None else last.path,
                            before=last.before,
                            after=last.after,
                        )
                    )
            found += [c for c in candidates if self.recent(c)]
        changed = {'formula', 'conditions', 'sources', 'files', 'unknown', 'upstream', 'removed'}
        if not found and reasons & changed:
            relations: list[Node] = []
            for step in derived or steps:
                owner = graph.parent_of(step)
                if owner is not None and all(owner.id != r.id for r in relations):
                    relations.append(owner)
            if node is None:
                relations += _relations_named(graph, old.column)
            elif change.removed:
                relations += [r for r in [self.pinner.builder(graph.parent_of(node))] if r is not None]
            touched = self.touch(relations)
            if touched is not None:
                found.append(touched)
        if 'definition' in reasons and node is not None and defining is not None:
            terms = {t.name for t in old.terms}
            for column in [node] if defining is node else [node, defining]:
                changes = [t for t in self.history(column).terms if t.term in terms]
                if changes:
                    recent = [t for t in changes if self.recent(t.commit)] or changes[-1:]
                    last = recent[-1]
                    found.append(self.attribution(last.commit, f'term {last.term!r}', last.path))
                    break
        found.sort(key=lambda a: (a.sha == '', a.date), reverse=True)
        unique: list[Attribution] = []
        for attribution in found:
            if all((a.sha, a.what) != (attribution.sha, attribution.what) for a in unique):
                unique.append(attribution)
        return unique[:3]

    def recent(self, commit: Any) -> bool:
        """Whether ``commit`` was made since the lockfile was written (uncommitted edits were): a
        commit in the same second counts, unless it is the one that wrote the lockfile."""
        if not commit.sha or self.since is None:
            return True
        date, sha = self.since
        return bool(commit.date > date or (commit.date == date and commit.sha != sha))

    def history(self, column: Node) -> Any:
        if column.id not in self.histories:
            self.histories[column.id] = history(self.graph, self.root, column, limit=20)
        return self.histories[column.id]

    def top(self, folder: Path) -> Path | None:
        if folder not in self.tops:
            found = (
                run_git(folder, 'rev-parse', '--show-toplevel', check=False).strip()
                if folder.is_dir()
                else ''
            )
            self.tops[folder] = Path(found) if found else None
        return self.tops[folder]

    def touch(self, relations: list[Node]) -> Attribution | None:
        """The latest change to the files that build ``relations``: uncommitted edits, else the
        last commit, with one git call per repository however many files there are."""
        files: dict[Path, dict[str, str]] = {}
        for relation in relations:
            defined = defining_file(self.graph, relation, [])
            if defined is None:
                continue
            target = (self.root / defined[0]).resolve()
            top = self.top(target.parent)
            if top is not None:
                files.setdefault(top, {})[target.relative_to(top.resolve()).as_posix()] = defined[0]
        found: list[Attribution] = []
        for top, paths in files.items():
            repository = self.where(top)
            # NUL-separated, so paths come unquoted; a rename's entry is followed by its old path.
            status = run_git(top, 'status', '--porcelain', '-z', '--', *paths, check=False).split('\0')
            if status[0]:
                inside = status[0][3:]
                shown = paths.get(inside, inside)
                found.append(Attribution(shown, '', '', '', 'uncommitted changes', shown, repository))
                continue
            output = run_git(
                top,
                '-c',
                'core.quotePath=false',
                'log',
                '-1',
                '--name-only',
                '--format=%H%x1f%an%x1f%cI%x1f%s',
                '--',
                *paths,
                check=False,
            )
            lines = [line for line in output.splitlines() if line.strip()]
            if not lines:
                continue
            sha, author, date, subject = [*lines[0].split('\x1f'), '', '', ''][:4]
            changed = next((paths[line] for line in lines[1:] if line in paths), lines[-1])
            found.append(Attribution(changed, sha, author, utc(date), subject, changed, repository))
        found.sort(key=lambda a: (a.sha == '', a.date), reverse=True)
        return found[0] if found else None

    def attribution(
        self, commit: Any, what: str, path: str | None, *, before: str | None = None, after: str | None = None
    ) -> Attribution:
        top = commit.repository
        return Attribution(
            what=what,
            sha=commit.sha,
            author=commit.author,
            date=commit.date,
            subject=commit.subject,
            path=path,
            repository=self.where(top) if top is not None else None,
            before=before,
            after=after,
        )

    def where(self, top: Path) -> str:
        return relative_path(top.resolve(), self.root.resolve())


def locked_at(path: Path) -> tuple[str, str] | None:
    """When the lockfile was written, in UTC, ISO 8601, to the second, with the commit that last
    changed it; with uncommitted edits (or outside git), its modification time and no commit."""
    if not path.is_file():
        return None
    folder = path.resolve().parent
    if not run_git(folder, 'status', '--porcelain', '--', path.name, check=False).strip():
        output = run_git(folder, 'log', '-1', '--format=%cI%x1f%H', '--', path.name, check=False).strip()
        if output:
            date, _, sha = output.partition('\x1f')
            return utc(date), sha
    moment = dt.datetime.fromtimestamp(int(path.stat().st_mtime), dt.UTC)
    return moment.isoformat(), ''
