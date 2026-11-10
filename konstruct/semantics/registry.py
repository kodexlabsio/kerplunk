"""Ontology packs: open ontologies for the schemas common tools land, bound when Konstruct sees them.

A pack is a definitions file for one source, such as Stripe or Zendesk, with a ``match`` block:
the tables a schema must have, each with the columns it must have (``charge|charges`` accepts
either name, and ``events_[0-9]*`` any matching table, the newest taken). ``match`` may also be a
list of such blocks, one for each way the source is landed (Fivetran and a Singer tap name the
same tables differently); any one of them matching is enough. A schema matches only with evidence
that it comes from the source as well: its name fits one of the pack's ``schemas`` patterns, or a
loader (Fivetran, Airbyte) landed its tables, so an application's own ``orders`` table is not
taken for Shopify's. A block of generic table names alone (``users``, ``orders``) with fewer
than three columns that are not generic needs the schema's name. A schema named for a source
also matches its pack when it holds two of the pack's entity tables with their keys, as any
loader names them (a Singer tap's ``tap_jira.issues``).

Every pack that matches a schema is bound to it, so a schema that one loader fills from several
sources gets each source's pack. Packs whose blocks hold in the same tables contend for them, and
the best binds: the one the schema's name points to (by the longest literal part of its
``schemas`` patterns), then the one whose block requires more tables, then more columns, then the
one whose name sorts first. The pack's terms, which name columns as ``<table>.<column>``, are
bound to that schema's columns as drafts; terms whose tables or columns the schema lacks are left
out, and an entity or a measure is not bound to a table Fivetran's history mode lands. A table
that the match does not require is not taken when another bound pack requires it, and one that
several bound packs want goes to the pack that names it exactly (``tickets``, where another names
``ticket`` and finds it in the plural), or to none. Its ``relationships``
(``{from: ticket.requester_id, to: user.id}``) become ontology relationships between the entities
of those tables where the schema has both columns.

Packs are written by hand in ``ontologies/*.yml`` or generated from published connector schemas
into ``ontologies/generated/`` by ``.kodex/scripts/generate_packs.py``. ``ontologies/index.json.gz``
holds them all, merged, so a build reads one file; a hand-written pack's terms win over a
generated one's.

Konstruct's own packs are a directory in the plugin registry (:mod:`konstruct.plugins.registry`),
as a plugin's packs are; :func:`load_packs` reads every directory the registry has.
"""

from __future__ import annotations

import fnmatch
import gzip
from dataclasses import asdict, dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import yaml

from konstruct.semantics import definitions

if TYPE_CHECKING:
    from konstruct.plugins.registry import Plugins

PACKS = Path(__file__).parent / 'ontologies'
"""The directory of Konstruct's own packs."""
INDEX = PACKS / 'index.json.gz'
GENERIC_COLUMNS = frozenset(
    (
        'id uuid gid key name title label description summary notes note body content text message '
        'email phone first_name last_name full_name display_name username address city state country '
        'zip postal_code timezone time_zone locale language currency type kind category status state_name '
        'url link href slug domain website image avatar icon color created created_at created_on updated '
        'updated_at updated_on modified modified_at deleted deleted_at archived is_deleted is_active active '
        'enabled disabled visible hidden public private date timestamp start_date end_date start end '
        'starts_at ends_at started_at ended_at due_date value amount total price quantity count position '
        'order sort_order rank priority version metadata properties data attributes settings tags object '
        'user_id account_id organization_id owner_id parent_id customer_id company_id project_id team_id '
        'group_id external_id source source_id role subject code number reference company'
    ).split()
)
"""Column names too common to tell one source from another, or from an application's own tables;
a generated match block requires two columns besides these."""
GENERIC_TABLES = frozenset(
    (
        'account accounts address addresses category categories comment comments companies company contact '
        'contacts customer customers department departments employee employees event events file files '
        'group groups invoice invoices issue issues item items location locations member members message '
        'messages note notes order orders organization organizations payment payments permission '
        'permissions product products project projects role roles session sessions setting settings '
        'subscription subscriptions tag tags task tasks team teams ticket tickets transaction transactions '
        'user users'
    ).split()
)
"""Table names any application might have."""


VERSIONED = frozenset(('_fivetran_active', '_fivetran_start'))
"""Columns of a table that Fivetran's history mode lands, with a row for each version of an object:
its id is not a key."""


def is_generic_table(spec: str) -> bool:
    """Whether every name a table spec accepts is a table name any application might have."""
    return '*' not in spec and all(name in GENERIC_TABLES for name in spec.split('|'))


