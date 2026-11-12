"""`konstruct sensitivity`: where sensitive data comes from, where the lineage takes it, and where it stops.

A column is **marked** sensitive by what already classifies it:

* a business term with a ``sensitivity`` (``sensitivity: personal``) in ``definitions.yml`` or in an
  ontology pack marks each column the term binds; a term bound to a relation marks the relation
  as one that holds such data;
* the warehouse marks a column with a tag that classifies data (``PII``, ``PHI``, ``PCI``,
  ``SENSITIVE``, ``CONFIDENTIAL``, ``GDPR``, ``PERSONAL_DATA``, Snowflake's ``PRIVACY_CATEGORY``
  and ``SEMANTIC_CATEGORY``, a ``SENSITIVITY`` or ``CLASSIFICATION`` tag, Databricks' ``class.*``
  tags, and the tags ``[sensitivity] tags`` names), on the column or on its table, unless its
  value says the data is not sensitive (``none``, ``public``, ``insensitive``), or with a masking
  policy. Konstruct reads them from Snowflake, Databricks and BigQuery (see
  :mod:`konstruct.inputs.warehouse`).

Sensitivity then **flows** down the column lineage: a column computed from a sensitive column is
sensitive too, with the same labels, whether it copies the value or transforms it (``UPPER``,
``CONCAT``, ``LEFT``, a ``CASE`` or ``COALESCE`` branch or condition, a ``UNION``, a window
function, a join that brings it in, a count), and so is every field of a sensitive struct or map
(``profile.email``, ``profile['email']``) and every struct that holds a sensitive field. A hash
(``MD5``, ``SHA2``, ``HASH``, ``FARM_FINGERPRINT`` and the like) does not stop it: a hashed value
is found again by hashing a guess, so a column whose every read is hashed is still sensitive, as
**pseudonymised**. The rule is conservative, and a read of a sensitive column stops it only when
it is inside one of:

* a function ``[sensitivity] sanitisers`` names, such as a tokenisation function;
* an aggregate (``SUM``, ``AVG``, ``COUNT``, ``STDDEV``, ``VARIANCE`` and their kin) whose groups
  the SQL requires to hold at least *k* distinct values of a key (``[sensitivity] k``, 10 by
  default): ``HAVING COUNT(DISTINCT customer_id) >= 10``. A count of rows proves nothing, since
  one person can have any number of rows, and neither does a count of the input's own row or
  primary key, which is unique per row; the key counted must be a foreign key to the key of an
  entity whose own relation has a column marked sensitive (users with a name, not orders), as a
  dbt ``relationships`` test, a pack's or an Ossie model's relationship, or a join to the entity's
  key says. In the gate, a test, a join, an entity term, an Ossie key or a primary key in a file
  the change touches is no such evidence. The proof holds only where the statement reads one
  relation, with no join, no ``WHERE``, at most one ``GROUP BY`` and that on no identifier, unique
  or sensitive column and with no ``ROLLUP``, ``CUBE`` or ``GROUPING SETS``, and the ``HAVING`` is
  that count and nothing else; where the aggregate is the expression's only one, is not a window
  function, has no ``FILTER``, and reads the sensitive column alone, with no ``CASE``, ``IF`` or
  comparison inside it.

Anything else keeps the column sensitive: ``MIN``, ``MAX``, ``MEDIAN``, ``ANY_VALUE``,
``ARRAY_AGG`` and ``STRING_AGG`` (they return or collect individual values), an expression
Konstruct did not read or cannot parse, a read it cannot find in the expression, a column only
part of whose reads are stopped. Columns that only join, filter or group a relation's rows do not
pass their values on: they are listed as **references** and do not make the relation's columns
sensitive.

A built relation or an exposure that holds sensitive data should be **marked to hold it**: listed
in ``[sensitivity] holders`` (names or glob patterns), bound to a sensitive term, or, for one
column, marked itself. ``konstruct gate`` fails a change that moves sensitive data into a model
or exposure not marked to hold it, and one that makes a mark disappear: a column a sensitive term
binds at the base and not after the change, or a relation the change touches that reads, with
no column lineage, one that held sensitive data at the base.

A column copied, unsanitised, from a column with a masking policy into a table or a materialised
view (not a view, which the warehouse masks as it reads the source) holds the values in clear
unless it has a masking policy of its own: it is an **unmasked copy**.
"""

from __future__ import annotations

import re
import time
import tomllib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from konstruct.core.graph import RELATION_KINDS, Graph, Node, split_reading
from konstruct.core.graph import column_id as _column_id
from konstruct.core.sql import PARSE_ERRORS
from konstruct.engine import snapshot

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style
    from konstruct.plugins.api import CheckContext, Finding

DEFAULT_K = 10
"""The smallest group an aggregate must be taken over to stop sensitivity, by default."""


def _normal(name: str) -> str:
    """A function or tag name as compared: lower case, without underscores, dashes or spaces."""
    return re.sub(r'[\s_-]+', '', name.lower())


HASHES = frozenset(
    _normal(name)
    for name in (
        'md5', 'md5_hex', 'md5_binary', 'md5_digest', 'md5_number_lower64', 'md5_number_upper64', 'sha',
        'sha1', 'sha1_hex', 'sha1_binary', 'sha1_digest', 'sha2', 'sha2_hex', 'sha2_binary',
        'sha2_digest', 'sha224', 'sha256', 'sha384', 'sha512', 'standard_hash', 'hash', 'hashbytes',
        'farm_fingerprint', 'xxhash32', 'xxhash64', 'xxh3', 'xxhash3_64', 'murmurhash3_32',
        'murmurhash3_64', 'cityhash64', 'siphash64', 'hash_md5', 'hash_sha256', 'keccak256',
    )
)  # fmt: skip
"""The hash functions, as :func:`_normal` writes them: a value read only through one is
pseudonymised, and still sensitive, since a guess hashed the same way finds it."""

GROUPED = frozenset(
    _normal(name)
    for name in (
        'sum', 'avg', 'mean', 'count', 'stddev', 'stddev_pop', 'stddev_samp', 'variance', 'variance_pop',
        'variance_samp', 'var_pop', 'var_samp', 'corr', 'covar_pop', 'covar_samp',
    )
)  # fmt: skip
"""The aggregates that stop sensitivity over groups the SQL requires to hold at least *k* keys."""

TAGS = frozenset(
    _normal(name)
    for name in (
        'pii', 'phi', 'pci', 'personal', 'personal_data', 'personal_information', 'sensitive',
        'sensitivity', 'confidential', 'gdpr', 'privacy', 'privacy_category', 'semantic_category',
        'classification', 'data_classification',
    )
)  # fmt: skip
"""The warehouse tags that mark a column sensitive, besides Databricks' ``class.*`` tags."""

_CONTAINERS = frozenset(
    _normal(name)
    for name in (
        'sensitivity',
        'classification',
        'data_classification',
        'privacy_category',
        'semantic_category',
    )
)
"""Tags whose value, rather than their name, says what kind of data the column holds."""

_NOT_SENSITIVE = frozenset(
    _normal(v)
    for v in ('none', 'public', 'insensitive', 'not_sensitive', 'non_sensitive', 'false', 'no', '0')
)


_CALL = re.compile(r'([A-Za-z_][A-Za-z0-9_$]*)\s*\(')


def _function(node: exp.Expr) -> str:
    """A function's name as :func:`_normal` writes it (``SHA256`` and ``sha2_digest`` alike)."""
    if isinstance(node, exp.Anonymous):
        return _normal(str(node.name))
    return _normal(node.key)


# Settings


@dataclass(frozen=True)
class Settings:
    """``[sensitivity]`` in ``konstruct.toml``."""

    holders: tuple[str, ...] = ()
    """Relations and exposures marked to hold sensitive data, by name, label, warehouse name or
    id, each a glob pattern (``stg_*``)."""
    sanitisers: tuple[str, ...] = ()
    """Functions that stop sensitivity, such as a tokenisation function."""
    tags: tuple[str, ...] = ()
    """Warehouse tags that mark a column sensitive, besides the built-in ones."""
    k: int = DEFAULT_K
    """The smallest group an aggregate must be taken over to stop sensitivity."""

    @classmethod
    def read(cls, table: Any) -> Settings:
        """The settings in a ``[sensitivity]`` table (None for none).

        Raises:
            konstruct.engine.config.ConfigError: The table is not valid.
        """
        from konstruct.engine.config import ConfigError

        if table is None:
            return cls()
        if not isinstance(table, Mapping):
            raise ConfigError('[sensitivity] must be a table')
        unknown = sorted(set(table) - {'holders', 'sanitisers', 'tags', 'k'})
        if unknown:
            raise ConfigError(
                f'[sensitivity] has no setting {unknown[0]!r}; it takes holders, sanitisers, tags, k'
            )
        lists: dict[str, tuple[str, ...]] = {}
        for key in ('holders', 'sanitisers', 'tags'):
            value = table.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ConfigError(f'[sensitivity] {key} must be a list of names: {key} = ["..."]')
            lists[key] = tuple(v.strip() for v in value)
        k = table.get('k', DEFAULT_K)
        if isinstance(k, bool) or not isinstance(k, int) or k < 2:
            raise ConfigError('[sensitivity] k must be a whole number of at least 2')
        return cls(lists['holders'], lists['sanitisers'], lists['tags'], k)

    def strictest(self, other: Settings | None) -> Settings:
        """These settings and ``other`` together, each as strict as either: a holder or a
        sanitiser only when both have it, a tag when either does, and the larger *k*."""
        if other is None:
            return self
        holders = tuple(h for h in self.holders if h in other.holders)
        sanitisers = tuple(s for s in self.sanitisers if _normal(s) in {_normal(o) for o in other.sanitisers})
        tags = tuple(dict.fromkeys([*self.tags, *other.tags]))
        return Settings(holders, sanitisers, tags, max(self.k, other.k))

    def to_dict(self) -> dict[str, Any]:
        return {
            'holders': list(self.holders),
            'sanitisers': list(self.sanitisers),
            'tags': list(self.tags),
            'k': self.k,
        }


