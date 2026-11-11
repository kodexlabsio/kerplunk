"""Business definitions: ``definitions.yml``, kept in the repository and reviewed like code.

A definitions file records what the business means by a term and which columns hold it::

    version: 1
    terms:
      - name: Customer lifetime value
        kind: measure               # entity, measure or term; inferred when left out
        synonyms: [lifetime value, LTV, CLV]
        definition: Total value of all payments a customer has made, in Australian dollars.
        owner: {name: Priya Shah, role: Head of Finance}
        status: approved            # draft, agreed, approved or retired
        approved: 2026-10-30        # required once approved
        grain: one row per customer
        columns: [customers.customer_lifetime_value]
        filters: ["orders.status <> 'returned'"]
        unit: currency
        currency: AUD
        related: [Order amount]
        exceptions: Gift card payments made before 2024 are excluded.
        sensitivity: personal       # marks the bound columns as holding sensitive data

``columns`` and the columns in ``filters`` use the names the rest of Konstruct accepts
(``table.column``, ``schema.table.column``, a dbt source). The build attaches each term to the
graph as a ``term`` node with ``defines`` edges to its columns and ``relates_to`` edges to related
terms, and warns about anything that no longer matches the data stack.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from konstruct.core import yamlio

STATUSES = ('draft', 'agreed', 'approved', 'retired')
KINDS = ('entity', 'measure', 'term')
FILE_NAMES = ('definitions.yml', 'definitions.yaml')
FIELDS = frozenset(
    {
        'name',
        'kind',
        'synonyms',
        'definition',
        'owner',
        'status',
        'approved',
        'grain',
        'columns',
        'filters',
        'unit',
        'currency',
        'related',
        'broader',
        'exceptions',
        'sensitivity',
    }
)
_CURRENCY = re.compile(r'^[A-Z]{3}$')


@dataclass
class Owner:
    name: str
    role: str | None = None

    def __str__(self) -> str:
        return f'{self.name} ({self.role})' if self.role else self.name


@dataclass
class Term:
    name: str
    definition: str
    path: str
    line: int
    synonyms: list[str] = field(default_factory=list)
    owner: Owner | None = None
    status: str = 'draft'
    approved: str | None = None
    grain: str | None = None
    columns: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    unit: str | None = None
    currency: str | None = None
    related: list[str] = field(default_factory=list)
    broader: list[str] = field(default_factory=list)
    """The wider concepts this one is a kind of (SKOS ``broader``): a refund rate is a rate."""
    exceptions: str | None = None
    kind: str | None = None
    """``entity``, ``measure`` or ``term``; unset, the ontology infers it from the bound columns."""
    iri: str | None = None
    """For a term from a SKOS or OWL vocabulary, the concept's IRI."""
    sensitivity: str | None = None
    """What kind of sensitive data the term's columns hold, such as ``personal``: each bound
    column is marked sensitive, and so is every column computed from it, and a relation the term
    is bound to is marked to hold such data (see :mod:`konstruct.analysis.sensitivity`)."""

    @property
    def where(self) -> str:
        """Where the term is defined: ``path:line``, or for a vocabulary concept its file and IRI."""
        if self.line:
            return f'{self.path}:{self.line}'
        return f'{self.path} ({self.iri})' if self.iri else self.path

    @property
    def slug(self) -> str:
        return slug(self.name)

    @property
    def names(self) -> list[str]:
        """The name and every synonym."""
        return [self.name, *self.synonyms]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.owner is None:
            data['owner'] = None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Term:
        fields = dict(data)
        owner = fields.pop('owner', None)
        return cls(owner=Owner(**owner) if owner else None, **fields)


@dataclass
class DefinitionsFile:
    path: str
    terms: list[Term] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {'path': self.path, 'terms': [t.to_dict() for t in self.terms], 'errors': self.errors}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DefinitionsFile:
        return cls(
            path=data['path'],
            terms=[Term.from_dict(t) for t in data.get('terms', [])],
            errors=list(data.get('errors', [])),
        )