def needs_name(block: dict[str, list[str]]) -> bool:
    """Whether a match block holds only in a schema named for its source: one of generic table
    names alone, with fewer than three columns that are not generic between them."""
    distinctive = {c for columns in block.values() for c in columns} - GENERIC_COLUMNS
    return all(is_generic_table(spec) for spec in block) and len(distinctive) < 3


@dataclass
class Pack:
    name: str
    description: str
    match: list[dict[str, list[str]]]
    """Ways the source's schema can be recognised; each is a set of required tables (``a|b``
    alternatives, or a ``*`` pattern) with their required columns."""
    schemas: list[str] = field(default_factory=list)
    """Patterns for the names of schemas the source lands in (``*stripe*``)."""
    terms: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    """``{'from': 'table.column', 'to': 'table.column'}``: a column and the key it names."""
    title: str = ''
    """The source's own name, such as ``Zendesk Support``."""
    origins: list[str] = field(default_factory=list)
    """Where its tables come from: ``hand-written``, or the loaders a generated pack is made from,
    ``fivetran``, ``singer`` and ``dlt``."""
    source: str = 'konstruct'
    """The distribution that provides the pack."""


@dataclass
class Match:
    """One schema a pack matched, with the actual name of each of the pack's tables in it."""

    pack: Pack
    schema: str
    """The schema's relation key prefix, such as ``analytics.stripe``."""
    tables: dict[str, str]
    """The pack's table name (its first alternative, or the spec a term uses) to the schema's table."""


def read_pack(data: dict[str, Any], default_name: str, origin: str) -> Pack:
    """A pack from its YAML data."""
    raw_match = data.get('match') or {}
    blocks = raw_match if isinstance(raw_match, list) else [raw_match]
    return Pack(
        name=str(data.get('name') or default_name),
        description=str(data.get('description') or ''),
        match=[
            {str(k): [str(c).lower() for c in v or []] for k, v in block.items()}
            for block in blocks
            if isinstance(block, dict) and block
        ],
        schemas=[str(p).lower() for p in data.get('schemas') or []],
        terms=list(data.get('terms') or []),
        relationships=[
            {'from': str(r['from']), 'to': str(r['to'])}
            for r in data.get('relationships') or []
            if isinstance(r, dict) and r.get('from') and r.get('to')
        ],
        title=str(data.get('title') or ''),
        origins=[str(o) for o in data.get('origins') or [origin]],
    )


def _refs(term: dict[str, Any]) -> set[tuple[str, str]]:
    """Each table name and column a term binds, one pair for each ``|`` alternative."""
    out = set()
    for ref in term.get('columns') or []:
        table, _, column = str(ref).lower().rpartition('.')
        out |= {(name, column) for name in table.split('|')}
    return out


def merge(own: Pack, generated: Pack) -> Pack:
    """A hand-written pack with a generated pack of the same source: its own terms first, then
    the generated terms whose names and columns it does not already have. A column is the same
    when any of its table's ``|`` alternatives and its column are."""
    names = {str(t['name']).lower() for t in own.terms}
    columns = set().union(*(_refs(t) for t in own.terms)) if own.terms else set()
    terms = list(own.terms)
    for term in generated.terms:
        if str(term['name']).lower() in names or _refs(term) & columns:
            continue
        terms.append(term)
    pairs = {(r['from'], r['to']) for r in own.relationships}
    return Pack(
        name=own.name,
        description=own.description or generated.description,
        match=own.match + [m for m in generated.match if m not in own.match],
        schemas=list(dict.fromkeys(own.schemas + generated.schemas)),
        terms=terms,
        relationships=own.relationships
        + [r for r in generated.relationships if (r['from'], r['to']) not in pairs],
        title=own.title or generated.title,
        origins=list(dict.fromkeys(own.origins + generated.origins)),
    )


def build_index(directory: Path = PACKS) -> list[dict[str, Any]]:
    """Every pack in ``directory``, hand-written and generated, merged by name, as index data."""
    own = {}
    for path in sorted(directory.glob('*.yml')):
        pack = read_pack(yaml.safe_load(path.read_text(encoding='utf-8')) or {}, path.stem, 'hand-written')
        own[pack.name] = pack
    generated = {}
    for path in sorted((directory / 'generated').glob('*.yml')):
        pack = read_pack(yaml.safe_load(path.read_text(encoding='utf-8')) or {}, path.stem, 'generated')
        generated[pack.name] = pack
    packs = []
    for name in sorted(own.keys() | generated.keys()):
        if name in own and name in generated:
            packs.append(merge(own[name], generated[name]))
        else:
            packs.append(own.get(name) or generated[name])
    return [{k: v for k, v in asdict(p).items() if k != 'source'} for p in packs]


