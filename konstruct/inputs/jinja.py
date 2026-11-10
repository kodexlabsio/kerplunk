"""Render dbt model SQL without dbt: Jinja with ref, source, var, config, the project's macros and
its packages' macros, and adapter.dispatch.

Konstruct uses dbt's compiled SQL when it has it. This renderer is for SQL dbt has not compiled: the
other side of a `konstruct diff`, or a project whose artefacts are missing. It runs in a sandbox, never
touches a database, and stands in for anything that would (run_query, statement, load_result) with
an empty result.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from jinja2 import Undefined
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

_SET = re.compile(r'^\s*\{%-?\s*(set|do)\b')


_DATEPARTS = frozenset(
    {
        'year',
        'quarter',
        'month',
        'week',
        'day',
        'hour',
        'minute',
        'second',
        'millisecond',
        'microsecond',
        'dayofweek',
    }
)


def _sql_argument(value: Any) -> str:
    """dbt passes SQL snippets as strings ('ordered_at'); date parts are passed bare ('day')."""
    if isinstance(value, (list, tuple)):
        return ', '.join(_sql_argument(v) for v in value)
    text = str(value)
    return f"'{text}'" if text.lower() in _DATEPARTS else text


class _Lenient(Undefined):
    """An undefined name that can be called, indexed and iterated. Called, it renders as a function
    call that keeps its arguments, so the SQL still parses and the columns passed to an unknown
    macro still appear in the lineage."""

    def __call__(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
        values = [*args, *kwargs.values()]
        return f'konstruct_macro({", ".join(_sql_argument(v) for v in values)})' if values else ''

    def __getattr__(self, name: str) -> Any:
        if name.startswith('__'):
            raise AttributeError(name)
        return self

    def __getitem__(self, key: Any) -> _Lenient:  # type: ignore[override]
        return self

    def __iter__(self) -> Any:
        return iter(())

    def __str__(self) -> str:
        return ''

    def __bool__(self) -> bool:
        return False


class _Namespace:
    def __init__(self, macros: dict[str, Any]) -> None:
        self._macros = macros

    def __getattr__(self, name: str) -> Any:
        try:
            return self._macros[name]
        except KeyError as error:
            raise AttributeError(name) from error


class _DbtBuiltins:
    """dbt's own cross-database macros (dbt.date_trunc and friends), as plain SQL."""

    def __getattr__(self, name: str) -> Any:
        return _Lenient(name=name)

    def date_trunc(self, datepart: str, date: str) -> str:
        return f"date_trunc('{datepart}', {date})"

    def dateadd(self, datepart: str, interval: Any, from_date_or_timestamp: str) -> str:
        return f'date_add({from_date_or_timestamp}, interval ({interval}) {datepart})'

    def datediff(self, first_date: str, second_date: str, datepart: str) -> str:
        return f"date_diff('{datepart}', {first_date}, {second_date})"

    def last_day(self, date: str, datepart: str) -> str:
        return f'last_day({date})'

    def cast(self, field: str, type: str) -> str:
        return f'cast({field} as {type})'

    def safe_cast(self, field: str, type: str) -> str:
        return f'try_cast({field} as {type})'

    def concat(self, fields: list[str]) -> str:
        return ' || '.join(str(f) for f in fields)

    def hash(self, field: str) -> str:
        return f'md5(cast({field} as varchar))'

    def split_part(self, string_text: str, delimiter_text: str, part_number: Any) -> str:
        return f'split_part({string_text}, {delimiter_text}, {part_number})'

    def replace(self, field: str, old_chars: str, new_chars: str) -> str:
        return f'replace({field}, {old_chars}, {new_chars})'

    def length(self, expression: str) -> str:
        return f'length({expression})'

    def right(self, string_text: str, length_expression: str) -> str:
        return f'right({string_text}, {length_expression})'

    def position(self, substring_text: str, string_text: str) -> str:
        return f'position({substring_text} in {string_text})'

    def any_value(self, expression: str) -> str:
        return f'any_value({expression})'

    def bool_or(self, expression: str) -> str:
        return f'bool_or({expression})'

    def listagg(
        self,
        measure: str,
        delimiter_text: str = "','",
        order_by_clause: str | None = None,
        limit_num: Any = None,
    ) -> str:
        return f'string_agg({measure}, {delimiter_text})'

    def string_literal(self, value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def escape_single_quotes(self, expression: str) -> str:
        return str(expression).replace("'", "''")

    def date_spine(self, datepart: str, start_date: str, end_date: str) -> str:
        return (
            f'select cast(spine as date) as date_{datepart} '
            f'from generate_series(cast({start_date} as timestamp), cast({end_date} as timestamp), '
            f'interval 1 {datepart}) as t(spine)'
        )

    def current_timestamp(self) -> str:
        return 'current_timestamp'

    def type_string(self) -> str:
        return 'varchar'

    def type_int(self) -> str:
        return 'integer'

    def type_bigint(self) -> str:
        return 'bigint'

    def type_numeric(self) -> str:
        return 'numeric'

    def type_float(self) -> str:
        return 'float'

    def type_boolean(self) -> str:
        return 'boolean'

    def type_timestamp(self) -> str:
        return 'timestamp'


class _Adapter:
    def __init__(self, adapter_type: str, lookup: Callable[[str], Any]) -> None:
        self.type_ = adapter_type
        self._lookup = lookup

    def type(self) -> str:
        return self.type_

    def dispatch(self, name: str, macro_namespace: str | None = None) -> Any:
        for candidate in (f'{self.type_}__{name}', f'default__{name}', name):
            found = self._lookup(candidate)
            if found is not None:
                return found
        return _Lenient()

    def quote(self, identifier: str) -> str:
        return f'"{identifier}"'


def _macro_text(paths: list[Path], keep: Callable[[Iterable[Path]], list[Path]] | None = None) -> str:
    texts: list[str] = []
    for base in paths:
        if base.is_dir():
            files = sorted(base.rglob('*.sql'))
            texts.extend(p.read_text(encoding='utf-8', errors='replace') for p in (keep or list)(files))
    return '\n'.join(texts)


class Renderer:
    """Renders models of one dbt project, with its macros and its installed packages' macros."""

    def __init__(
        self,
        project_root: Path,
        manifest: dict[str, Any],
        *,
        adapter_type: str | None = None,
        macro_paths: list[Path] | None = None,
        keep: Callable[[Iterable[Path]], list[Path]] | None = None,
    ) -> None:
        self.manifest = manifest
        self.adapter_type = adapter_type or manifest.get('metadata', {}).get('adapter_type') or 'default'
        self.env = SandboxedEnvironment(undefined=_Lenient, extensions=['jinja2.ext.do'])
        self.relations: dict[str, str] = {}
        self.by_package: dict[tuple[str, str], str] = {}
        for node in manifest.get('nodes', {}).values():
            if node.get('resource_type') in ('model', 'seed', 'snapshot') and node.get('relation_name'):
                self.relations.setdefault(node['name'], node['relation_name'])
                self.by_package[(node.get('package_name', ''), node['name'])] = node['relation_name']
        self.sources = {
            (s['source_name'], s['name']): s['relation_name']
            for s in manifest.get('sources', {}).values()
            if s.get('relation_name')
        }
        self.variables: dict[str, Any] = {}
        project_name = manifest.get('metadata', {}).get('project_name')
        self.macros: dict[str, Any] = {}
        self.packages: dict[str, _Namespace] = {}
        context = self._context()
        packages_dir = project_root / 'dbt_packages'
        if packages_dir.is_dir():
            for package in sorted(p for p in packages_dir.iterdir() if p.is_dir()):
                macros = self._load(_macro_text([package / 'macros'], keep), context)
                self.packages[package.name] = _Namespace(macros)
                for name, macro in macros.items():
                    self.macros.setdefault(name, macro)
        own = self._load(
            _macro_text(macro_paths if macro_paths is not None else [project_root / 'macros'], keep), context
        )
        self.macros.update(own)
        if project_name:
            self.packages[project_name] = _Namespace(own)

    def _lookup(self, name: str) -> Any:
        return self.macros.get(name)

    def _ref(self, *args: Any, **kwargs: Any) -> str:
        names = [a for a in args if isinstance(a, str)]
        if len(names) >= 2:
            return self.by_package.get((names[0], names[1]), names[1])
        name = names[0] if names else ''
        return self.relations.get(name, name)

    def _source(self, source_name: str, table: str) -> str:
        return str(self.sources.get((source_name, table), f'{source_name}.{table}'))

    def _var(self, name: str, default: Any = None) -> Any:
        return self.variables.get(name, default)

    def _context(self) -> dict[str, Any]:
        empty = _Lenient()
        return {
            'ref': self._ref,
            'source': self._source,
            'var': self._var,
            'config': lambda *a, **k: '',
            'is_incremental': lambda: False,
            'env_var': lambda name, default='': default,
            'adapter': _Adapter(self.adapter_type, self._lookup),
            'target': {'type': self.adapter_type, 'name': 'dev', 'schema': '', 'database': ''},
            'execute': False,
            'run_query': lambda *a, **k: empty,
            'load_result': lambda *a, **k: empty,
            'return': lambda value: value,
            'log': lambda *a, **k: '',
            'exceptions': empty,
            'this': empty,
            'modules': empty,
            'dbt_utils': _Namespace({}),
            'dbt': _DbtBuiltins(),
        }

    def _load(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            module = self.env.from_string(text).make_module(context)
        except TemplateError:
            return {}
        return {name: getattr(module, name) for name in dir(module) if not name.startswith('_')}

    def render(self, sql: str) -> str:
        context = self._context()
        context.update(self.macros)
        context.update(self.packages)
        rendered: str = self.env.from_string(sql).render(context)
        return rendered


def render_model(sql: str, renderer: Renderer | None, fallback: Callable[[str], str]) -> str:
    """Render with ``renderer`` when there is one and it succeeds; otherwise with ``fallback``."""
    if renderer is not None:
        try:
            return renderer.render(sql)
        except (TemplateError, TypeError, ValueError, AttributeError, KeyError, RecursionError):
            pass
    return fallback(sql)