def slug(name: str) -> str:
    """``Customer lifetime value`` -> ``customer_lifetime_value``: the term's node id suffix."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or 'term'


class _LineLoader(yaml.SafeLoader):
    """A safe loader that records the line each mapping starts on, as ``__line__``."""


def _mapping_with_line(loader: _LineLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping = {str(k): v for k, v in loader.construct_mapping(node, deep=True).items()}
    mapping['__line__'] = node.start_mark.line + 1
    return mapping


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_with_line)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return ' '.join(value.split()) or None
    if isinstance(value, (int, float, dt.date)):
        return str(value)
    return None


def _strings(value: Any) -> list[str] | None:
    """A list of strings, or a single string as a list; None if it is neither."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    return None


def parse(text: str, path: str) -> DefinitionsFile:
    """Parse a definitions file. Problems are collected as ``path:line: message``, so one bad
    term does not hide the rest."""
    return _parse(lambda: yamlio.load(text, _LineLoader), path)


def _parse(load: Callable[[], Any], path: str) -> DefinitionsFile:
    result = DefinitionsFile(path=path)

    def problem(line: int, message: str) -> None:
        result.errors.append(f'{path}:{line}: {message}')

    try:
        data = load()
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        line = mark.line + 1 if mark is not None else 1
        problem(line, f'not valid YAML: {getattr(error, "problem", None) or error}')
        return result
    if data is None:
        return result
    if not isinstance(data, dict):
        problem(1, 'expected a mapping with a `terms` list')
        return result
    version = data.get('version', 1)
    if version != 1:
        problem(int(data.get('__line__', 1)), f'unsupported version {version!r}; expected 1')
        return result
    terms = data.get('terms')
    if terms is None:
        return result
    if not isinstance(terms, list):
        problem(int(data.get('__line__', 1)), '`terms` must be a list')
        return result
    for entry in terms:
        if not isinstance(entry, dict):
            problem(int(data.get('__line__', 1)), 'each term must be a mapping')
            continue
        term = _term(entry, path, problem)
        if term is not None:
            result.terms.append(term)
    return result


def _term(entry: dict[str, Any], path: str, problem: Any) -> Term | None:
    line = int(entry.pop('__line__', 1))
    name = _text(entry.get('name'))
    if not name:
        problem(line, 'term has no `name`')
        return None
    label = f'term {name!r}'
    for key in sorted(set(entry) - FIELDS):
        close = [f for f in FIELDS if f.startswith(key[:3])]
        hint = f'; did you mean `{close[0]}`?' if close else ''
        problem(line, f'{label}: unknown field `{key}`{hint}')
    definition = _text(entry.get('definition'))
    if not definition:
        problem(line, f'{label}: no `definition`')
        definition = ''
    status = _text(entry.get('status')) or 'draft'
    if status not in STATUSES:
        problem(line, f'{label}: status {status!r} is not one of {", ".join(STATUSES)}')
        status = 'draft'
    approved_value = entry.get('approved')
    approved: str | None = None
    if isinstance(approved_value, dt.date):
        approved = approved_value.isoformat()
    elif isinstance(approved_value, str):
        try:
            approved = dt.date.fromisoformat(approved_value.strip()).isoformat()
        except ValueError:
            problem(line, f'{label}: `approved` must be a date such as 2026-10-30, not {approved_value!r}')
    elif approved_value is not None:
        problem(line, f'{label}: `approved` must be a date such as 2026-10-30')
    owner_value = entry.get('owner')
    owner: Owner | None = None
    if isinstance(owner_value, dict):
        owner_value.pop('__line__', None)
        owner_name = _text(owner_value.get('name'))
        if owner_name:
            owner = Owner(owner_name, _text(owner_value.get('role')))
        else:
            problem(line, f'{label}: `owner` needs a `name`')
        for key in sorted(set(owner_value) - {'name', 'role'}):
            problem(line, f'{label}: unknown owner field `{key}`')
    elif isinstance(owner_value, str) and owner_value.strip():
        match = re.match(r'^(.*?)\s*\(([^()]*)\)\s*$', owner_value.strip())
        owner = Owner(match[1], match[2] or None) if match else Owner(owner_value.strip())
    elif owner_value is not None:
        problem(line, f'{label}: `owner` must be a name or a mapping with `name` and `role`')
    lists: dict[str, list[str]] = {}
    for key in ('synonyms', 'columns', 'filters', 'related', 'broader'):
        value = _strings(entry.get(key))
        if value is None:
            problem(line, f'{label}: `{key}` must be a list of strings')
            value = []
        lists[key] = value
    kind = _text(entry.get('kind'))
    if kind is not None and kind not in KINDS:
        problem(line, f'{label}: kind {kind!r} is not one of {", ".join(KINDS)}')
        kind = None
    currency = _text(entry.get('currency'))
    if currency and not _CURRENCY.match(currency):
        problem(line, f'{label}: currency {currency!r} should be an ISO 4217 code such as GBP')
    sensitivity = entry.get('sensitivity')
    if sensitivity is not None and not (isinstance(sensitivity, str) and sensitivity.strip()):
        problem(line, f'{label}: `sensitivity` must be a word such as personal')
        sensitivity = None
    return Term(
        name=name,
        definition=definition,
        path=path,
        line=line,
        synonyms=lists['synonyms'],
        owner=owner,
        status=status,
        approved=approved,
        grain=_text(entry.get('grain')),
        columns=lists['columns'],
        filters=lists['filters'],
        unit=_text(entry.get('unit')),
        currency=currency,
        related=lists['related'],
        broader=lists['broader'],
        exceptions=_text(entry.get('exceptions')),
        kind=kind,
        sensitivity=' '.join(sensitivity.lower().split()) if isinstance(sensitivity, str) else None,
    )