def read_index() -> list[dict[str, Any]]:
    """The packs in the index, as ``build_index`` gives them; compressed, as the index of a few
    hundred packs is over a megabyte of JSON. A build reads only the index, never the YAML files
    it is made from."""
    data: list[dict[str, Any]] = orjson.loads(gzip.decompress(INDEX.read_bytes()))
    return data


@cache
def _all() -> tuple[Pack, ...]:
    return tuple(Pack(**item) for item in read_index())


def load_packs(
    names: list[str] | None = None, *, plugins: Plugins | None = None, problems: list[str] | None = None
) -> list[Pack]:
    """The packs of every pack directory in ``plugins`` (default: Konstruct's own), all of them or
    those named. Konstruct's own directory is read through its index. A pack whose name an earlier
    directory already has is left out, and a plugin's file that cannot be read too, each with a
    line added to ``problems`` when it is given."""
    from konstruct.plugins.registry import Plugins

    packs: list[Pack] = []
    problems = problems if problems is not None else []
    seen: dict[str, str] = {}
    for plugin in (plugins or Plugins.builtin()).group('packs'):
        if plugin.builtin:
            found = list(_all())
        else:
            found = []
            for path in sorted(Path(plugin.loaded).glob('*.yml')):
                try:
                    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
                    pack = read_pack(data, path.stem, 'hand-written')
                except (OSError, yaml.YAMLError, AttributeError, TypeError) as error:
                    problems.append(f'{plugin.label}: cannot read {path.name}: {error}')
                    continue
                found.append(replace(pack, source=plugin.distribution))
        for pack in found:
            if pack.name in seen:
                problems.append(
                    f'{plugin.label}: pack {pack.name!r} is already provided by {seen[pack.name]}'
                )
                continue
            seen[pack.name] = plugin.distribution
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


def _plural(name: str) -> str:
    """A table's name in the plural, as many loaders name tables: company is companies."""
    if name.endswith('y') and name[-2:-1] not in ('a', 'e', 'o', 'u'):
        return name[:-1] + 'ies'
    return name + ('es' if name.endswith(('s', 'x', 'ch', 'sh')) else 's')


def _table_of(spec: str, mapped: dict[str, str]) -> str | None:
    return mapped.get(spec) or mapped.get(_canonical(spec))


def _required(
    pack: Pack, tables: dict[str, set[str]], named: bool
) -> tuple[tuple[int, int], dict[str, str]] | None:
    """The tables of the pack's best match block that holds in a schema, with the block's size in
    tables and columns. A block of generic table names alone, with fewer than three columns that
    are not generic, holds only in a schema its name points to. A schema named for the source
    holds the pack too when it has two of the pack's entity tables with their keys, whatever
    loader landed them (a Singer tap's ``tap_hubspot.contacts``)."""
    best: tuple[tuple[int, int], dict[str, str]] | None = None
    for block in pack.match:
        if not named and needs_name(block):
            continue
        mapped: dict[str, str] = {}
        for spec, required in block.items():
            table = _find(spec, tables, required)
            if table is None:
                break
            mapped[_canonical(spec)] = table
        else:
            size = (len(set(mapped.values())), sum(len(c) for c in block.values()))
            if best is None or size > best[0]:
                best = (size, mapped)
    if best is None and named:
        keyed: dict[str, str] = {}
        for term in pack.terms:
            for ref in term.get('columns') or [] if term.get('kind') == 'entity' else []:
                spec, _, key = str(ref).rpartition('.')
                table = _find(spec if '*' in spec else f'{spec}|{_plural(_canonical(spec))}', tables, [key])
                if table is not None and table not in keyed.values():
                    keyed[_canonical(spec)] = table
        if len(keyed) >= 2:
            best = ((len(keyed), 0), keyed)
    return best


def _optional(pack: Pack, tables: dict[str, set[str]], mapped: dict[str, str]) -> dict[str, tuple[str, bool]]:
    """The tables the pack's terms and relationships name but its match does not require, each
    with whether the pack names it exactly rather than in the plural."""
    refs = [str(r) for t in pack.terms for r in t.get('columns') or []]
    refs += [r[side] for r in pack.relationships for side in ('from', 'to')]
    out: dict[str, tuple[str, bool]] = {}
    for ref in refs:
        name = ref.rsplit('.', 1)[0]
        if _table_of(name, mapped) is None and name not in out:
            found = _find(name if '*' in name else f'{name}|{_plural(_canonical(name))}', tables, [])
            if found is not None:
                out[name] = (found, '*' in name or found in name.split('|'))
    return out