def config_at(root: Path, ref: str | None) -> tuple[dict[str, Any], str] | None:
    """The project's configuration, with the file it is in: ``konstruct.toml``, or else
    ``[tool.konstruct]`` in ``pyproject.toml``, at the commit ``ref``, or in the working tree when
    ``ref`` is None. None when there is neither, when the commit cannot be read (an unknown or a
    shallow commit) or when the file is not valid TOML."""
    import subprocess

    from konstruct.analysis.diff import file_at

    top = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=False
    )
    if top.returncode != 0:
        return None
    repository = Path(top.stdout.strip())
    try:
        prefix = root.resolve().relative_to(repository.resolve())
    except ValueError:
        return None
    for name, pyproject in (('konstruct.toml', False), ('pyproject.toml', True)):
        try:
            text = file_at(repository, ref, (prefix / name).as_posix())
        except Exception:  # an unknown commit
            return None
        if text is None:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        if pyproject:
            tool = data.get('tool', {}).get('konstruct')
            if not isinstance(tool, dict):
                continue
            data = tool
        return data, (prefix / name).as_posix()
    return None


def settings_at(root: Path, ref: str | None) -> Settings | None:
    """``[sensitivity]`` in the project's configuration at ``ref`` (see :func:`config_at`); None
    when there is no configuration there, or it cannot be read or is not valid."""
    from konstruct.engine.config import ConfigError

    found = config_at(root, ref)
    if found is None:
        return None
    try:
        return Settings.read(found[0].get('sensitivity'))
    except ConfigError:
        return None


# Results


@dataclass
class Mark:
    """What marks a column sensitive."""

    sensitivity: str
    """The kind of data, such as ``personal``, or the warehouse tag's name or value."""
    by: str
    """What says so: a business term, a warehouse tag or a masking policy."""

    def to_dict(self) -> dict[str, Any]:
        return {'sensitivity': self.sensitivity, 'by': self.by}


@dataclass
class SensitiveColumn:
    """A column holding sensitive data: marked itself, or computed from one that is."""

    column: str
    """The column id."""
    label: str
    """Its short name."""
    relation: str
    """Its relation's short name."""
    relation_id: str
    """Its relation's id."""
    sensitivity: list[str]
    """The kinds of sensitive data it holds."""
    marks: list[Mark] = field(default_factory=list)
    """What marks it, when it is marked itself."""
    sources: list[str] = field(default_factory=list)
    """The marked columns it is computed from, by short name; empty for a marked column."""
    path: list[str] = field(default_factory=list)
    """The columns from a marked column to this one, the marked one first."""
    transform: str = 'marked'
    """``marked``; ``direct`` when every step on the path passes the value unchanged;
    ``pseudonymised`` when every path to it hashes the value; else ``transformed``."""
    built: bool = False
    """Whether something in the graph builds its relation (a model, a SQL file, a task)."""
    held: str | None = None
    """What marks it, or its relation, to hold sensitive data; None when nothing does."""
    masking_policy: str | None = None
    """The warehouse's masking policy on it, if it has one."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'column': self.column,
            'label': self.label,
            'relation': self.relation,
            'sensitivity': self.sensitivity,
            'marks': [m.to_dict() for m in self.marks],
            'sources': self.sources,
            'path': self.path,
            'transform': self.transform,
            'built': self.built,
            'held': self.held,
            'masking_policy': self.masking_policy,
        }


@dataclass
class Sanitised:
    """A read of a sensitive column that a transform stops."""

    column: str
    """The column computed, by short name."""
    source: str
    """The sensitive column it reads, by short name."""
    by: str
    """What stops it: a sanitiser (``TOKENISE``), or ``SUM over groups of at least 10 distinct keys``."""

    def to_dict(self) -> dict[str, Any]:
        return {'column': self.column, 'source': self.source, 'by': self.by}


@dataclass
class ExposureFlow:
    """An exposure (a dashboard, a BI view) that shows sensitive data."""

    exposure: str
    """The exposure's short name."""
    exposure_id: str
    """Its id."""
    sensitivity: list[str]
    """The kinds of sensitive data it shows."""
    columns: list[str] = field(default_factory=list)
    """The sensitive columns it reads, by short name."""
    column_ids: list[str] = field(default_factory=list, repr=False)
    """Their ids, for tools."""
    held: str | None = None
    """What marks it to hold sensitive data; None when nothing does."""
    path: str | None = field(default=None, repr=False)
    """The file that defines it, for the gate."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'exposure': self.exposure,
            'sensitivity': self.sensitivity,
            'columns': self.columns,
            'held': self.held,
        }


@dataclass
class Reference:
    """A sensitive column that only joins, filters or groups a relation's rows."""

    relation: str
    """The relation whose rows it shapes, by short name."""
    column: str
    """The sensitive column, by short name."""
    sensitivity: list[str]
    """Its kinds of sensitive data."""

    def to_dict(self) -> dict[str, Any]:
        return {'relation': self.relation, 'column': self.column, 'sensitivity': self.sensitivity}


@dataclass
class UnmaskedCopy:
    """A column of a table that holds, in clear, the values of a column the warehouse masks."""

    column: str
    """The copy, by short name."""
    column_id: str
    """Its id."""
    source: str
    """The masked column it comes from, by short name."""
    policy: str
    """The source's masking policy."""

    def to_dict(self) -> dict[str, Any]:
        return {'column': self.column, 'source': self.source, 'policy': self.policy}


@dataclass
class Uncertain:
    """A relation fed by one holding sensitive data whose column lineage Konstruct does not know,
    so it may hold that data."""

    relation: str
    """The relation fed, by short name."""
    source: str
    """The relation with sensitive columns that feeds it."""
    sensitivity: list[str]
    """The kinds of data the source holds."""
    relation_id: str = field(default='', repr=False)
    """The id of the relation fed, for the gate."""

    def to_dict(self) -> dict[str, Any]:
        return {'relation': self.relation, 'source': self.source, 'sensitivity': self.sensitivity}


