"""The ontology's keys and relationships, checked against the warehouse by ``konstruct verify``.

The ontology claims that a key identifies one instance per row (``customers.customer_id`` is the
key of *customer*) and that a referring column points at instances that exist
(``orders.customer_id`` belongs to a customer). Static analysis can only read those claims from
tests, constraints and joins. Here each claim is put to the data, in queries that only read:

* a key is confirmed when no value repeats and none is null;
* a relationship is confirmed when every value of the referring column is found in the key it
  refers to, and that key is confirmed, so its cardinality holds as well; one claimed one-to-one
  needs the referring column confirmed unique too.

A check reads the whole table, so it runs only where verify reads the warehouse's own tables
(DuckDB and Postgres); a sampled warehouse holds a sample of each table, which could miss a
duplicate or the row a value refers to. What a check finds is recorded in ``graph.json`` under
``meta.integrity``, from which the ontology marks a confirmed key or relationship ``verified``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from konstruct.semantics.ontology import derive, link_key

if TYPE_CHECKING:
    from konstruct.analysis.verify import Engine
    from konstruct.core.graph import Graph, Node


@dataclass
class KeyCheck:
    """One entity key column, checked for duplicate and null values."""

    entity: str
    """The entity it is the key of."""
    column: str
    """The key column, by label (``customers.customer_id``)."""
    state: str
    """``confirmed`` (no value repeats and none is null), ``failed``, or ``skipped``."""
    rows: int = 0
    """The rows of its table."""
    nulls: int = 0
    """Rows where the key is null."""
    duplicates: int = 0
    """Values that a second row repeats."""
    reason: str | None = None
    """Why it was skipped, when it was."""
    id: str = field(default='', repr=False)
    """The column's graph id, for tools; not written out."""

    @property
    def detail(self) -> str:
        """What the check found, in a sentence."""
        if self.state == 'skipped':
            return f'{self.column} not checked: {self.reason}'
        if self.state == 'confirmed':
            return f'{self.column} is unique and never null in {_rows(self.rows)}'
        found = [
            f'{self.duplicates} duplicate value{"" if self.duplicates == 1 else "s"}'
            if self.duplicates
            else '',
            f'{self.nulls} null{"" if self.nulls == 1 else "s"}' if self.nulls else '',
        ]
        return f'{self.column} has {" and ".join(f for f in found if f)} in {_rows(self.rows)}'

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != 'id' and v is not None}
        return {**data, 'detail': self.detail}


@dataclass
class LinkCheck:
    """One relationship, checked for values of the referring column that the key lacks."""

    source: str
    """The entity that refers (``order``)."""
    target: str
    """The entity referred to (``customer``)."""
    via: str
    """The columns that join them (``orders.customer_id = customers.customer_id``)."""
    state: str
    """``confirmed`` (every value is found and the key is confirmed), ``failed``, or ``skipped``."""
    values: int = 0
    """The non-null values of the referring column."""
    orphans: int = 0
    """Those not found in the key."""
    reason: str | None = None
    """Why it was skipped, or why a check that found no orphan still failed."""
    one_to_one: str | None = None
    """For a relationship claimed one-to-one, whether the referring column is unique and never
    null: ``confirmed``, ``failed`` (it is many-to-one; every value may still be found), or
    ``skipped``; left out for one claimed many-to-one."""
    columns: tuple[str, str] = field(default=('', ''), repr=False)
    """The graph ids of the referring column and the key, for tools; not written out."""

    @property
    def detail(self) -> str:
        """What the check found, in a sentence."""
        referring, key = (side.strip() for side in self.via.split('=', 1))
        if self.state == 'skipped':
            return f'{self.via} not checked: {self.reason}'
        if self.orphans:
            return f'{self.orphans} of {self.values} values of {referring} are not found in {key}'
        found = f'all {self.values} values of {referring} are found in {key}'
        found = f'{found}, but {self.reason}' if self.reason else found
        if self.one_to_one == 'failed':
            found += f', and {referring} repeats, so it is many-to-one'
        return found

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != 'columns' and v is not None}
        return {**data, 'detail': self.detail}


def check(
    graph: Graph, engine: Engine, only: list[str] | None = None
) -> tuple[list[KeyCheck], list[LinkCheck]]:
    """Check every entity key and relationship of ``graph``'s ontology in ``engine``'s warehouse,
    or with ``only`` those whose tables are all among the named models and tables: nothing
    outside them is read."""
    ontology = derive(graph)

    def wanted(relation: Node | None) -> bool:
        if relation is None:
            return False
        names = (relation.name, relation.id, graph.label(relation))
        return not only or any(name in names for name in only)

    keys: dict[str, KeyCheck] = {}

    def key_check(entity: str, column: Node) -> KeyCheck | None:
        relation = graph.parent_of(column)
        if relation is None or not wanted(relation):
            return None
        if column.id not in keys:
            keys[column.id] = _key(graph, engine, entity, column, relation)
        return keys[column.id]

    for entity in ontology.entities:
        for column_id in entity.key_ids:
            column = graph.nodes.get(column_id)
            if column is not None:
                key_check(entity.name, column)
    links = []
    for relationship in ontology.relationships:
        if relationship.columns is None:
            continue
        source_id, target_id = relationship.columns
        source, target = graph.nodes.get(source_id), graph.nodes.get(target_id)
        if source is None or target is None or not wanted(graph.parent_of(source)):
            continue
        key = key_check(relationship.target, target)
        if key is None:
            continue
        link = _link(
            graph, engine, relationship.source, relationship.target, relationship.via, source, target
        )
        if link.state == 'confirmed' and key.state != 'confirmed':
            link.state = 'failed' if key.state == 'failed' else 'skipped'
            link.reason = (
                f'{key.column}, the column it refers to, is not unique and never null'
                if key.state == 'failed'
                else f'{key.column}, the column it refers to, could not be checked'
            )
        # A one-to-one claim also needs the referring column unique: checked, and recorded, apart
        # from whether its values are found, which a many-to-one relationship needs alone.
        if relationship.cardinality == 'one-to-one':
            referring = keys.get(source_id)
            if referring is None and wanted(graph.parent_of(source)):
                relation = graph.parent_of(source)
                assert relation is not None
                referring = _key(graph, engine, relationship.source, source, relation)
            link.one_to_one = referring.state if referring is not None else 'skipped'
        links.append(link)
    return sorted(keys.values(), key=lambda k: (k.entity, k.column)), links