def find(
    packs: list[Pack], schemas: dict[str, dict[str, set[str]]], loaded: dict[str, set[str]] | None = None
) -> list[Match]:
    """The packs each schema matches. ``schemas`` maps each schema's key to its tables (lower
    case) and their columns (lower case); ``loaded`` maps a schema to the tables a loader landed.

    Every pack whose match holds is bound. Packs whose required tables overlap contend for them,
    and the best binds: the pack the schema's name points to (by the pattern with the longest
    literal part, so ``*salesforce_history*`` beats ``*salesforce*``), then the one whose match block
    requires more tables, then more columns, then the pack whose name sorts first. A table a
    pack's terms name but its match does not require is not taken when another bound pack
    requires it; one that several bound packs want goes to the pack that names it exactly, or to
    none."""
    found = []
    for schema in sorted(schemas):
        tables = schemas[schema]
        last = schema.rsplit('.', 1)[-1]
        landed = bool((loaded or {}).get(schema))
        candidates = []
        for pack in packs:
            # The longest literal part of a pattern the schema's name fits: salesforce_history
            # fits *salesforce* and, better, *salesforce_history*.
            named = max(
                (len(p.replace('*', '')) for p in pack.schemas if fnmatch.fnmatch(last, p)), default=0
            )
            if not named and not landed:
                continue
            held = _required(pack, tables, bool(named))
            if held is not None:
                candidates.append(((-named, -held[0][0], -held[0][1], pack.name), pack, held[1]))
        claimed: set[str] = set()
        bound: list[tuple[Pack, dict[str, str]]] = []
        for _, pack, mapped in sorted(candidates, key=lambda c: c[0]):
            if set(mapped.values()) & claimed:
                continue
            claimed |= set(mapped.values())
            bound.append((pack, mapped))
        optional = {pack.name: _optional(pack, tables, mapped) for pack, mapped in bound}
        wanted: dict[str, set[str]] = {}
        exactly: dict[str, set[str]] = {}
        for name, held_ in optional.items():
            for table, exact in held_.values():
                wanted.setdefault(table, set()).add(name)
                if exact:
                    exactly.setdefault(table, set()).add(name)
        for pack, mapped in bound:
            own = set(mapped.values())
            extra = {}
            for name, (table, _) in optional[pack.name].items():
                # A table another bound pack requires is its; one several want goes to the one
                # that names it exactly, and to none when that does not settle it.
                rivals = exactly.get(table) or wanted[table]
                if table in own or (table not in claimed and rivals == {pack.name}):
                    extra[name] = table
            found.append(Match(pack, schema, {**mapped, **extra}))
    return sorted(found, key=lambda m: (m.pack.name, m.schema))


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


def _versioned(schemas: dict[str, dict[str, set[str]]], column: str) -> bool:
    """Whether the table of a ``<schema>.<table>.<column>`` keeps a row for each version."""
    schema, table, _ = column.rsplit('.', 2)
    return bool(schemas[schema][table] & VERSIONED)


def _column(match: Match, schemas: dict[str, dict[str, set[str]]], ref: str) -> str | None:
    """``<schema>.<table>.<column>`` for a pack's ``<table>.<column>``, when the schema has it."""
    table, _, column = ref.rpartition('.')
    actual = _table_of(table, match.tables)
    if actual is not None and column.lower() in schemas[match.schema][actual]:
        return f'{match.schema}.{actual}.{column.lower()}'
    return None


def terms(
    match: Match, schemas: dict[str, dict[str, set[str]]], suffix: str | None = None
) -> definitions.DefinitionsFile:
    """The pack's terms bound to one matched schema, as a parsed definitions file. Terms whose
    columns the schema does not have are left out; ``suffix`` tells apart the same pack matched
    in several schemas. An entity or a measure is not bound to a table that keeps a row for each
    version of an object, as Fivetran's history mode lands it."""
    entries = []
    for term in match.pack.terms:
        columns = [c for c in (_column(match, schemas, str(ref)) for ref in term.get('columns') or []) if c]
        if term.get('kind') in ('entity', 'measure'):
            # One row for each version: neither a key nor a figure to add up.
            columns = [c for c in columns if not _versioned(schemas, c)]
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


def relationships(match: Match, schemas: dict[str, dict[str, set[str]]]) -> list[tuple[str, str]]:
    """The pack's relationships in one matched schema, as pairs of ``<schema>.<table>.<column>``."""
    pairs = []
    for rel in match.pack.relationships:
        many, one = _column(match, schemas, rel['from']), _column(match, schemas, rel['to'])
        if many is not None and one is not None and not _versioned(schemas, one):
            pairs.append((many, one))
    return pairs
