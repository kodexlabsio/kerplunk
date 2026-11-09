"""Looker semantic models (LookML), read as exposures of the tables they query.

Each LookML view becomes an exposure: what a dashboard built on it reads. Files are parsed with the
``lkml`` parser (nothing in them is run), then views are resolved across files: a refinement
(``view: +orders``) adds to or replaces the fields of ``orders``, a view that ``extends`` others
takes their table and fields under its own, and an abstract view (``extension: required``) is a
base only. A view over a table (``sql_table_name``, or the view's own name when it gives none)
reads that table, and each field reads the table columns its SQL names as ``${TABLE}.column``
(in ``sql``, ``sql_start``, ``sql_end``, ``sql_latitude`` and ``sql_longitude``), directly or
through other fields of the view it names as ``${field}`` or ``${view.field}``, including the
generated fields of a dimension group (``${created_date}``) and the fields a measure filters on.
A view over a derived table reads the tables its SQL reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import lkml
import sqlglot
from sqlglot import exp

from konstruct.sql import PARSE_ERRORS

FILE_SUFFIX = '.lkml'
_TABLE_COLUMN = re.compile(r'\$\{TABLE\}\.("(?:[^"]|"")+"|`[^`]+`|\w+)')
_FIELD = re.compile(r'\$\{(?:(\w+)\.)?(\w+)\}')
_FIELD_KINDS = ('dimensions', 'dimension_groups', 'measures', 'filters', 'parameters')
_SQL_KEYS = ('sql', 'sql_start', 'sql_end', 'sql_latitude', 'sql_longitude')
_DEFAULT_TIMEFRAMES = ('raw', 'time', 'date', 'week', 'month', 'quarter', 'year')
"""The timeframes Looker gives a time dimension group that lists none."""


@dataclass
class LookerField:
    name: str
    kind: str
    """``dimension``, ``dimension_group``, ``measure``, ``filter`` or ``parameter``."""
    columns: list[str] = field(default_factory=list)
    """The table columns it reads, through other fields of the view too."""
    label: str | None = None
    description: str | None = None


@dataclass
class LookerView:
    name: str
    path: str
    table: str | None = None
    """The table it reads (its ``sql_table_name`` without quoting, or its own name)."""
    derived_reads: list[str] = field(default_factory=list)
    """For a derived table, the tables its SQL reads."""
    fields: list[LookerField] = field(default_factory=list)
    label: str | None = None
    description: str | None = None


@dataclass
class LookerFile:
    """One file as the parser gives it: its view definitions, unresolved."""

    path: str
    views: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {'path': self.path, 'views': self.views, 'problems': self.problems}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LookerFile:
        return cls(str(data['path']), list(data['views']), list(data['problems']))


def parse(text: str, path: str) -> LookerFile:
    """The view definitions of a LookML file, as the parser gives them."""
    result = LookerFile(path=path)
    try:
        data: dict[str, Any] = lkml.load(text) or {}
    except Exception as error:  # lkml raises its own syntax errors
        result.problems.append(f'{path}: not valid LookML: {error}')
        return result
    result.views = [v for v in data.get('views', []) or [] if isinstance(v, dict) and v.get('name')]
    return result


def _unquote(name: str) -> str:
    if len(name) > 1 and name[0] == name[-1] and name[0] in '"`':
        return name[1:-1].replace(name[0] * 2, name[0])
    return name


def _table_name(text: str) -> str | None:
    """A ``sql_table_name`` as a dotted name, or None when it is built from Liquid, constants or
    parameters. An alias (``analytics.orders AS o``) is left out."""
    if '{' in text or '%' in text or '@{' in text:
        return None
    text = (
        re.sub(r'\s+(?:AS\s+)?\w+\s*$', '', text.strip(), flags=re.IGNORECASE)
        if ' ' in text.strip()
        else text
    )
    parts = [_unquote(p.strip()) for p in re.split(r'\.(?=(?:[^"`]*["`][^"`]*["`])*[^"`]*$)', text.strip())]
    return '.'.join(parts) if all(parts) else None


def _derived_reads(sql: str, dialect: str | None) -> list[str] | None:
    if '${' in sql or '{%' in sql or '{{' in sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except PARSE_ERRORS:
        return None
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    reads = set()
    for table in tree.find_all(exp.Table):
        if table.name and not (not table.db and table.name.lower() in ctes):
            reads.add('.'.join(p for p in (table.catalog, table.db, table.name) if p))
    return sorted(reads)


def _list(raw: dict[str, Any], key: str) -> list[Any]:
    """A list attribute, as lkml gives it (``extends__all: [[...]]``, or plainly)."""
    value = raw.get(f'{key}__all')
    if isinstance(value, list):
        return [item for part in value for item in (part if isinstance(part, list) else [part])]
    value = raw.get(key)
    return value if isinstance(value, list) else []


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """``extra`` over ``base``: its attributes win, and its fields add to or replace base's."""
    merged = {**base, **{k: v for k, v in extra.items() if k not in _FIELD_KINDS and k != 'name'}}
    if _list(base, 'extends') and _list(extra, 'extends'):
        merged['extends__all'] = [_list(base, 'extends') + _list(extra, 'extends')]
        merged.pop('extends', None)
    for own, inherited in (('sql_table_name', 'derived_table'), ('derived_table', 'sql_table_name')):
        if own in extra and inherited not in extra:
            merged.pop(inherited, None)
    for kind in _FIELD_KINDS:
        fields = {f['name']: f for f in base.get(kind, []) or [] if isinstance(f, dict) and f.get('name')}
        for item in extra.get(kind, []) or []:
            if isinstance(item, dict) and item.get('name'):
                fields[item['name']] = {**fields.get(item['name'], {}), **item}
        merged[kind] = list(fields.values())
    return merged