def parse_file(path: Path, relative: str | None = None) -> DefinitionsFile:
    """Parse a definitions file from disk; ``relative`` is the path reported in problems."""
    return _parse(lambda: yamlio.read(path, _LineLoader), relative or str(path))


def validate(terms: list[Term]) -> list[str]:
    """Problems that need every term at once: duplicate names, unknown related terms, and
    approved terms without an owner or an approval date. Column checks need the graph and are
    done by the build."""
    problems: list[str] = []
    seen: dict[str, Term] = {}
    for term in terms:
        for name in term.names:
            key = name.lower()
            other = seen.get(key)
            if other is not None and other is not term:
                problems.append(
                    f'{term.where}: term {term.name!r}: {name!r} is also a name of '
                    f'{other.name!r} ({other.where})'
                )
            else:
                seen[key] = term
    for term in terms:
        where = f'{term.where}: term {term.name!r}'
        if term.status == 'approved':
            if term.owner is None:
                problems.append(f'{where}: approved but has no owner')
            if term.approved is None:
                problems.append(f'{where}: approved but has no `approved` date')
        if term.status in ('agreed', 'approved') and not term.columns:
            problems.append(f'{where}: {term.status} but bound to no columns')
        for related in term.related:
            if related.lower() not in seen:
                problems.append(f'{where}: related term {related!r} is not defined')
        for wider in term.broader:
            if wider.lower() not in seen:
                problems.append(f'{where}: broader term {wider!r} is not defined')
    # A concept cannot be wider than itself: a walk over every broader term, each cycle once.
    state: dict[str, int] = {}
    reported: set[frozenset[str]] = set()

    def visit(term: Term, path: list[Term]) -> None:
        state[term.name.lower()] = 1
        for wider in term.broader:
            parent = seen.get(wider.lower())
            if parent is None:
                continue
            key = parent.name.lower()
            if state.get(key) == 1:
                cycle = [*path[path.index(parent) :], parent] if parent in path else [term, parent]
                members = frozenset(t.name.lower() for t in cycle)
                if members not in reported:
                    reported.add(members)
                    problems.append(
                        f'{parent.where}: term {parent.name!r}: broader terms form a cycle '
                        f'({" -> ".join(t.name for t in cycle)})'
                    )
            elif key not in state:
                visit(parent, [*path, parent])
        state[term.name.lower()] = 2

    for term in terms:
        if term.name.lower() not in state:
            visit(term, [term])
    return problems
