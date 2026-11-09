"""Ontology packs: open ontologies for the schemas common tools land, bound when Konstruct sees them.

A pack (``konstruct/semantics/ontologies/<name>.yml``) is a definitions file for one source, such as Stripe
or Shopify, with a ``match`` block: the tables a schema must have, each with the columns it must
have (``charge|charges`` accepts either name, and ``events_[0-9]*`` any matching table, the newest
taken). A schema matches only with evidence that it comes from the source as well: its name fits
one of the pack's ``schemas`` patterns, or a loader (Fivetran, Airbyte) landed its tables, so an
application's own ``orders`` table is not taken for Shopify's. The pack's terms, which name
columns as ``<table>.<column>``, are then bound to that schema's columns as drafts; terms whose
tables or columns the schema lacks are left out.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from konstruct.semantics import definitions

PACKS = Path(__file__).parent / 'ontologies'


@dataclass
class Pack:
    name: str
    description: str
    match: dict[str, list[str]]
    """Each required table (``a|b`` alternatives, or a ``*`` pattern) with its required columns."""
    schemas: list[str] = field(default_factory=list)
    """Patterns for the names of schemas the source lands in (``*stripe*``)."""
    terms: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Match:
    """One schema a pack matched, with the actual name of each of the pack's tables in it."""

    pack: Pack
    schema: str
    """The schema's relation key prefix, such as ``analytics.stripe``."""
    tables: dict[str, str]
    """The pack's table name (its first alternative) to the schema's table."""


def load_packs(names: list[str] | None = None) -> list[Pack]:
    """The packs Konstruct ships, all of them or those named."""
    packs = []
    for path in sorted(PACKS.glob('*.yml')):
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        pack = Pack(
            name=str(data.get('name') or path.stem),
            description=str(data.get('description') or ''),
            match={str(k): [str(c).lower() for c in v] for k, v in (data.get('match') or {}).items()},
            schemas=[str(p).lower() for p in data.get('schemas') or []],
            terms=list(data.get('terms') or []),
        )
        if names is None or pack.name in names:
            packs.append(pack)
    return packs


def _canonical(spec: str) -> str:
    return spec.split('|')[0]


def _find(spec: str, tables: dict[str, set[str]], required: list[str]) -> str | None:
    """The table of a schema that ``spec`` names and that has the ``required`` columns."""
    if '*' in spec:
        candidates = sorted(t for t in tables if fnmatch.fnmatch(t, spec))
        candidates = [t for t in candidates if set(required) <= tables[t]]
        return candidates[-1] if candidates else None
    for name in spec.split('|'):
        if name in tables and set(required) <= tables[name]:
            return name
    return None


def find(
    packs: list[Pack], schemas: dict[str, dict[str, set[str]]], loaded: dict[str, set[str]] | None = None
) -> list[Match]:
    """The schemas each pack matches. ``schemas`` maps each schema's key to its tables (lower
    case) and their columns (lower case); ``loaded`` maps a schema to the tables a loader landed."""
    found = []
    for pack in packs:
        for schema in sorted(schemas):
            tables = schemas[schema]
            named = any(fnmatch.fnmatch(schema.rsplit('.', 1)[-1], p) for p in pack.schemas)
            if not named and not (loaded or {}).get(schema):
                continue
            mapped: dict[str, str] = {}
            for spec, required in pack.match.items():
                table = _find(spec, tables, required)
                if table is None:
                    break
                mapped[_canonical(spec)] = table
            else:
                # Tables the terms name but the match does not require, singular or plural.
                for term in pack.terms:
                    for ref in term.get('columns') or []:
                        name = str(ref).rsplit('.', 1)[0]
                        if name not in mapped:
                            table = _find(name if '*' in name else f'{name}|{name}s', tables, [])
                            if table is not None:
                                mapped[name] = table
                found.append(Match(pack, schema, mapped))
    return found


def suffixes(matches: list[Match]) -> dict[str, str]:
    """For a pack matched in several schemas, the shortest end of each schema's key that tells it
    apart (``stripe_eu``, or ``prod.stripe`` beside ``dev.stripe``)."""
    result: dict[str, str] = {}
    by_pack: dict[str, list[str]] = {}
    for match in matches:
        by_pack.setdefault(match.pack.name, []).append(match.schema)
    for schemas in by_pack.values():
        if len(schemas) < 2:
            continue
        for schema in schemas:
            parts = schema.split('.')
            for size in range(1, len(parts) + 1):
                end = '.'.join(parts[-size:])
                if sum(1 for other in schemas if other == end or other.endswith('.' + end)) == 1:
                    result[schema] = end
                    break
    return result


def terms(
    match: Match, schemas: dict[str, dict[str, set[str]]], suffix: str | None = None
) -> definitions.DefinitionsFile:
    """The pack's terms bound to one matched schema, as a parsed definitions file. Terms whose
    columns the schema does not have are left out; ``suffix`` tells apart the same pack matched
    in several schemas."""
    tables = schemas[match.schema]
    entries = []
    for term in match.pack.terms:
        columns = []
        for ref in term.get('columns') or []:
            table, _, column = str(ref).rpartition('.')
            actual = match.tables.get(table)
            if actual is not None and column.lower() in tables[actual]:
                columns.append(f'{match.schema}.{actual}.{column}')
        if not columns:
            continue
        # A pack's terms are drafts until the project adopts them in its own definitions.yml.
        entry = {k: v for k, v in term.items() if k not in ('status', 'owner', 'approved')}
        entry['columns'] = columns
        if suffix:
            entry['name'] = f'{term["name"]} ({suffix})'
        entries.append(entry)
    path = f'ontology pack {match.pack.name}'
    return definitions.parse(yaml.safe_dump({'version': 1, 'terms': entries}, sort_keys=False), path)