def resolve(files: list[LookerFile], dialect: str | None = None) -> tuple[list[LookerView], list[str]]:
    """The views of a LookML project, with refinements and ``extends`` applied, abstract views
    left out, and the table and columns each field reads."""
    problems: list[str] = []
    definitions: dict[str, tuple[dict[str, Any], str]] = {}
    refinements: list[tuple[str, dict[str, Any], str]] = []
    for file in sorted(files, key=lambda f: f.path):
        for raw in file.views:
            name = str(raw['name'])
            if name.startswith('+'):
                refinements.append((name[1:], raw, file.path))
            elif name in definitions:
                problems.append(
                    f'{file.path}: view {name!r} is also defined in {definitions[name][1]}; kept the first'
                )
            else:
                definitions[name] = (raw, file.path)
    for name, raw, path in refinements:
        if name in definitions:
            definitions[name] = (_merge(definitions[name][0], raw), definitions[name][1])
        else:
            problems.append(f'{path}: refinement +{name} refines a view that is not defined')

    def extended(name: str, seen: frozenset[str] = frozenset()) -> dict[str, Any]:
        raw = definitions[name][0]
        if not _list(raw, 'extends'):
            return raw
        merged: dict[str, Any] = {'name': name}
        for base in _list(raw, 'extends'):
            if base in definitions and base not in seen:
                merged = _merge(merged, extended(str(base), seen | {name}))
        return _merge(merged, raw)

    views = []
    for name in sorted(definitions):
        raw, path = definitions[name]
        if raw.get('extension') == 'required':
            continue
        views.append(_view(extended(name), path, dialect, problems))
    return views, problems


def _view(raw: dict[str, Any], path: str, dialect: str | None, problems: list[str]) -> LookerView:
    view = LookerView(
        name=str(raw['name']), path=path, label=raw.get('label'), description=raw.get('description')
    )
    where = f'{path}: view {view.name!r}'
    derived = raw.get('derived_table')
    if isinstance(derived, dict):
        if derived.get('sql'):
            reads = _derived_reads(str(derived['sql']), dialect)
            if reads is None:
                problems.append(f'{where}: the derived table SQL uses Liquid or LookML references')
            view.derived_reads = reads or []
        else:
            problems.append(f'{where}: a native derived table (explore_source) is not read')
    elif raw.get('sql_table_name'):
        view.table = _table_name(str(raw['sql_table_name']))
        if view.table is None:
            problems.append(f'{where}: sql_table_name is built at run time, so it is skipped')
    else:
        view.table = view.name  # Looker reads the table named after the view
    items: dict[str, dict[str, Any]] = {}
    kinds: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for plural in _FIELD_KINDS:
        for item in raw.get(plural, []) or []:
            if isinstance(item, dict) and item.get('name'):
                name = str(item['name'])
                items[name], kinds[name] = item, plural[:-1]
                if plural == 'dimension_groups':
                    for timeframe in _list(item, 'timeframes') or (
                        _DEFAULT_TIMEFRAMES if item.get('type') != 'duration' else ()
                    ):
                        aliases[f'{name}_{timeframe}'] = name
                    for interval in _list(item, 'intervals'):
                        aliases[f'{interval}s_{name}'] = name
    for name, kind in kinds.items():
        item = items[name]
        view.fields.append(
            LookerField(
                name,
                kind,
                _columns_of(name, view.name, items, kinds, aliases) if view.table else [],
                item.get('label'),
                item.get('description'),
            )
        )
    return view


def _columns_of(
    name: str,
    view: str,
    items: dict[str, dict[str, Any]],
    kinds: dict[str, str],
    aliases: dict[str, str],
    seen: frozenset[str] = frozenset(),
) -> list[str]:
    """The table columns a field reads: those its SQL names as ``${TABLE}.column``, and those of
    the fields of the same view it names or filters on."""
    item = items.get(name, {})
    sqls = [str(item[k]) for k in _SQL_KEYS if item.get(k)]
    found = [_unquote(c) for sql in sqls for c in _TABLE_COLUMN.findall(sql)]
    if not sqls and kinds.get(name) == 'dimension' and item.get('type') not in ('location', 'distance'):
        found.append(name)  # a dimension with no SQL reads the column of its own name
    others = [
        f for sql in sqls for qualifier, f in _FIELD.findall(sql) if qualifier in ('', view) and f != 'TABLE'
    ]
    for condition in _list(item, 'filters'):
        if isinstance(condition, dict):
            others += [
                str(k).split('.')[-1] for k in condition if '.' not in str(k) or str(k).split('.')[0] == view
            ]
    for other in others:
        other = aliases.get(other, other)
        if other in items and other not in seen and other != name:
            found += _columns_of(other, view, items, kinds, aliases, seen | {name})
    return sorted(set(found))