def annotate(
    graph: Graph, keys: list[KeyCheck], links: list[LinkCheck], warehouse: str, *, partial: bool = False
) -> None:
    """Record in ``graph.json`` what the checks found, for the ontology to read; a skipped check
    records nothing, so it neither confirms nor fails a fact. A ``partial`` run, limited to some
    models, keeps what earlier runs found of the others. The warehouse is named by its file name
    or database, never its path."""
    name = _warehouse_name(warehouse)

    def record(item: KeyCheck | LinkCheck) -> dict[str, Any]:
        found = {'state': item.state, 'detail': f'{item.detail} ({name})'}
        if isinstance(item, LinkCheck) and item.one_to_one in ('confirmed', 'failed'):
            found['one_to_one'] = item.one_to_one
        return found

    earlier = graph.meta.get('integrity') if partial else None
    found: dict[str, Any] = earlier if isinstance(earlier, dict) else {}
    graph.meta['integrity'] = {
        'warehouse': name,
        'keys': {**(found.get('keys') or {}), **{k.id: record(k) for k in keys if k.state != 'skipped'}},
        'links': {
            **(found.get('links') or {}),
            **{link_key(*item.columns): record(item) for item in links if item.state != 'skipped'},
        },
    }


def _warehouse_name(warehouse: str) -> str:
    """A DuckDB file by its name alone, so no path on the machine that ran verify is recorded."""
    from pathlib import PurePath

    if '/' in warehouse or '\\' in warehouse:
        return PurePath(warehouse.replace('\\', '/')).name
    return warehouse


def _rows(count: int) -> str:
    return f'{count} row{"" if count == 1 else "s"}'


def _table(graph: Graph, engine: Engine, relation: Node) -> str:
    if not relation.relation:
        raise LookupError(f'{graph.label(relation)} has no relation in the warehouse')
    return engine.table(relation.relation)


def _name(column: Node) -> str:
    from konstruct.analysis.verify import quote

    if '.' in column.name:
        raise LookupError('it is a field nested in a column')
    return quote(column.name)


def _scalar(engine: Engine, sql: str) -> list[int]:
    _, rows = engine.read(sql)
    return [int(value or 0) for value in rows[0]]


def _key(graph: Graph, engine: Engine, entity: str, column: Node, relation: Node) -> KeyCheck:
    result = KeyCheck(entity, graph.label(column), 'skipped', id=column.id)
    try:
        table, name = _table(graph, engine, relation), _name(column)
        rows, values, distinct = _scalar(
            engine, f'SELECT count(*), count({name}), count(DISTINCT {name}) FROM {table}'
        )
    except Exception as error:  # a table the warehouse lacks, or a type it cannot compare
        result.reason = _reason(error)
        return result
    if not rows:
        result.reason = 'its table is empty'
        return result
    result.rows, result.nulls, result.duplicates = rows, rows - values, values - distinct
    result.state = 'failed' if result.nulls or result.duplicates else 'confirmed'
    return result


def _link(
    graph: Graph, engine: Engine, source: str, target: str, via: str, referring: Node, key: Node
) -> LinkCheck:
    result = LinkCheck(source, target, via, 'skipped', columns=(referring.id, key.id))
    try:
        many, one = graph.parent_of(referring), graph.parent_of(key)
        if many is None or one is None:
            raise LookupError('a column has no table')
        many_table, one_table = _table(graph, engine, many), _table(graph, engine, one)
        f, k = _name(referring), _name(key)
        [values] = _scalar(engine, f'SELECT count({f}) FROM {many_table}')
        [orphans] = _scalar(
            engine,
            f'SELECT count(*) FROM {many_table} AS m WHERE m.{f} IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM {one_table} AS o WHERE o.{k} = m.{f})',
        )
    except Exception as error:  # a table the warehouse lacks, or types it cannot compare
        result.reason = _reason(error)
        return result
    if not values:
        result.reason = 'the referring column holds no value'
        return result
    result.values, result.orphans = values, orphans
    result.state = 'failed' if orphans else 'confirmed'
    return result


def _reason(error: Exception) -> str:
    text = str(error).strip().splitlines()[0][:160] if str(error).strip() else type(error).__name__
    return text if isinstance(error, LookupError) else f'{type(error).__name__}: {text}'