@dataclass
class Sensitivity:
    """Where sensitive data is, where the lineage takes it and where a transform stops it."""

    columns: list[SensitiveColumn] = field(default_factory=list)
    """Every sensitive column, by relation."""
    sanitised: list[Sanitised] = field(default_factory=list)
    """Reads of sensitive columns that a transform stops."""
    exposures: list[ExposureFlow] = field(default_factory=list)
    """Exposures that show sensitive data."""
    references: list[Reference] = field(default_factory=list)
    """Sensitive columns that only shape another relation's rows."""
    unmasked: list[UnmaskedCopy] = field(default_factory=list)
    """Tables that copy a masked column in clear."""
    uncertain: list[Uncertain] = field(default_factory=list)
    """Relations that may hold sensitive data through lineage Konstruct does not know."""
    settings: Settings = field(default_factory=Settings)
    """The settings used."""
    subject: str | None = None
    """The table or column asked about, when the result is about one."""
    seconds: float = 0.0
    """Time taken."""

    @property
    def marked(self) -> list[SensitiveColumn]:
        """The columns marked sensitive themselves."""
        return [c for c in self.columns if c.marks]

    @property
    def unheld(self) -> list[SensitiveColumn]:
        """Sensitive columns of built relations that nothing marks to hold the data."""
        return [c for c in self.columns if c.built and not c.held]

    @property
    def unheld_exposures(self) -> list[ExposureFlow]:
        """Exposures showing sensitive data that nothing marks to hold it."""
        return [e for e in self.exposures if not e.held]

    def column(self, ref: str) -> SensitiveColumn | None:
        """The sensitive column with this id or short name, if it is one."""
        return next((c for c in self.columns if ref in (c.column, c.label)), None)

    def summary(self) -> str:
        """One sentence on what was found."""
        if not self.columns:
            subject = f' in {self.subject}' if self.subject else ''
            return f'No sensitive columns{subject}: nothing marks a column sensitive, or nothing computed from one.'
        relations = len({c.relation_id for c in self.columns})
        parts = [
            f'{_plural(len(self.columns), "sensitive column")} in {_plural(relations, "relation")} '
            f'({len(self.marked)} marked, {len(self.columns) - len(self.marked)} through lineage)'
        ]
        if self.unheld:
            parts.append(f'{len(self.unheld)} in built relations not marked to hold the data')
        if self.exposures:
            parts.append(
                f'{_plural(len(self.exposures), "exposure")} showing it'
                + (f', {len(self.unheld_exposures)} not marked to' if self.unheld_exposures else '')
            )
        if self.sanitised:
            parts.append(f'{_plural(len(self.sanitised), "read")} stopped by a sanitiser')
        if self.unmasked:
            parts.append(
                f'{_plural(len(self.unmasked), "unmasked copy", "unmasked copies")} of masked columns'
            )
        return '; '.join(parts) + '.'

    def to_dict(self) -> dict[str, Any]:
        return {
            'subject': self.subject,
            'summary': self.summary(),
            'settings': self.settings.to_dict(),
            'columns': [c.to_dict() for c in self.columns],
            'unheld': [c.label for c in self.unheld],
            'sanitised': [s.to_dict() for s in self.sanitised],
            'exposures': [e.to_dict() for e in self.exposures],
            'references': [r.to_dict() for r in self.references],
            'unmasked': [u.to_dict() for u in self.unmasked],
            'uncertain': [u.to_dict() for u in self.uncertain],
            'seconds': round(self.seconds, 3),
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import sensitivity_lines, show

        return show(sensitivity_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f'{count} {word if count == 1 else plural or word + "s"}'


# The analysis


@dataclass
class _State:
    """What the walk knows about one sensitive column."""

    labels: set[str]
    sources: set[str]
    previous: str | None
    transform: str
    masked: str | None
    """The nearest masked column upstream (the column itself when it is masked)."""
    part: bool = False
    """Whether the column only holds a sensitive field (a struct with a sensitive field), so its
    other fields are not sensitive because of it."""


# How much of a value each kind of step passes on, the most first: where two paths reach a column,
# the one that passes more is the one it reports.
_TRANSFORMS = {'marked': 3, 'direct': 2, 'transformed': 1, 'pseudonymised': 0}


def _tag(name: str, value: str, extra: frozenset[str]) -> str | None:
    """What a warehouse tag says the column holds, or None when it marks nothing."""
    lowered = name.strip().lower()
    last = lowered.rsplit('.', 1)[-1]
    # Databricks' classification tags are class.<kind> (class.email_address).
    databricks = lowered.startswith('class.') and lowered != 'class.'
    known = databricks or _normal(lowered) in TAGS | extra or _normal(last) in TAGS | extra
    if not known or _normal(value) in _NOT_SENSITIVE:
        return None
    if databricks:
        return lowered.removeprefix('class.')
    if (_normal(last) in _CONTAINERS or _normal(lowered) in _CONTAINERS) and value.strip():
        return value.strip().lower()
    return last


def column_marks(graph: Graph, settings: Settings | None = None) -> dict[str, list[Mark]]:
    """Every column marked sensitive, by id, with what marks it."""
    settings = settings or Settings()
    extra = frozenset(_normal(t) for t in settings.tags)
    found: dict[str, list[Mark]] = {}
    for node in graph.nodes.values():
        if node.kind == 'term':
            sensitivity = node.attrs.get('sensitivity')
            if not isinstance(sensitivity, str) or not sensitivity:
                continue
            pack = node.attrs.get('registry')
            by = f'term {node.name!r}' + (f' (ontology pack {pack})' if isinstance(pack, str) else '')
            for edge in graph.out_edges(node.id, 'defines'):
                target = graph.nodes.get(edge.target)
                if target is not None and target.kind == 'column':
                    found.setdefault(target.id, []).append(Mark(sensitivity, by))
        elif node.kind == 'column':
            tags = node.attrs.get('warehouse_tags')
            if isinstance(tags, dict):
                for name, value in sorted(tags.items()):
                    label = _tag(str(name), str(value or ''), extra)
                    if label is not None:
                        shown = f'{name} = {value}' if value else str(name)
                        found.setdefault(node.id, []).append(Mark(label, f'warehouse tag {shown}'))
            policy = node.attrs.get('masking_policy')
            if isinstance(policy, str) and policy:
                found.setdefault(node.id, []).append(Mark('masked', f'masking policy {policy}'))
    return found


def shown(graph: Graph, node: Node) -> str:
    """A relation's or a column's short name, or, where another relation has the same short name,
    its warehouse name (``raw.people.email`` beside ``mart.people.email``)."""
    if node.kind == 'column':
        parent = graph.parent_of(node)
        return f'{shown(graph, parent)}.{node.name}' if parent is not None else node.name
    label = graph.label(node)
    if node.kind in RELATION_KINDS and node.relation:
        names = graph.indexed('short relation names', _short_names, basis='relations')
        if names.get(label.lower(), 0) > 1:
            return node.relation
    return label


def _short_names(graph: Graph) -> dict[str, int]:
    """How many physical relations have each short name."""
    keys: dict[str, set[str]] = {}
    for node in graph.relations():
        keys.setdefault(graph.label(node).lower(), set()).add(node.relation or node.id)
    return {name: len(found) for name, found in keys.items()}


def _reads(column: exp.Column, relation: str | None, name: str) -> bool:
    """Whether ``column`` in an expression reads ``relation``'s column ``name``: its full name,
    or its name under a qualifier that ends the relation's key, or its name alone."""
    if isinstance(column.this, exp.Star):
        return False
    text = '.'.join(p.name.lower() for p in column.parts)
    if text == name:
        return True
    if not text.endswith('.' + name):
        return False
    # A reading of the relation (``"orders#2"``) reads its columns.
    prefix = split_reading(text[: -len(name) - 1])[0]
    return relation is None or relation == prefix or relation.endswith('.' + prefix)


def _unwrap(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _literal_count(node: exp.Expr) -> int | None:
    node = _unwrap(node)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(str(node.this))
        except ValueError:
            return None
    return None


def _counted_key(node: exp.Expr) -> exp.Column | None:
    """The column ``node`` counts the distinct values of (``COUNT(DISTINCT customer_id)``), or
    None for any other count, or anything else."""
    node = _unwrap(node)
    if not isinstance(node, exp.Count) or isinstance(node.parent, exp.Window):
        return None
    argument = node.this
    if isinstance(argument, exp.Distinct) and len(argument.expressions) == 1:
        column = _unwrap(argument.expressions[0])
        return column if isinstance(column, exp.Column) else None
    return None


def _group_size(condition: exp.Expr, keyed: Callable[[exp.Column], bool]) -> int | None:
    """The fewest people a condition such as ``COUNT(DISTINCT customer_id) >= 10`` requires of
    each group, where ``keyed`` says the column counted is a key: a count of rows is not one,
    since one person can have many."""
    left, right = condition.this, condition.expression

    def counts(node: exp.Expr) -> bool:
        column = _counted_key(node)
        return column is not None and keyed(column)

    if isinstance(condition, exp.GTE) and counts(left):
        return _literal_count(right)
    if isinstance(condition, exp.GT) and counts(left):
        found = _literal_count(right)
        return None if found is None else found + 1
    if isinstance(condition, exp.LTE) and counts(right):
        return _literal_count(left)
    if isinstance(condition, exp.LT) and counts(right):
        found = _literal_count(left)
        return None if found is None else found + 1
    return None


# What may not be inside an aggregate that stops a value: anything that picks some rows or
# values over others, or reads another column.
_PICKS = (exp.Case, exp.If, exp.Predicate, exp.Filter, exp.Subquery, exp.Select, exp.Window, exp.Nullif)


class _Analysis:
    def __init__(
        self, graph: Graph, settings: Settings, unmarked: Iterable[str] = (), untrusted: Iterable[str] = ()
    ) -> None:
        self.graph = graph
        self.settings = settings
        self.unmarked = set(unmarked)
        self.dialect = graph.meta.get('dialect') if isinstance(graph.meta.get('dialect'), str) else None
        self.sanitisers = frozenset(_normal(s) for s in settings.sanitisers)
        self.trees: dict[str, exp.Expr | None] = {}
        self.groups: dict[str, int | None] = {}
        self.holders: dict[str, str | None] = {}
        self.marks = column_marks(graph, settings)
        self.foreign: dict[str, set[str]] | None = None
        self.untrusted = frozenset(untrusted)
        self.term_holders: dict[str, str] = {}
        for node in graph.nodes.values():
            if node.kind == 'term' and isinstance(node.attrs.get('sensitivity'), str):
                for edge in graph.out_edges(node.id, 'defines'):
                    target = graph.nodes.get(edge.target)
                    if target is not None and target.kind != 'column' and target.id not in self.unmarked:
                        self.term_holders.setdefault(target.id, f'term {node.name!r}')
        # Each column's nested fields, and the structs each nested field is in.
        self.fields: dict[str, list[str]] = {}
        self.structs: dict[str, list[str]] = {}
        for node in graph.nodes.values():
            if node.kind != 'column' or '.' not in node.name or not node.parent:
                continue
            parts = node.name.split('.')
            for size in range(len(parts) - 1, 0, -1):
                ancestor = _column_id(node.parent, '.'.join(parts[:size]))
                if ancestor in graph.nodes:
                    self.fields.setdefault(ancestor, []).append(node.id)
                    self.structs.setdefault(node.id, []).append(ancestor)

    # What stops a value

    def calls(self, expression: str) -> bool:
        """Whether an expression may call a function that stops or hashes a value: a cheap look
        at its text, before it is parsed."""
        names = {_normal(n) for n in _CALL.findall(expression)}
        return not names.isdisjoint(HASHES | GROUPED) or not names.isdisjoint(self.sanitisers)

    def tree(self, column: Node) -> exp.Expr | None:
        if column.id not in self.trees:
            expression = column.attrs.get('expression')
            tree: exp.Expr | None = None
            if isinstance(expression, str):
                try:
                    tree = sqlglot.parse_one(expression, read=self.dialect)
                except PARSE_ERRORS:
                    tree = None
            self.trees[column.id] = tree
        return self.trees[column.id]

    def group_size(self, relation: Node | None) -> int | None:
        """The fewest distinct keys every group of ``relation``'s statement holds, when its SQL
        proves it: it reads one relation, with no join and no ``WHERE``, at most one ``GROUP BY``
        and that on no identifier, sensitive column, ``ROLLUP``, ``CUBE`` or ``GROUPING SETS``, and
        every ``HAVING`` condition is a count of a key's distinct values of at least that. The
        conditions of every step are merged, so anything less leaves room for a group of one
        person's rows."""
        if relation is None:
            return None
        if relation.id not in self.groups:
            self.groups[relation.id] = self._group_size(relation)
        return self.groups[relation.id]

    def _group_size(self, relation: Node) -> int | None:
        graph = self.graph
        read = {
            e.source
            for e in graph.in_edges(relation.id, 'feeds')
            if e.attrs.get('role') != 'reference'
            and e.source in graph.nodes
            and graph.nodes[e.source].kind in RELATION_KINDS
        }
        if len(read) != 1:
            return None
        conditions = [str(c) for c in relation.attrs.get('conditions', []) or []]
        groups = [c for c in conditions if c.startswith('GROUP BY:')]
        if len(groups) > 1:
            return None
        found: int | None = None
        for condition in conditions:
            label, sep, text = condition.partition(': ')
            if not sep:
                return None
            try:
                tree = sqlglot.parse_one(text, read=self.dialect)
            except PARSE_ERRORS:
                return None
            if label == 'GROUP BY':
                if (
                    tree.find(exp.Rollup, exp.Cube, exp.GroupingSets) is not None
                    or 'GROUPING' in text.upper()
                ):
                    return None
                for column in tree.find_all(exp.Column):
                    if self.identifies(column):
                        return None
                continue
            if label != 'HAVING':
                # A join, a filter, a QUALIFY: each can leave a group of one person's rows.
                return None
            tree = _unwrap(tree)
            for part in list(tree.flatten()) if isinstance(tree, exp.And) else [tree]:
                part = _unwrap(part)
                size = _group_size(part, self.keyed) if isinstance(part, exp.Binary) else None
                if size is None:
                    return None
                found = size if found is None else max(found, size)
        return found

    def identifies(self, column: exp.Column) -> bool:
        """Whether a grouping column may pick out one person: an identifier, a unique column, a
        sensitive one, or one Konstruct cannot find."""
        from konstruct.semantics.ontology import id_like

        if id_like(column.name.lower()):
            return True
        parts = [p.name for p in column.parts]
        if len(parts) < 2:
            return True
        node = self.resolve(column)
        return node is None or self.unique_somewhere(node) or node.id in self.marks

    def keyed(self, column: exp.Column) -> bool:
        """Whether a counted column identifies the people (or other objects) its rows are about,
        so that its distinct values count them rather than rows: a foreign key to another
        relation's entity, and never the input's own row or primary key, which is unique per row
        and counts nothing more than rows do (one person can have any number of payments)."""
        node = self.resolve(column)
        if node is None or self.unique(node) or node.attrs.get('ossie_key'):
            return False
        relation = self.graph.parent_of(node)
        primary = relation.attrs.get('primary_key') if relation is not None else None
        if isinstance(primary, list) and node.name.lower() in {str(p).lower() for p in primary}:
            return False
        if self.foreign is None:
            self.foreign = foreign_keys(self.graph, self.untrusted)
        # A key of people is one whose own relation holds their personal data (users with a
        # name), not a key of orders or payments.
        return any(self.personal(key) for key in self.foreign.get(node.id, ()))

    def personal(self, key: str) -> bool:
        """Whether an entity key's relation has a column marked sensitive, by a mark that is not
        the change's own."""
        parent = self.graph.nodes[key].parent
        return any(
            c.id in self.marks and c.id not in self.unmarked for c in self.graph.columns_of(parent or '')
        )

    def resolve(self, column: exp.Column) -> Node | None:
        parts = [p.name for p in column.parts]
        for cut in range(len(parts) - 1, 0, -1):
            relation = self.graph.node_for_reading('.'.join(parts[:cut]))
            if relation is not None:
                return self.graph.column(relation.id, '.'.join(parts[cut:]))
        return None

    def unique(self, node: Node) -> bool:
        """Whether a test shows the column unique on every row (not one with a ``where``)."""
        from konstruct.semantics.ontology import unique_tested

        return any(
            node in unique_tested(self.graph, test)
            for e in self.graph.in_edges(node.id, 'tests')
            if (test := self.graph.nodes.get(e.source)) is not None
        )

    def unique_somewhere(self, node: Node) -> bool:
        """Whether a ``unique`` test, with a ``where`` or not, holds on the column: on the rows it
        selects (``is_current``), a group of the column's value holds one person's rows."""
        return any(
            (test := self.graph.nodes.get(e.source)) is not None and test.attrs.get('test') == 'unique'
            for e in self.graph.in_edges(node.id, 'tests')
        )

    def proven(self, aggregate: exp.Expr, tree: exp.Expr, read: exp.Column, size: int | None) -> bool:
        """Whether an aggregate over the read stops its value (see the module documentation)."""
        if size is None or size < self.settings.k or not isinstance(aggregate, exp.AggFunc):
            return False
        if (
            isinstance(aggregate.parent, (exp.Window, exp.Filter))
            or len(list(tree.find_all(exp.AggFunc))) != 1
        ):
            return False
        if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Star):
            return False
        if any(isinstance(n, _PICKS) for n in aggregate.find_all(*_PICKS) if n is not aggregate):
            return False
        reads = {'.'.join(p.name.lower() for p in c.parts) for c in aggregate.find_all(exp.Column)}
        return reads == {'.'.join(p.name.lower() for p in read.parts)}

    def guard(self, read: exp.Column, tree: exp.Expr, size: int | None) -> tuple[str, str] | None:
        """What one read of a value goes through: ``('stopped', how)`` for a sanitiser or a proven
        aggregate, ``('pseudonymised', how)`` for a hash, or None when it passes as it is."""
        hashed: str | None = None
        node = read.parent
        while node is not None:
            if isinstance(node, exp.Func):
                name = _function(node)
                label = (str(node.name) if isinstance(node, exp.Anonymous) else node.sql_name()).upper()
                if name in self.sanitisers:
                    return ('stopped', label)
                if name in GROUPED and self.proven(node, tree, read, size):
                    return ('stopped', f'{label} over groups of at least {size} distinct keys')
                if name in HASHES and hashed is None:
                    hashed = label
            node = node.parent
        return ('pseudonymised', hashed) if hashed else None

    def classify(self, column: Node, upstream: Node) -> tuple[str, str | None]:
        """What ``column``'s expression does with ``upstream``'s value: ``stopped`` when every read
        of it is stopped, ``pseudonymised`` when every read is stopped or hashed, else
        ``carried``; with how, for the first two."""
        expression = column.attrs.get('expression')
        if not isinstance(expression, str) or not self.calls(expression):
            # Most columns call nothing that could stop or hash a value: no need to parse them.
            return ('carried', None)
        tree = self.tree(column)
        if tree is None:
            return ('carried', None)
        parent = self.graph.parent_of(upstream)
        relation = parent.relation if parent is not None else None
        name = upstream.name.lower()
        reads = [c for c in tree.find_all(exp.Column) if _reads(c, relation, name)]
        if not reads:
            return ('carried', None)
        # A UNION's branches or a subquery are steps of their own, which the conditions merge.
        nested = (
            any(str(n.name).upper() == 'UNION' for n in tree.find_all(exp.Anonymous))
            or tree.find(exp.Select, exp.Subquery) is not None
        )
        size = None if nested else self.group_size(self.graph.parent_of(column))
        found = [self.guard(read, tree, size) for read in reads]
        if any(f is None for f in found):
            return ('carried', None)
        stops = [f[1] for f in found if f is not None and f[0] == 'stopped']
        if len(stops) == len(found):
            return ('stopped', stops[0])
        hashes = [f[1] for f in found if f is not None and f[0] == 'pseudonymised']
        return ('pseudonymised', hashes[0])

    def counted(self, column: Node, upstream: Node) -> bool:
        """Whether every read of ``upstream`` in ``column``'s expression is counted: the count
        says how many rows have the value, and holds none of it."""
        tree = self.tree(column)
        if tree is None:
            return False
        parent = self.graph.parent_of(upstream)
        relation = parent.relation if parent is not None else None
        reads = [c for c in tree.find_all(exp.Column) if _reads(c, relation, upstream.name.lower())]
        return bool(reads) and all(read.find_ancestor(exp.Count) is not None for read in reads)

    def sanitiser(self, column: Node, upstream: Node) -> str | None:
        """What stops ``upstream``'s value from reaching ``column``, or None."""
        kind, how = self.classify(column, upstream)
        return how if kind == 'stopped' else None

    # Holders

    def holder(self, node: Node) -> str | None:
        """What marks a relation or an exposure to hold sensitive data, or None."""
        if node.id not in self.holders:
            self.holders[node.id] = self._holder(node)
        return self.holders[node.id]

    def _holder(self, node: Node) -> str | None:
        if node.id in self.term_holders:
            return self.term_holders[node.id]
        names = {node.name.lower(), self.graph.label(node).lower(), node.id.lower()}
        if node.relation:
            names.add(node.relation.lower())
        for pattern in self.settings.holders:
            wanted = pattern.lower()
            if any(fnmatchcase(name, wanted) for name in names):
                return f'[sensitivity] holders {pattern!r}'
        return None

    # The walk

    def walk(self) -> tuple[dict[str, list[Mark]], dict[str, _State], dict[tuple[str, str], str]]:
        """The marks, what the walk down the lineage knows of each sensitive column, and the reads
        a sanitiser or a proven aggregate stops, by (column, upstream column)."""
        graph = self.graph
        marks = self.marks
        state: dict[str, _State] = {}
        sanitised: dict[tuple[str, str], str] = {}
        queue: deque[str] = deque()

        def reach(column: Node, known: _State, previous: str, transform: str, part: bool = False) -> None:
            policy = column.attrs.get('masking_policy')
            masked = column.id if isinstance(policy, str) and policy else known.masked
            found = state.get(column.id)
            if found is None:
                state[column.id] = _State(
                    set(known.labels), set(known.sources), previous, transform, masked, part
                )
                queue.append(column.id)
                return
            grows = not known.labels <= found.labels or not known.sources <= found.sources
            if _TRANSFORMS[transform] > _TRANSFORMS[found.transform]:
                found.transform, found.previous, grows = transform, previous, True
            if found.part and not part:
                found.part, grows = False, True
            found.labels |= known.labels
            found.sources |= known.sources
            found.masked = found.masked or masked
            if grows:
                queue.append(column.id)

        for column_id in sorted(marks):
            node = graph.nodes[column_id]
            policy = node.attrs.get('masking_policy')
            state[column_id] = _State(
                {m.sensitivity for m in marks[column_id]},
                {column_id},
                None,
                'marked',
                column_id if isinstance(policy, str) and policy else None,
            )
            queue.append(column_id)
        while queue:
            upstream_id = queue.popleft()
            upstream = graph.nodes[upstream_id]
            known = state[upstream_id]
            passed = 'direct' if known.transform in ('marked', 'direct') else known.transform
            for edge in sorted(graph.in_edges(upstream_id, 'derived_from'), key=lambda e: e.source):
                column = graph.nodes.get(edge.source)
                if column is None or column.kind != 'column':
                    continue
                kind, how = self.classify(column, upstream)
                if kind == 'stopped':
                    sanitised.setdefault((column.id, upstream_id), str(how))
                    continue
                if kind == 'pseudonymised' or known.transform == 'pseudonymised':
                    transform = 'pseudonymised'
                elif edge.attrs.get('transform') == 'direct' and passed == 'direct':
                    transform = 'direct'
                else:
                    transform = 'transformed'
                reach(column, known, upstream_id, transform)
            # A sensitive struct's fields hold its data, and a struct holds its fields' data.
            if not known.part:
                for field_id in self.fields.get(upstream_id, []):
                    reach(graph.nodes[field_id], known, upstream_id, passed)
            for struct_id in self.structs.get(upstream_id, []):
                reach(graph.nodes[struct_id], known, upstream_id, passed, part=True)
        return marks, state, sanitised

    def run(self) -> Sensitivity:
        marks, state, sanitised = self.walk()
        result = Sensitivity(settings=self.settings)
        result.columns = [self.column(i, state, marks) for i in state]
        result.columns.sort(key=lambda c: (c.relation.lower(), c.relation_id, c.label.lower()))
        result.sanitised = sorted(
            (
                Sanitised(self.label(c), self.label(u), by)
                for (c, u), by in sanitised.items()
                if c not in state
            ),
            key=lambda s: (s.column, s.source),
        )
        result.exposures = self.exposures(state)
        result.references = self.references(state)
        result.unmasked = self.unmasked(state)
        result.uncertain = self.uncertain(state)
        return result

    def label(self, node_id: str) -> str:
        node = self.graph.nodes.get(node_id)
        return shown(self.graph, node) if node is not None else node_id

    def path(self, column_id: str, state: dict[str, _State]) -> list[str]:
        path: list[str] = []
        seen: set[str] = set()
        current: str | None = column_id
        while current is not None and current not in seen:
            seen.add(current)
            path.append(self.label(current))
            current = state[current].previous
        return path[::-1]

    def column(
        self, column_id: str, state: dict[str, _State], marks: dict[str, list[Mark]]
    ) -> SensitiveColumn:
        graph = self.graph
        node = graph.nodes[column_id]
        relation = graph.parent_of(node)
        known = state[column_id]
        own = marks.get(column_id, [])
        held = 'marked' if own and column_id not in self.unmarked else None
        if held is None and relation is not None:
            held = self.holder(relation)
        policy = node.attrs.get('masking_policy')
        return SensitiveColumn(
            column=column_id,
            label=self.label(column_id),
            relation=self.label(relation.id) if relation is not None else '',
            relation_id=relation.id if relation is not None else '',
            sensitivity=sorted(known.labels),
            marks=own,
            sources=sorted(self.label(s) for s in known.sources if s != column_id),
            path=self.path(column_id, state),
            transform=known.transform,
            built=relation is not None and graph.built(relation),
            held=held,
            masking_policy=policy if isinstance(policy, str) and policy else None,
        )

    def exposures(self, state: dict[str, _State]) -> list[ExposureFlow]:
        graph = self.graph
        found: list[ExposureFlow] = []
        for node in graph.nodes.values():
            if node.kind != 'exposure':
                continue
            reads: list[str] = []
            for edge in graph.in_edges(node.id, 'feeds'):
                source = graph.nodes.get(edge.source)
                if source is None:
                    continue
                if source.kind == 'column':
                    reads += [source.id] if source.id in state else []
                elif source.kind in RELATION_KINDS and not graph.fed_by_columns_of(node.id, source.id):
                    reads += [c.id for c in graph.columns_of(source.id) if c.id in state]
            reads = sorted(set(reads))
            if reads:
                found.append(
                    ExposureFlow(
                        graph.label(node),
                        node.id,
                        sorted({label for r in reads for label in state[r].labels}),
                        [self.label(r) for r in reads],
                        reads,
                        self.holder(node),
                        node.path,
                    )
                )
        return sorted(found, key=lambda e: e.exposure.lower())

    def references(self, state: dict[str, _State]) -> list[Reference]:
        graph = self.graph
        found: list[Reference] = []
        for column_id in sorted(state):
            for edge in graph.out_edges(column_id, 'feeds'):
                if edge.attrs.get('role') == 'reference' and edge.target in graph.nodes:
                    found.append(
                        Reference(
                            self.label(edge.target), self.label(column_id), sorted(state[column_id].labels)
                        )
                    )
        return sorted(found, key=lambda r: (r.relation, r.column))

    def unmasked(self, state: dict[str, _State]) -> list[UnmaskedCopy]:
        graph = self.graph
        found: list[UnmaskedCopy] = []
        for column_id, known in sorted(state.items()):
            if known.masked is None or known.masked == column_id or known.transform == 'pseudonymised':
                continue
            relation = graph.parent_of(graph.nodes[column_id])
            if relation is None or not graph.built(relation) or not materialised(relation):
                continue
            source = graph.nodes[known.masked]
            policy = str(source.attrs.get('masking_policy'))
            found.append(UnmaskedCopy(self.label(column_id), column_id, self.label(known.masked), policy))
        return found

    def uncertain(self, state: dict[str, _State]) -> list[Uncertain]:
        """Relations fed by one with sensitive columns, without a column edge or a reference
        from it: what they hold of it is not known."""
        graph = self.graph
        holding: dict[str, set[str]] = {}
        for column_id, known in state.items():
            parent = graph.nodes[column_id].parent
            if parent:
                holding.setdefault(parent, set()).update(known.labels)
        found: list[Uncertain] = []
        for relation_id in sorted(holding):
            for target in self.fed(relation_id):
                if self.blind(relation_id, target):
                    found.append(
                        Uncertain(
                            self.label(target), self.label(relation_id), sorted(holding[relation_id]), target
                        )
                    )
        return found

    def blind(self, source: str, target: str) -> bool:
        """Whether ``target`` reads ``source`` with no column edge or reference to it, so that
        what it holds of it is not known (its SQL is dynamic, or its column lineage failed)."""
        graph = self.graph
        return not any(
            e.target.startswith(source + '#')
            for c in graph.columns_of(target)
            for e in graph.out_edges(c.id, 'derived_from')
        ) and not any(
            e.target == target and e.attrs.get('role') == 'reference'
            for c in graph.columns_of(source)
            for e in graph.out_edges(c.id, 'feeds')
        )

    def blind_reads(self, targets: Iterable[str]) -> list[tuple[str, str]]:
        """Each relation that one of ``targets`` reads blind (see :meth:`blind`), as ``(source,
        target)``, whether or not the source holds sensitive data now."""
        wanted = set(targets)
        return [
            (relation.id, target)
            for relation in sorted(self.graph.relations(), key=lambda n: n.id)
            for target in self.fed(relation.id)
            if target in wanted and self.blind(relation.id, target)
        ]

    def fed(self, relation_id: str) -> list[str]:
        """The relations ``relation_id`` feeds, directly or through a task that reads it."""
        graph = self.graph
        found: list[str] = []
        for edge in graph.out_edges(relation_id, 'feeds'):
            target = graph.nodes.get(edge.target)
            if target is None or edge.attrs.get('role') == 'reference':
                continue
            if target.kind in RELATION_KINDS:
                found.append(target.id)
            elif target.kind == 'task':
                found += [
                    e.target
                    for e in graph.out_edges(target.id, 'feeds')
                    if e.target in graph.nodes and graph.nodes[e.target].kind in RELATION_KINDS
                ]
        return sorted(set(found) - {relation_id})


def materialised(relation: Node) -> bool:
    """Whether a relation holds its own copy of the data (a table, or a materialised view) rather
    than reading it as a view does."""
    if relation.attrs.get('materialized_view'):
        return True
    kind = str(relation.attrs.get('table_type') or '').lower()
    materialized = str(relation.attrs.get('materialized') or '').lower()
    return not (kind == 'view' or materialized in ('view', 'ephemeral'))


def sensitivity(
    graph: Graph,
    settings: Settings | None = None,
    ref: str | Node | None = None,
    *,
    unmarked: Iterable[str] = (),
) -> Sensitivity:
    """Where sensitive data is in ``graph``, where the lineage takes it and where it stops (see
    the module documentation). With ``ref`` (a table, model or column), only what concerns it:
    its sensitive columns, the reads of them that are stopped, and the exposures that show them.
    ``unmarked`` are column ids whose own marks do not count as marking them to hold the data.

    Raises:
        konstruct.QueryError: ``ref`` matches nothing, or several things.
    """
    from konstruct.analysis.query import resolve_one

    started = time.perf_counter()
    result = _Analysis(graph, settings or Settings(), unmarked).run()
    if ref is not None:
        node = resolve_one(graph, ref) if isinstance(ref, str) else ref
        ids = {node.id} | ({c.id for c in graph.columns_of(node.id)} if node.kind != 'column' else set())
        labels = {shown(graph, graph.nodes[i]) for i in ids}
        result.subject = shown(graph, node)
        result.columns = [c for c in result.columns if c.column in ids]
        result.sanitised = [s for s in result.sanitised if s.column in labels or s.source in labels]
        result.exposures = [
            e for e in result.exposures if e.exposure_id in ids or any(c in ids for c in e.column_ids)
        ]
        result.references = [r for r in result.references if r.column in labels or r.relation in labels]
        result.unmasked = [u for u in result.unmasked if u.column_id in ids or u.source in labels]
        result.uncertain = [u for u in result.uncertain if u.relation_id in ids or u.source in labels]
    result.seconds = time.perf_counter() - started
    return result


def sensitive_columns(graph: Graph, settings: Settings | None = None) -> dict[str, list[str]]:
    """Every sensitive column's id, with the kinds of data it holds: what :func:`mask` hides."""
    _, state, _ = _Analysis(graph, settings or Settings()).walk()
    return {column_id: sorted(known.labels) for column_id, known in state.items()}


GOVERNANCE = ('warehouse_tags', 'masking_policy', 'materialized_view')
"""The attributes only a warehouse gives a column or a relation, which mark data sensitive."""


def _place(relation: str) -> str:
    """A relation's table name: what every way of naming it (``w.main.people``, ``main.people``,
    ``people``) has in common."""
    return relation.rsplit('.', 1)[-1]


def _same_place(a: str, b: str) -> bool:
    """Whether two warehouse keys can name one table: equal, or one the other with its catalogue
    or schema left out, as a build from files alone leaves out what SQL does not write
    (``w.main.people``, ``main.people`` and ``people`` are one table)."""
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def carry_governance(graph: Graph, previous: Graph) -> int:
    """Give ``graph``'s nodes the warehouse governance (tags, masking policies, materialised
    views) that ``previous`` recorded for them and ``graph`` lacks, as for a graph built from files
    alone after one that read the warehouse; how many nodes gained any. A relation is found by its
    warehouse key, with or without the catalogue the warehouse adds, and a column by its relation
    and name. A tag carried over only makes more data sensitive. A masking policy is carried only
    to a column of a relation nothing in the graph builds, and only when every place that can be
    that column has the same policy: rebuilding a table (``create or replace``) drops a policy set
    on its column, so a policy the last build saw there is not one the change keeps."""
    # The previous graph's governed relations and columns, by warehouse key without catalogue.
    places: dict[tuple[str, str], list[tuple[str, Node]]] = {}
    for old in previous.nodes.values():
        if not any(k in old.attrs for k in GOVERNANCE):
            continue
        owner = previous.parent_of(old) if old.kind == 'column' else old
        if owner is None or not owner.relation:
            continue
        name = old.name.lower() if old.kind == 'column' else ''
        places.setdefault((_place(owner.relation), name), []).append((owner.relation, old))
    count = 0
    for node in graph.nodes.values():
        owner = graph.parent_of(node) if node.kind == 'column' else node
        if owner is None or not owner.relation:
            continue
        name = node.name.lower() if node.kind == 'column' else ''
        found = [
            old
            for key, old in places.get((_place(owner.relation), name), [])
            if _same_place(key, owner.relation)
        ]
        if not found:
            continue
        rebuilt = node.kind == 'column' and graph.built(owner)
        missing: dict[str, Any] = {}
        tags: dict[str, Any] = {}
        for old in found:
            tags.update(old.attrs.get('warehouse_tags') or {})
        if tags and 'warehouse_tags' not in node.attrs:
            missing['warehouse_tags'] = tags
        policies = {str(o.attrs['masking_policy']) for o in found if o.attrs.get('masking_policy')}
        if len(policies) == 1 and 'masking_policy' not in node.attrs and not rebuilt:
            missing['masking_policy'] = policies.pop()
        if any(o.attrs.get('materialized_view') for o in found) and 'materialized_view' not in node.attrs:
            missing['materialized_view'] = True
        if missing:
            node.attrs.update(missing)
            count += 1
    if count:
        graph.changed()
    return count


def joined_columns(graph: Graph, condition: str) -> list[tuple[Node, Node]]:
    """The pairs of columns a ``JOIN ON`` condition equates."""
    label, sep, text = condition.partition(': ')
    if not sep or 'JOIN' not in label:
        return []
    dialect = graph.meta.get('dialect')
    try:
        tree = sqlglot.parse_one(text, read=dialect if isinstance(dialect, str) else None)
    except PARSE_ERRORS:
        return []

    def column(node: exp.Expr) -> Node | None:
        if not isinstance(node, exp.Column):
            return None
        parts = [p.name for p in node.parts]
        for cut in range(len(parts) - 1, 0, -1):
            relation = graph.node_for_reading('.'.join(parts[:cut]))
            if relation is not None:
                return graph.column(relation.id, '.'.join(parts[cut:]))
        return None

    pairs = []
    for equality in tree.find_all(exp.EQ):
        left, right = column(equality.this), column(equality.expression)
        if left is not None and right is not None:
            pairs.append((left, right))
    return pairs


def _from(path: str | None, untrusted: frozenset[str]) -> bool:
    """Whether a node defined in ``path`` comes from a file in ``untrusted``."""
    if not path or not untrusted:
        return False
    wanted = path.replace('\\', '/').removeprefix('./')
    return any(f == wanted or f.endswith('/' + wanted) for f in untrusted)


def entity_keys(graph: Graph, untrusted: frozenset[str] = frozenset()) -> set[str]:
    """The columns that key an entity, by what says so outside the files in ``untrusted``: a
    ``unique`` test that holds on every row (:func:`konstruct.semantics.ontology.unique_tested`), a
    single-column primary key or identifier field, an Ossie key, or a term of kind ``entity`` bound
    to the column. A primary key or identifier field does not record the
    file that declares it, so while ``untrusted`` holds a YAML file or the relation's own file,
    none counts."""
    from konstruct.semantics.ontology import unique_tested

    keys: set[str] = set()
    declared = not any(f.endswith(('.yml', '.yaml')) for f in untrusted)
    for node in graph.nodes.values():
        if node.kind == 'test' and not _from(node.path, untrusted):
            keys |= {column.id for column in unique_tested(graph, node)}
        elif node.kind == 'column' and node.attrs.get('ossie_key'):
            if not _from(str(node.attrs['ossie_key']), untrusted):
                keys.add(node.id)
        elif node.kind in RELATION_KINDS and declared and not _from(node.path, untrusted):
            for attr in ('primary_key', 'identifier_fields'):
                names = node.attrs.get(attr)
                if isinstance(names, list) and len(names) == 1:
                    column = graph.column(node.id, str(names[0]))
                    if column is not None:
                        keys.add(column.id)
        elif node.kind == 'term' and node.attrs.get('kind') == 'entity' and not _from(node.path, untrusted):
            keys |= {e.target for e in graph.out_edges(node.id, 'defines') if e.target in graph.nodes}
    return {k for k in keys if graph.nodes[k].kind == 'column'}


def foreign_keys(graph: Graph, untrusted: frozenset[str] = frozenset()) -> dict[str, set[str]]:
    """Each column that refers to another relation's entity key, with the keys it refers to: the
    referring side of a dbt ``relationships`` test, of an ontology pack's or an Ossie model's
    relationship, or of a join that equates the column with an entity's key in another relation.
    Its distinct values count the objects the key is of. A test, a join, an entity term or an
    Ossie model in a file in ``untrusted`` (a change under review) is not taken as evidence."""
    keys = entity_keys(graph, untrusted)
    found: dict[str, set[str]] = {}

    def add(column: str, key: str) -> None:
        if (
            key in keys
            and column not in keys
            and column in graph.nodes
            and graph.nodes[column].parent != graph.nodes[key].parent
        ):
            found.setdefault(column, set()).add(key)

    for node in graph.nodes.values():
        if (
            node.kind == 'test'
            and node.attrs.get('test') == 'relationships'
            and not _from(node.path, untrusted)
        ):
            edges = graph.out_edges(node.id, 'tests')
            targets = [e.target for e in edges if e.attrs.get('role') == 'relationship target']
            for edge in edges:
                if edge.attrs.get('role') is None:
                    for target in targets:
                        add(edge.target, target)
    ossie = any(f.endswith(('.ossie.yaml', '.ossie.yml')) for f in untrusted)
    relationships = list(graph.meta.get('pack_relationships') or [])
    if not ossie:
        relationships += list(graph.meta.get('ossie_relationships') or [])
    for item in relationships:
        if isinstance(item, dict) and isinstance(item.get('from'), str) and isinstance(item.get('to'), str):
            add(item['from'], item['to'])
    for relation in graph.relations():
        if _from(relation.path, untrusted):
            continue
        for condition in relation.attrs.get('conditions', []) or []:
            for left, right in joined_columns(graph, str(condition)):
                add(left.id, right.id)
                add(right.id, left.id)
    return found


def mask(sensitivity: list[str]) -> str:
    """What a sensitive value is shown as: ``<masked: personal>``."""
    return f'<masked: {", ".join(sensitivity) or "sensitive"}>'


# The gate

FLOW_FIX = (
    'Leave the sensitive column out, stop it with a sanitiser or an aggregate over groups of at least '
    'k distinct keys (HAVING COUNT(DISTINCT customer_id) >= k), or have its owner add the model or exposure to [sensitivity] holders in konstruct.toml '
    'in a change of its own.'
)
UNMASKED_FIX = (
    'Give the column a masking policy of its own, materialise the model as a view, or leave the column out.'
)
REMOVED_FIX = "Keep the mark, or have the data's owner approve removing it."


def gate_findings(context: CheckContext) -> list[Finding]:
    """The gate's sensitive-data findings for a change: a column or an exposure the change alters
    or adds that receives sensitive data and is not marked to hold it, a relation the change
    alters that is fed sensitive data through SQL whose columns are not known, a table the change
    alters that copies a masked column in clear, and a sensitivity mark the change takes away (in
    definitions.yml, or by turning an ontology pack off).

    Nothing the change itself does marks what it alters to hold the data. The settings are the
    strictest of the base's and the working tree's, a base whose settings cannot be read (no
    configuration, an invalid one, a commit a shallow clone lacks) counting as one with none; a
    column the change adds or alters is not held because of its own marks; and a term the change
    adds or rebinds marks nothing to hold the data. A mark the change makes disappear, rather
    than removes, is judged against the base, built from its files when the head shows a sign of one."""
    from konstruct.analysis.gate import _changed_columns, _name
    from konstruct.plugins.api import Finding

    graph, diff = context.graph, context.diff
    root = graph.meta.get('root')
    head = base = None
    ref = diff.base_commit or diff.base
    if isinstance(root, str):
        head = settings_at(Path(root), None)
        base = settings_at(Path(root), ref)
    settings = (head or Settings()).strictest(base or Settings())
    changed = _changed_columns(graph, diff)
    unmarked: set[str] = set(changed)
    for change in diff.definitions:
        for mark in change.marked:
            unmarked |= {n.id for n in graph.resolve(mark)}
    # A join or a test the change adds is no evidence that a key counts people: the files it
    # changes are judged, not trusted.
    analysis = _Analysis(graph, settings, unmarked, untrusted=diff.files)
    result = analysis.run()
    # The relations the change touches: those the diff compares, and every relation a changed
    # file builds, as the diff leaves out a statement whose column lineage fails.
    touched = {r.node_id: r.path for r in diff.relations if r.node_id}
    for built in graph.relations():
        where = _changed_file(diff.files, built.path) or _changed_file(
            diff.files, built.attrs.get('sql_path')
        )
        if where is not None and built.id not in touched:
            touched[built.id] = built.path or where
    found: list[Finding] = []
    by_relation: dict[str, list[SensitiveColumn]] = {}
    for column in result.unheld:
        if column.column in changed:
            by_relation.setdefault(column.relation_id, []).append(column)
    for relation_id, columns in by_relation.items():
        names = ', '.join(_name(graph, c.column, c.label) for c in columns[:3])
        more = f' and {len(columns) - 3} more' if len(columns) > 3 else ''
        kinds = ', '.join(sorted({s for c in columns for s in c.sensitivity}))
        sources = sorted({s for c in columns for s in c.sources})
        origin = f' from {", ".join(sources[:2])}' if sources else ''
        found.append(
            Finding(
                'sensitive-flow',
                f'{shown(graph, graph.nodes[relation_id])} receives {kinds} data{origin} in {names}{more}, '
                'and is not marked to hold it',
                changed[columns[0].column].path,
                FLOW_FIX,
            )
        )
    for exposure in result.unheld_exposures:
        reached = [c for c in exposure.column_ids if c in changed]
        defined = _changed_file(diff.files, exposure.path)
        if reached or defined:
            found.append(
                Finding(
                    'sensitive-flow',
                    f'{exposure.exposure} shows {", ".join(exposure.sensitivity)} data from '
                    f'{", ".join(exposure.columns[:3])}, and is not marked to hold it',
                    changed[reached[0]].path if reached else defined,
                    FLOW_FIX,
                )
            )
    reported: set[tuple[str, str]] = set()
    for uncertain in result.uncertain:
        where = touched.get(uncertain.relation_id)
        node = graph.nodes.get(uncertain.relation_id)
        if where is not None and node is not None and analysis.holder(node) is None:
            reported.add((uncertain.source, uncertain.relation_id))
            found.append(
                Finding(
                    'sensitive-flow',
                    f'{uncertain.relation} reads {uncertain.source}, which holds '
                    f'{", ".join(uncertain.sensitivity)} data, through SQL whose columns Konstruct does '
                    'not know, so it may hold that data, and is not marked to hold it',
                    where,
                    FLOW_FIX,
                )
            )
    for copy in result.unmasked:
        if copy.column_id in changed:
            found.append(
                Finding(
                    'unmasked-copy',
                    f'{copy.column} copies {copy.source}, which {copy.policy} masks, into a table without '
                    'a masking policy',
                    changed[copy.column_id].path,
                    UNMASKED_FIX,
                )
            )
    for change in diff.definitions:
        found += [Finding('sensitivity-removed', line, change.path, REMOVED_FIX) for line in change.unmarked]
    if isinstance(root, str):
        found += _packs_removed(Path(root), ref)
        found += _vanished(analysis, Path(root), ref, touched, reported, diff.files)
    return found


def _unbound(graph: Graph) -> list[tuple[Node, str]]:
    """Each column a sensitive term names that binds to nothing, with the term."""
    found: list[tuple[Node, str]] = []
    for node in graph.nodes.values():
        if node.kind == 'term' and node.attrs.get('sensitivity'):
            for ref in node.attrs.get('columns') or []:
                bound = [n for n in graph.resolve(str(ref)) if n.kind == 'column' or n.kind in RELATION_KINDS]
                if len(bound) != 1:
                    found.append((node, str(ref)))
    return found


def _vanished(
    analysis: _Analysis,
    root: Path,
    ref: str,
    touched: Mapping[str, str],
    reported: set[tuple[str, str]],
    files: Mapping[str, str],
) -> list[Finding]:
    """The findings for a mark the change makes disappear rather than remove: a sensitive term's
    column that binds at the base and not now (a model whose column lineage now fails no longer
    creates it), and a relation the change touches that reads, through SQL whose columns are not
    known, a relation that held sensitive data at the base. Only when the head shows a sign of
    either is the base built, from its files alone (and kept in :func:`base_cache`); when
    it cannot be, each sign is a finding, and so is a term the change leaves alone that the base
    does not have, as a base whose files Konstruct did not read in full would not."""
    from konstruct.plugins.api import Finding

    graph = analysis.graph
    unbound = _unbound(graph)
    blind = analysis.blind_reads(touched)
    if not unbound and not blind:
        return []
    previous = base_graph(root, ref, base_cache())
    found: list[Finding] = []
    for term, column in unbound:
        if previous is None:
            why = 'Konstruct cannot build the base'
        elif term.id not in previous.nodes and not _changed_file(files, term.path):
            why = 'the base, built from its files, has no such term'
        else:
            continue
        found.append(
            Finding(
                'sensitivity-removed',
                f'the {term.attrs["sensitivity"]} term {term.name!r} names {column}, which is not a column, '
                f'and {why} to tell whether the change made its mark disappear',
                term.path,
                REMOVED_FIX,
            )
        )
    if previous is None:
        held: dict[str, list[str]] | None = None
    else:
        carry_governance(previous, graph)
        held = {}
        for sensitive in _Analysis(previous, analysis.settings).run().columns:
            held[sensitive.relation_id] = sorted(
                {*held.get(sensitive.relation_id, []), *sensitive.sensitivity}
            )
        for term in previous.nodes.values():
            now = graph.nodes.get(term.id)
            if term.kind != 'term' or not term.attrs.get('sensitivity') or now is None:
                continue
            if not now.attrs.get('sensitivity'):
                continue  # taking the sensitivity away is a change to its definition, judged there
            bound = {e.target for e in graph.out_edges(now.id, 'defines')}
            for edge in previous.out_edges(term.id, 'defines'):
                lost = previous.nodes.get(edge.target)
                if lost is None or lost.kind != 'column' or edge.target in bound:
                    continue
                found.append(
                    Finding(
                        'sensitivity-removed',
                        f'{shown(previous, lost)} is no longer marked {now.attrs["sensitivity"]}: the term '
                        f'{now.name!r} binds it at the base and not after the change',
                        now.path,
                        REMOVED_FIX,
                    )
                )
    for source, target in blind:
        if (analysis.label(source), target) in reported or analysis.holder(graph.nodes[target]) is not None:
            continue
        if held is None:
            what = 'which may hold sensitive data at the base (Konstruct cannot build the base to tell)'
        elif held.get(source):
            what = f'which holds {", ".join(held[source])} data at the base'
        else:
            continue
        found.append(
            Finding(
                'sensitive-flow',
                f'{analysis.label(target)} reads {analysis.label(source)}, {what}, through SQL whose '
                'columns Konstruct does not know, so it may hold that data, and is not marked to hold it',
                touched[target],
                FLOW_FIX,
            )
        )
    return found


BASE_GRAPHS = 8
"""How many graphs of base commits the gate keeps in :func:`base_cache`."""


def base_cache() -> Path | None:
    """Where the gate keeps the graphs of base commits: the user's own cache directory for
    ``base`` (:func:`konstruct.engine.snapshot.user_cache`), never a path in the repository or one
    its configuration names, so a change cannot commit a graph of its base for the gate to read;
    None in CI."""
    return snapshot.user_cache('base')


def base_graph(root: Path, ref: str, cache: Path | None = None) -> Graph | None:
    """The project's graph as it is at ``ref``, built from its files alone in a temporary
    directory, or None when the commit or its configuration cannot be read or the build fails.

    The files are the commit's own blobs, read with ``git cat-file`` and not through ``git
    archive``, which a ``.gitattributes`` ``export-ignore`` or ``export-subst`` the commit sets
    would change. A symbolic link whose target, followed within the commit, is a regular file of
    the project is written as that file; any other link, and a submodule, is left out. With
    ``cache``, the graph is kept there, by repository, work tree, commit, the project's directory
    in it and the versions :func:`konstruct.engine.snapshot.cache_name` covers, and read back
    instead of built again; where the files do not name the repository as git does, it is built
    every time."""
    import tempfile

    from konstruct.analysis.diff import DiffError
    from konstruct.core.formats import FormatError
    from konstruct.engine.build import build
    from konstruct.engine.config import ConfigError, load_config

    located = _located(root, ref)
    if located is None:
        return None
    top, commit, prefix, name = located
    kept = cache / name if cache is not None and name is not None else None
    if kept is not None and snapshot.private(kept.parent) and snapshot.private(kept):
        try:
            return Graph.read(kept)
        except (OSError, ValueError, FormatError):
            pass  # built again below, and written over
    with tempfile.TemporaryDirectory(prefix='konstruct-base-') as scratch:
        tree = Path(scratch) / 'tree'
        try:
            snapshot.write_tree(top, commit, prefix, tree)
            config = load_config(tree / prefix, Path(scratch) / 'out', trusted=False)
            config.override(run_dbt=False, warehouses=False)
            config.html = config.report = False
            graph = build(config).graph
        except (DiffError, ConfigError, OSError, ValueError):
            return None
    if kept is not None:
        try:
            kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not snapshot.private(kept.parent):
                return graph
            graph.write(kept)  # through a new file renamed over it, so a reader sees all or none
            kept.chmod(0o600)
            for old in sorted(kept.parent.glob('*.json'), key=lambda p: p.stat().st_mtime)[:-BASE_GRAPHS]:
                old.unlink()
        except OSError:
            pass  # a cache that cannot be written only costs the next gate the build
    return graph


def _located(root: Path, ref: str) -> tuple[Path, str, Path, str | None] | None:
    """The repository's top, the commit ``ref`` names, the project's directory in the repository
    and the file of the commit's graph in the cache (:func:`konstruct.engine.snapshot.cache_name`),
    None where it is not cached, or None when git cannot tell."""
    found = snapshot.cache_name(root, ref)
    if found is None:
        return None
    return (*found[:3], f'{found[3]}.json' if found[3] is not None else None)


def _changed_file(files: Mapping[str, str], path: str | None) -> str | None:
    """The changed file (relative to the repository) that ``path`` (relative to the project) is."""
    if not path:
        return None
    wanted = path.replace('\\', '/').removeprefix('./')
    return next((f for f in files if f == wanted or f.endswith('/' + wanted)), None)


def _packs_removed(root: Path, ref: str) -> list[Finding]:
    """A finding for each ontology pack with sensitive terms the change turns off."""
    from konstruct.plugins.api import Finding
    from konstruct.semantics.registry import load_packs

    packs = load_packs()
    every = {p.name for p in packs}
    sensitive = {p.name for p in packs if any(t.get('sensitivity') for t in p.terms)}

    def enabled(found: tuple[dict[str, Any], str] | None) -> set[str]:
        chosen = (found[0].get('ontologies') or {}).get('packs') if found is not None else None
        return every if not isinstance(chosen, list) else {str(p) for p in chosen}

    head = config_at(root, None)
    removed = sorted((enabled(config_at(root, ref)) - enabled(head)) & sensitive)
    where = head[1] if head is not None else 'konstruct.toml'
    return [
        Finding(
            'sensitivity-removed',
            f'the change turns off the ontology pack {name}, whose terms mark columns sensitive',
            where,
            REMOVED_FIX,
        )
        for name in removed
    ]
