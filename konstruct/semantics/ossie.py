"""Apache Ossie semantic models, read as an input and written as an export.

`Apache Ossie <https://ossie.apache.org/>`_ (formerly the Open Semantic Interchange) is a YAML format
for semantic models: datasets over physical tables, their fields, the relationships between them
and the metrics computed over them. Its schema leaves out lineage, ownership and certification,
which is what Konstruct adds.

Reading: every ``*.ossie.yaml`` (or ``.ossie.yml``) file in the repository is an input. Each
metric becomes a business term of kind ``measure``, bound to the physical columns it reads through
its fields; a dataset's primary key marks an entity key; a relationship links two entities. A
dataset, field or metric that names something the data stack does not have is a build warning.
Both the released 0.1.1 shape (models in a ``semantic_model`` list) and the 0.2.0 draft (one model
at the root) are read.

Writing: ``konstruct export ossie`` writes the ontology as one 0.2.0.dev0 model. A dataset read from
an Ossie model is written back as authored, and so is each of its model's metrics whose datasets all
keep their names, with Konstruct's extension added; the rest is derived: a dataset for each other
relation that holds an entity or a measure's sources, the relationships between them whose key is
known to be unique, and each aggregate measure as a metric whose expression is its formula composed
down to those sources (or, for a metric read from an Ossie model, its own expression). The portable
``sql`` is written in ANSI SQL, with the warehouse's own dialect under ``dialects``. Each metric
carries its status, owner, verification, usage and columns in a ``KONSTRUCT`` custom extension, and
each derived dataset that holds an entity names it in one, since Ossie has no entities of its own.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS

log = logging.getLogger(__name__)

FILE_SUFFIXES = ('.ossie.yaml', '.ossie.yml')
EXPORT_VERSION = '0.2.0.dev0'
VENDOR = 'KONSTRUCT'
# The table name that marks a source column in a stored metric expression.
_TOKENS = '_konstruct_column'
# The expression dialects Konstruct can read, best first; OSSIE_SQL_2026 is ANSI SQL.
_READABLE = ('OSSIE_SQL_2026', 'ANSI_SQL')
_SQLGLOT_DIALECTS = {
    'SNOWFLAKE': 'snowflake',
    'DATABRICKS': 'databricks',
    'BIGQUERY': 'bigquery',
    'TRINO': 'trino',
    'CLICKHOUSE': 'clickhouse',
    'POSTGRES': 'postgres',
    'REDSHIFT': 'redshift',
}
_OSSIE_DIALECTS = {sqlglot_name: token for token, sqlglot_name in _SQLGLOT_DIALECTS.items()}
# Physical types by their base name (the words before any length, precision or element type).
_DATATYPES = {
    'String': {
        'VARCHAR',
        'CHAR',
        'CHARACTER',
        'CHARACTER VARYING',
        'TEXT',
        'STRING',
        'NVARCHAR',
        'NCHAR',
        'BPCHAR',
    },
    'Integer': {
        'INT',
        'INTEGER',
        'BIGINT',
        'SMALLINT',
        'TINYINT',
        'INT2',
        'INT4',
        'INT8',
        'INT64',
        'HUGEINT',
        'UBIGINT',
        'UINTEGER',
        'USMALLINT',
        'UTINYINT',
        'UHUGEINT',
        'BYTEINT',
        'SERIAL',
        'BIGSERIAL',
        'SMALLSERIAL',
    },
    'Decimal': {'DECIMAL', 'NUMERIC', 'NUMBER', 'BIGNUMERIC', 'BIGDECIMAL'},
    'Float': {'FLOAT', 'FLOAT4', 'FLOAT8', 'FLOAT64', 'DOUBLE', 'DOUBLE PRECISION', 'REAL'},
    'Boolean': {'BOOL', 'BOOLEAN'},
    'Date': {'DATE'},
    # Ossie's Time has no zone; a time with a zone is still a time of day.
    'Time': {'TIME', 'TIME WITHOUT TIME ZONE', 'TIME WITH TIME ZONE', 'TIMETZ'},
    'DateTime': {
        'TIMESTAMP',
        'DATETIME',
        'TIMESTAMP WITHOUT TIME ZONE',
        'TIMESTAMP_NTZ',
        'TIMESTAMP_S',
        'TIMESTAMP_MS',
        'TIMESTAMP_NS',
    },
    'DateTimeTz': {
        'TIMESTAMPTZ',
        'TIMESTAMP WITH TIME ZONE',
        'TIMESTAMP_TZ',
        'TIMESTAMP_LTZ',
        'TIMESTAMP WITH LOCAL TIME ZONE',
    },
}
_BY_BASE = {base: name for name, bases in _DATATYPES.items() for base in bases}


@dataclass
class Dataset:
    name: str
    source: str
    fields: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """Field name to its SQL expression over the source's columns, and the dialect to read it in."""
    primary_key: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    """The dataset as authored, without its metrics, so the export can write it back."""


@dataclass
class Metric:
    name: str
    sql: str | None
    dataset: str | None = None
    """The dataset of a dataset-scoped metric; None for a model-scoped one."""
    description: str | None = None
    synonyms: list[str] = field(default_factory=list)
    dialect: str | None = None
    """The sqlglot dialect its expression is read in; None for ANSI SQL."""
    raw: dict[str, Any] = field(default_factory=dict)
    """The metric as authored."""


@dataclass
class Model:
    name: str
    path: str
    description: str | None = None
    datasets: dict[str, Dataset] = field(default_factory=dict)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)


@dataclass
class OssieFile:
    path: str
    models: list[Model] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _expression(value: Any) -> tuple[str | None, str | None]:
    """The SQL of an Ossie expression and the sqlglot dialect to read it with."""
    if not isinstance(value, dict):
        return None, None
    if isinstance(value.get('sql'), str):
        return value['sql'], None
    options = [
        d for d in value.get('dialects') or [] if isinstance(d, dict) and isinstance(d.get('expression'), str)
    ]
    for wanted in _READABLE:
        for option in options:
            if option.get('dialect') == wanted:
                return option['expression'], None
    for option in options:
        dialect = _SQLGLOT_DIALECTS.get(str(option.get('dialect')))
        if dialect:
            return option['expression'], dialect
    return None, None


def _synonyms(ai_context: Any) -> list[str]:
    if isinstance(ai_context, dict) and isinstance(ai_context.get('synonyms'), list):
        return [str(s) for s in ai_context['synonyms']]
    return []


def parse(text: str, path: str) -> OssieFile:
    """Parse an Ossie file; problems are collected, so one bad model does not hide the rest."""
    result = OssieFile(path=path)
    try:
        data = yaml.safe_load(text)
    except RecursionError:
        result.errors.append(f'{path}: not valid YAML: nested too deeply to read')
        return result
    except yaml.YAMLError as error:
        result.errors.append(f'{path}: not valid YAML: {error}')
        return result
    if not isinstance(data, dict):
        result.errors.append(f'{path}: expected an Ossie semantic model')
        return result
    raw_models = data['semantic_model'] if isinstance(data.get('semantic_model'), list) else [data]
    for raw in raw_models:
        if not isinstance(raw, dict) or not raw.get('name') or not isinstance(raw.get('datasets'), list):
            result.errors.append(f'{path}: a semantic model needs a `name` and a `datasets` list')
            continue
        description = raw.get('description')
        model = Model(name=str(raw['name']), path=path, description=str(description) if description else None)
        where = f'{path}: model {model.name!r}'
        for item in raw['datasets']:
            if not isinstance(item, dict) or not item.get('name') or not item.get('source'):
                result.errors.append(f'{where}: a dataset needs a `name` and a `source`')
                continue
            dataset = Dataset(
                name=str(item['name']),
                source=str(item['source']).strip(),
                raw=_plain({k: v for k, v in item.items() if k != 'metrics'}),
            )
            for raw_field in item.get('fields') or []:
                if not isinstance(raw_field, dict) or not raw_field.get('name'):
                    result.errors.append(f'{where}: a field of dataset {dataset.name!r} has no `name`')
                    continue
                sql, dialect = _expression(raw_field.get('expression'))
                if sql:
                    dataset.fields[str(raw_field['name'])] = (sql, dialect)
                else:
                    result.errors.append(
                        f'{where}: field {dataset.name}.{raw_field["name"]} has no expression Konstruct can read'
                    )
            dataset.primary_key = [str(k) for k in item.get('primary_key') or []]
            model.datasets[dataset.name] = dataset
            for raw_metric in item.get('metrics') or []:
                if isinstance(raw_metric, dict) and raw_metric.get('name'):
                    model.metrics.append(_metric(raw_metric, dataset.name))
        model.relationships = [_plain(r) for r in raw.get('relationships') or [] if isinstance(r, dict)]
        for raw_metric in raw.get('metrics') or []:
            if isinstance(raw_metric, dict) and raw_metric.get('name'):
                model.metrics.append(_metric(raw_metric, None))
        result.models.append(model)
    return result


def _metric(raw: dict[str, Any], dataset: str | None) -> Metric:
    sql, dialect = _expression(raw.get('expression'))
    description = raw.get('description')
    return Metric(
        name=str(raw['name']),
        sql=sql,
        dataset=dataset,
        description=str(description) if description else None,
        synonyms=_synonyms(raw.get('ai_context')),
        dialect=dialect,
        raw=_plain(raw),
    )


def _plain(value: Any) -> Any:
    """``value`` as JSON data, so it can be kept in the graph: YAML dates become strings."""
    result: Any = json.loads(json.dumps(value, default=str))
    return result


def parse_file(path: Path, relative: str | None = None) -> OssieFile:
    return parse(path.read_text(encoding='utf-8'), relative or str(path))


@dataclass
class Binding:
    """What one Ossie model means in the graph."""

    relations: dict[str, Node] = field(default_factory=dict)
    """Dataset name to the relation its source names."""
    fields: dict[tuple[str, str], list[Node]] = field(default_factory=dict)
    """(dataset, field) to the source columns its expression reads."""
    metrics: dict[str, list[Node]] = field(default_factory=dict)
    """Metric name to the source columns it reads."""
    portable: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    """Metric name to its expression rewritten over source columns (each field replaced by its own
    expression, in parentheses), with each column written as a token column of the ``_TOKENS``
    table, and the tokens' column ids; so the metric can be written back over any dataset names."""
    datasets: dict[str, str] = field(default_factory=dict)
    """Metric name to the relation id of the dataset a dataset-scoped metric belongs to."""
    keys: list[Node] = field(default_factory=list)
    """Columns that a dataset's single-field primary key names; one field of a composite key is
    not unique on its own."""
    relationships: list[tuple[Node, Node, str]] = field(default_factory=list)
    """(many-side column, one-side column, relationship name)."""
    problems: list[str] = field(default_factory=list)
    failed: set[str] = field(default_factory=set)
    """Metrics whose expression did not resolve, which are left out."""
    reads: dict[str, dict[str, str]] = field(default_factory=dict)
    """Metric name to the datasets its expression names and their relation ids, so the export
    knows when the metric can be written back as authored."""


def _tree(sql: str, dialect: str | None) -> exp.Expression | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        return tree if isinstance(tree, exp.Expression) else None
    except PARSE_ERRORS:
        return None


def bind(graph: Graph, model: Model) -> Binding:
    """Resolve ``model``'s datasets, fields and metrics to the graph's relations and columns."""
    result = Binding()
    where = f'{model.path}: model {model.name!r}'
    trees: dict[tuple[str, str], exp.Expression] = {}  # each field over token columns
    tokens: dict[str, str] = {}  # column id -> token

    def token(node: Node) -> exp.Column:
        name = tokens.setdefault(node.id, f'c{len(tokens)}')
        return exp.column(name, table=_TOKENS)

    for dataset in model.datasets.values():
        relation = graph.node_for_relation(dataset.source)
        if relation is None:
            candidates = [n for n in graph.resolve(dataset.source) if n.kind in RELATION_KINDS]
            relation = candidates[0] if len(candidates) == 1 else None
        if relation is None:
            result.problems.append(
                f'{where}: dataset {dataset.name!r} reads {dataset.source!r}, which does not exist'
            )
            continue
        result.relations[dataset.name] = relation
        for name, (sql, dialect) in dataset.fields.items():
            tree = _tree(sql, dialect)
            if tree is None:
                result.problems.append(f'{where}: field {dataset.name}.{name} is not valid SQL')
                continue
            found = []
            unresolved = False
            for column in tree.find_all(exp.Column):
                node = graph.column(relation.id, column.name)
                if node is None:
                    unresolved = True
                    result.problems.append(
                        f'{where}: field {dataset.name}.{name} reads {column.name!r}, which '
                        f'{graph.label(relation)} does not have'
                    )
                elif node not in found:
                    found.append(node)
            result.fields[(dataset.name, name)] = found
            if not unresolved:
                over = tree.copy()
                for column in list(over.find_all(exp.Column)):
                    node = graph.column(relation.id, column.name)
                    if node is not None:
                        replacement = token(node)
                        # A field that is one column is its root, which replace() cannot swap.
                        over = replacement if column is over else over
                        column.replace(replacement)
                trees[(dataset.name, name)] = over
        if len(dataset.primary_key) == 1:
            key_columns = result.fields.get((dataset.name, dataset.primary_key[0]), [])
            if len(key_columns) == 1:
                result.keys.append(key_columns[0])
    seen_metrics: set[str] = set()
    for metric in model.metrics:
        if metric.name in seen_metrics:
            result.problems.append(f'{where}: metric {metric.name!r} is defined twice; the first is kept')
            continue
        seen_metrics.add(metric.name)
        if metric.sql is None:
            result.problems.append(f'{where}: metric {metric.name!r} has no expression Konstruct can read')
            result.failed.add(metric.name)
            continue
        tree = _tree(metric.sql, metric.dialect)
        if tree is None:
            result.problems.append(f'{where}: metric {metric.name!r} is not valid SQL')
            result.failed.add(metric.name)
            continue
        read: list[Node] = []
        portable = tree.copy()
        replacements: list[tuple[exp.Column, exp.Expression]] = []
        for column in list(portable.find_all(exp.Column)):
            key: tuple[str, str] | None
            if metric.dataset is None:
                key = (column.table, column.name) if column.table else None
            elif column.table and column.table.lower() == 'source':
                relation = result.relations.get(metric.dataset)
                node = graph.column(relation.id, column.name) if relation else None
                if node is not None:
                    replacements.append((column, token(node)))
                    if node not in read:
                        read.append(node)
                continue
            else:
                key = (metric.dataset, column.name)
            if key is None and any(name == column.name for _, name in result.fields):
                # The spec requires dataset.field even when only one dataset has the field.
                result.problems.append(
                    f'{where}: metric {metric.name!r} reads {column.sql()!r} without its dataset; a '
                    'model-scoped metric names each field as dataset.field'
                )
                result.failed.add(metric.name)
                continue
            if key is None or key not in result.fields:
                result.problems.append(
                    f'{where}: metric {metric.name!r} reads {column.sql()!r}, which is not a field of the model'
                )
                result.failed.add(metric.name)
                continue
            if key in trees:
                field_tree = trees[key].copy()
                replacements.append(
                    (column, field_tree if isinstance(field_tree, exp.Column) else exp.Paren(this=field_tree))
                )
            read.extend(n for n in result.fields[key] if n not in read)
            result.reads.setdefault(metric.name, {})[key[0]] = result.relations[key[0]].id
        if metric.dataset is not None and metric.dataset in result.relations:
            result.reads.setdefault(metric.name, {})[metric.dataset] = result.relations[metric.dataset].id
        result.metrics[metric.name] = read
        if len(replacements) == len(list(tree.find_all(exp.Column))):
            for reference, swapped in replacements:
                reference.replace(swapped)
            used = {c.name for c in portable.find_all(exp.Column) if c.table == _TOKENS}
            result.portable[metric.name] = (
                portable.sql(dialect=metric.dialect),
                {t: i for i, t in tokens.items() if t in used},
            )
        if metric.dataset is not None and metric.dataset in result.relations:
            result.datasets[metric.name] = result.relations[metric.dataset].id
    for rel in model.relationships:
        rel_name = rel.get('name')
        source, target = result.relations.get(str(rel.get('from'))), result.relations.get(str(rel.get('to')))
        left_fields, right_fields = rel.get('from_columns') or [], rel.get('to_columns') or []
        if source is None or target is None or not left_fields:
            result.problems.append(f'{where}: relationship {rel_name!r} joins datasets that do not resolve')
            continue
        if len(left_fields) != len(right_fields):
            result.problems.append(
                f'{where}: relationship {rel_name!r} has {len(left_fields)} from_columns and '
                f'{len(right_fields)} to_columns'
            )
            continue
        if len(left_fields) > 1:
            result.problems.append(
                f'{where}: relationship {rel_name!r} joins on several fields; only single-field '
                'relationships become ontology relationships'
            )
            continue
        many = result.fields.get((str(rel['from']), str(left_fields[0])), [])
        one = result.fields.get((str(rel['to']), str(right_fields[0])), [])
        if len(many) == 1 and len(one) == 1:
            result.relationships.append((many[0], one[0], str(rel_name)))
        else:
            result.problems.append(
                f'{where}: relationship {rel_name!r} joins fields that are not one column each'
            )
    return result


# Export


def identifier(text: str) -> str:
    """An Ossie identifier: letters, digits and underscores."""
    cleaned = re.sub(r'\W+', '_', text).strip('_')
    return cleaned or 'unnamed'


def unique_name(name: str, used: set[str]) -> str:
    candidate, index = name, 2
    while candidate in used:
        candidate = f'{name}_{index}'
        index += 1
    used.add(candidate)
    return candidate


def datatype(sql_type: str | None) -> str | None:
    """The Ossie logical type of a physical column type; ``Opaque`` for a type outside Ossie's
    vocabulary (structs, arrays, maps, JSON, intervals, UUIDs), None when the type is unknown."""
    if not sql_type or not sql_type.strip():
        return None
    text = sql_type.strip().upper()
    if text.endswith(']') or '<' in text:
        return 'Opaque'
    base = re.sub(r'\s+', ' ', re.sub(r'\([^)]*\)', ' ', text)).strip()
    return _BY_BASE.get(base, 'Opaque')


def aggregate(tree: exp.Expression) -> bool:
    """Whether ``tree`` aggregates rows; a window function (``SUM(x) OVER ()``) does not."""
    return any(
        isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Window) is None for node in tree.walk()
    )


def export(graph: Graph, ontology: Any, name: str | None = None) -> str:
    """The ontology as an Apache Ossie 0.2.0.dev0 semantic model, as YAML."""
    from konstruct.analysis.explain import Explainer

    explainer = Explainer(graph, qualified=True)
    relations: dict[str, Node] = {}  # dataset name -> relation
    names: dict[str, str] = {}  # relation id -> dataset name
    field_names: dict[str, dict[str, str]] = {}  # relation id -> column id -> field name
    used_fields: dict[str, set[str]] = {}  # relation id -> its field names
    extra: dict[str, list[Node]] = {}  # relation id -> columns written beside an authored dataset's fields
    # A relation read from an Ossie dataset is written back as that dataset, under its own name.
    authored: dict[str, dict[str, Any]] = {}  # relation id -> how it was read
    reserved: dict[str, str] = {}  # authored dataset name -> relation id
    candidates = [
        (node, held)
        for node in graph.nodes.values()
        if node.kind in RELATION_KINDS
        and isinstance(held := node.attrs.get('ossie_authored'), dict)
        and isinstance(held.get('dataset'), dict)
    ]
    # The first file read keeps a dataset name that two models give to different relations.
    for node, held in sorted(candidates, key=lambda c: (str(c[1].get('path')), str(c[1].get('model')))):
        dataset_name = identifier(str(held['dataset'].get('name', '')))
        if dataset_name not in reserved:
            reserved[dataset_name] = node.id
            authored[node.id] = held

    def dataset_for(relation: Node) -> str:
        if relation.id not in names:
            held = authored.get(relation.id)
            used: set[str] = set()
            mapping: dict[str, str] = {}
            if held is not None:
                dataset = identifier(str(held['dataset']['name']))
                # Fields and the dataset's own metrics share one namespace.
                used.update(str(m) for m in held.get('metrics') or [])
                for raw in _authored_fields(held):
                    used.add(str(raw['name']))
                    sql, read_in = _expression(raw.get('expression'))
                    tree = _tree(sql, read_in) if sql else None
                    column = graph.column(relation.id, tree.name) if isinstance(tree, exp.Column) else None
                    if column is not None:
                        mapping.setdefault(column.id, str(raw['name']))
            else:
                # The short label where it is free, else the full relation key, so two relations
                # that share a name (raw.orders, main.orders) stay two datasets.
                label, full = identifier(graph.label(relation)), identifier(relation.relation or relation.id)
                taken = set(relations) | {n for n, owner in reserved.items() if owner != relation.id}
                dataset = label if label not in taken else unique_name(full, taken)
                mapping = {c.id: unique_name(identifier(c.name), used) for c in graph.columns_of(relation.id)}
            names[relation.id] = dataset
            relations[dataset] = relation
            field_names[relation.id] = mapping
            used_fields[relation.id] = used
        return names[relation.id]

    def reference(column: Node) -> exp.Column | None:
        owner = graph.parent_of(column)
        if owner is None:
            return None
        dataset = dataset_for(owner)
        field_name = field_names[owner.id].get(column.id)
        if field_name is None and owner.id in authored and column.kind == 'column':
            # A column the authored dataset has no field for is written as a field beside them.
            field_name = unique_name(identifier(column.name), used_fields[owner.id])
            field_names[owner.id][column.id] = field_name
            extra.setdefault(owner.id, []).append(column)
        return exp.column(field_name, table=dataset, quoted=False) if field_name else None

    for relation_id in authored:
        dataset_for(graph.nodes[relation_id])

    keys: dict[str, str] = {}  # dataset -> key field
    held_by: dict[str, str] = {}  # derived dataset -> the entity whose key it holds
    for entity in ontology.entities:
        for key_id in entity.key_ids:
            column = graph.nodes.get(key_id)
            owner = graph.parent_of(column) if column is not None else None
            if column is None or owner is None:
                continue
            if owner.id in authored:
                # An authored dataset keeps its own primary key, or takes one only from its fields.
                if not authored[owner.id]['dataset'].get('primary_key'):
                    held_name = field_names[owner.id].get(column.id)
                    if held_name is not None:
                        keys.setdefault(names[owner.id], held_name)
                continue
            key = reference(column)
            if key is not None and str(key.table) not in keys:
                keys[str(key.table)] = key.name
                held_by[str(key.table)] = entity.name

    dialect = graph.meta.get('dialect')
    native = _OSSIE_DIALECTS.get(dialect) if isinstance(dialect, str) else None
    metrics = []
    scoped: dict[str, list[dict[str, Any]]] = {}  # dataset -> its own metrics
    used_metrics: set[str] = set()
    origins: set[str | None] = set()  # the Ossie model of each metric written, None for others
    for measure in ontology.measures:
        term = graph.nodes.get(f'term.{_slug(measure.name)}')
        imported = term is not None and isinstance(term.attrs.get('ossie_expression'), str)
        # A first pass with placeholders, so a measure that is not exported adds no dataset.
        trial = measure_tree(graph, explainer, measure, _placeholder)
        reason = None
        if trial is None:
            reason = (
                'its expression does not resolve'
                if imported or _from_ossie(term)
                else 'its formula does not reach source columns'
            )
        elif not aggregate(trial):
            # An Ossie metric is an aggregate; a per-row formula is not one.
            reason = 'it is not an aggregate'
        owner_id = term.attrs.get('ossie_dataset') if term is not None else None
        owner = graph.nodes.get(owner_id) if isinstance(owner_id, str) else None
        if reason is None and trial is not None and not list(trial.find_all(exp.Column)) and owner is None:
            reason = 'it reads no column, so no dataset it belongs to'
        if reason is not None:
            log.warning('ossie export: measure %r is left out: %s', measure.name, reason)
            continue
        authored_metric = term.attrs.get('ossie_metric') if imported and term is not None else None
        reads = term.attrs.get('ossie_reads') if term is not None else None
        # Written as authored only when every dataset it names is written back, under its own
        # name, from the metric's own model: another model's dataset of that name has other fields.
        verbatim = (
            term is not None
            and isinstance(authored_metric, dict)
            and isinstance(reads, dict)
            and bool(reads)
            and all(
                names.get(rid) == ds
                and rid in authored
                and authored[rid].get('model') == term.attrs.get('model')
                and authored[rid].get('path') == term.path
                for ds, rid in reads.items()
            )
        )
        tree = trial if verbatim else measure_tree(graph, explainer, measure, reference)
        if tree is None:
            continue
        home = dataset_for(owner) if owner is not None else None
        metric: dict[str, Any]
        context: dict[str, Any]
        kept_text: str | None = None
        if verbatim and isinstance(authored_metric, dict) and term is not None:
            # Every dataset it names is written back under its own name, with its own fields, so
            # the metric is written as authored: its expression, dialects, data type and the rest.
            metric = copy.deepcopy(authored_metric)
            authored_name = str(authored_metric['name'])
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', authored_name):
                authored_name = identifier(authored_name.lower())
            metric['name'] = unique_name(authored_name, used_metrics)
            held_context = metric.get('ai_context')
            context = dict(held_context) if isinstance(held_context, dict) else {}
            kept_text = held_context if isinstance(held_context, str) else None
            synonyms = [*_synonyms(context), *(term.attrs.get('synonyms', []) if term else []), measure.name]
        else:
            refs = list(tree.find_all(exp.Column))
            if home is not None and all(r.table == home for r in refs):
                for ref in refs:
                    ref.set('table', None)
            else:
                home = None
            # The portable expression in ANSI SQL, and the warehouse's own dialect beside it when
            # the expression was read in that dialect (a metric read from Ossie in portable SQL
            # keeps only its portable form, as translating it could change its meaning).
            read_in = term.attrs.get('formula_dialect') if imported and term is not None else dialect
            expression: dict[str, Any] = {'sql': tree.sql()}
            if native and read_in == dialect:
                expression['dialects'] = [{'dialect': native, 'expression': tree.sql(dialect=dialect)}]
            metric = {
                'name': unique_name(identifier(measure.name.lower()), used_metrics),
                'expression': expression,
            }
            if measure.definition:
                metric['description'] = measure.definition
            context = {}
            synonyms = [measure.name, *(term.attrs.get('synonyms', []) if term else [])]
        kept = [s for s in dict.fromkeys(synonyms) if s != metric['name']]
        if kept:
            context['synonyms'] = kept
        else:
            context.pop('synonyms', None)
        instructions = [f'Grain: {measure.grain}.'] if measure.grain else []
        if measure.filters:
            instructions.append('Required filters: ' + '; '.join(measure.filters) + '.')
        if instructions and 'instructions' not in context:
            context['instructions'] = ' '.join(instructions)
        if kept_text is not None:
            metric['ai_context'] = kept_text  # an authored instruction string stays as written
        elif context:
            metric['ai_context'] = context
        extension = {
            'status': measure.status,
            'owner': measure.owner,
            'approved': measure.approved,
            'entity': measure.entity,
            'grain': measure.grain,
            'columns': [
                {
                    k: v
                    for k, v in {
                        'column': c.column,
                        'formula': c.formula,
                        'sources': c.sources,
                        'verified': c.verified,
                        'edges': c.edges,
                        'usage': c.usage,
                    }.items()
                    if v is not None
                }
                for c in measure.columns
            ],
        }
        others = [
            e
            for e in metric.get('custom_extensions') or []
            if not (isinstance(e, dict) and e.get('vendor_name') == VENDOR)
        ]
        metric['custom_extensions'] = [
            *others,
            {
                'vendor_name': VENDOR,
                'data': json.dumps({k: v for k, v in extension.items() if v is not None}),
            },
        ]
        model = term.attrs.get('model') if term is not None and _from_ossie(term) else None
        origins.add(model if isinstance(model, str) else None)
        if home is not None:
            scoped.setdefault(home, []).append(metric)
        else:
            metrics.append(metric)

    # A relationship read from an Ossie model keeps its name.
    authored_names = {
        (r['from'], r['to']): r['name']
        for r in graph.meta.get('ossie_relationships') or []
        if isinstance(r, dict) and r.get('name')
    }
    relationships = []
    used_relationships: set[str] = set()
    written: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    written_names: set[tuple[str, str]] = set()  # (path, name) of each written as authored
    for held_rel in graph.meta.get('ossie_authored_relationships') or []:
        raw = held_rel.get('raw') if isinstance(held_rel, dict) else None
        if not isinstance(raw, dict):
            continue
        source, target = str(raw.get('from')), str(raw.get('to'))
        ends = [reserved.get(source), reserved.get(target)]
        # Written as authored when both datasets are written back from the relationship's model
        # and every column it names is one of their fields.
        if all(
            rid is not None
            and authored[rid].get('model') == held_rel.get('model')
            and authored[rid].get('path') == held_rel.get('path')
            for rid in ends
        ):
            declared = [{str(f['name']) for f in _authored_fields(authored[str(rid)])} for rid in ends]
            if not (
                {str(c) for c in raw.get('from_columns') or []} <= declared[0]
                and {str(c) for c in raw.get('to_columns') or []} <= declared[1]
            ):
                log.warning(
                    'ossie export: relationship %r is left out: it names a field its datasets do not have',
                    raw.get('name'),
                )
                continue
            authored_rel = copy.deepcopy(raw)
            authored_rel['name'] = unique_name(
                identifier(str(raw.get('name') or f'{source}_to_{target}')), used_relationships
            )
            relationships.append(authored_rel)
            written_names.add((str(held_rel.get('path')), str(raw.get('name'))))
            written.add(
                (
                    source,
                    target,
                    tuple(map(str, raw.get('from_columns') or [])),
                    tuple(map(str, raw.get('to_columns') or [])),
                )
            )
    for rel in ontology.relationships:
        # Ossie's to_columns are a key of the target; without that known, there is no relationship to state.
        if rel.columns is None or rel.cardinality is None:
            continue
        many, one = graph.nodes.get(rel.columns[0]), graph.nodes.get(rel.columns[1])
        left = reference(many) if many is not None else None
        right = reference(one) if one is not None else None
        if left is None or right is None:
            continue
        if (str(left.table), str(right.table), (left.name,), (right.name,)) in written:
            continue
        # The same join, read from a relationship already written as authored, on other fields.
        if any(
            (item.get('from'), item.get('to')) == (rel.columns[0], rel.columns[1])
            and (str(item.get('path')), str(item.get('name'))) in written_names
            for item in graph.meta.get('ossie_relationships') or []
            if isinstance(item, dict)
        ):
            continue
        relationship_name = authored_names.get(
            (rel.columns[0], rel.columns[1]), f'{rel.source}_to_{rel.target}'
        )
        relationships.append(
            {
                'name': unique_name(identifier(relationship_name), used_relationships),
                'from': left.table,
                'to': right.table,
                'from_columns': [left.name],
                'to_columns': [right.name],
            }
        )

    datasets = []
    for dataset, relation in sorted(relations.items()):
        held = authored.get(relation.id)
        fields: list[dict[str, Any]] = copy.deepcopy(_authored_fields(held)) if held is not None else []
        for column in extra.get(relation.id, []) if held is not None else graph.columns_of(relation.id):
            entry: dict[str, Any] = {
                'name': field_names[relation.id][column.id],
                'expression': {'sql': _quoted(column.name)},
            }
            logical = datatype(column.data_type)
            if logical:
                entry['datatype'] = logical
            if column.description:
                entry['description'] = column.description
            if logical == 'Opaque':
                entry['custom_extensions'] = [
                    {'vendor_name': VENDOR, 'data': json.dumps({'data_type': column.data_type})}
                ]
            fields.append(entry)
        item: dict[str, Any]
        if held is not None:
            item = copy.deepcopy(held['dataset'])
            item['name'] = dataset
        else:
            item = {'name': dataset, 'source': relation.relation or graph.label(relation)}
        if dataset in keys and not item.get('primary_key'):
            item['primary_key'] = [keys[dataset]]
        if relation.description and not item.get('description'):
            item['description'] = relation.description
        if fields:
            item['fields'] = fields
        if dataset in scoped:
            item['metrics'] = scoped[dataset]
        if held is None and dataset in held_by:
            # Ossie has no entities; the one a derived dataset holds goes in Konstruct's extension.
            item['custom_extensions'] = [
                {'vendor_name': VENDOR, 'data': json.dumps({'entity': held_by[dataset]})}
            ]
        datasets.append(item)

    if not datasets:
        from konstruct.outputs.export import ExportError

        raise ExportError('nothing to export: the ontology has no entities or measures')
    # The datasets of exactly one Ossie model are written back as that model.
    models = {authored[r.id]['model'] for r in relations.values() if r.id in authored} | origins
    single = next(iter(models)) if len(models) == 1 and len(authored) == len(relations) else None
    description = 'Entities, relationships and measures derived by Konstruct from the data stack.'
    if single is not None:
        first = next(iter(authored.values()))
        description = first.get('model_description') or description
    document: dict[str, Any] = {
        'version': EXPORT_VERSION,
        'name': identifier(single or name or 'konstruct'),
        'description': description,
        'datasets': datasets,
    }
    if relationships:
        document['relationships'] = relationships
    if metrics:
        document['metrics'] = metrics
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


def _authored_fields(held: dict[str, Any]) -> list[dict[str, Any]]:
    """The named fields of an authored dataset."""
    return [f for f in held['dataset'].get('fields') or [] if isinstance(f, dict) and f.get('name')]


def measure_tree(graph: Graph, explainer: Any, measure: Any, reference: Any) -> exp.Expression | None:
    """The measure's expression over dataset fields: a metric read from an Ossie model keeps its
    own expression; any other measure is its first complete column's formula composed to source."""
    term = graph.nodes.get(f'term.{_slug(measure.name)}')
    stored = term.attrs.get('ossie_expression') if term is not None else None
    if isinstance(stored, str) and term is not None:
        parsed = _tree(stored, term.attrs.get('formula_dialect'))
        if parsed is None:
            return None
        tokens: dict[str, str] = term.attrs.get('ossie_columns') or {}
        for column in list(parsed.find_all(exp.Column)):
            node = graph.nodes.get(tokens.get(column.name, '')) if column.table == _TOKENS else None
            replacement = reference(node) if node is not None else None
            if replacement is None:
                return None
            column.replace(replacement)
        return parsed
    if _from_ossie(term):
        # A metric read from Ossie that did not resolve: its columns are its inputs, and composing
        # one of them would state that input's formula as the metric's.
        return None
    held = next((c for c in measure.columns if c.formula and c.complete and c.id), None)
    node = graph.nodes.get(held.id) if held is not None and held.id else None
    composed = explainer.compose(node) if node is not None else None
    if composed is None or composed.tree is None:
        return None
    tree: exp.Expression = composed.tree.copy()
    for ref in list(tree.find_all(exp.Column)):
        found = explainer.namer.resolve(ref)
        if found is None:
            return None
        owner, column_name = found
        target = graph.column(owner.id, column_name)
        replacement = reference(target) if target is not None else None
        if replacement is None:
            return None
        ref.replace(replacement)
    return tree


def _from_ossie(term: Node | None) -> bool:
    return term is not None and 'ossie' in term.origins


def _placeholder(column: Node) -> exp.Column | None:
    return exp.column('x', table='t') if column.kind == 'column' else None


def _quoted(name: str) -> str:
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) else '"' + name.replace('"', '""') + '"'


def _slug(name: str) -> str:
    from konstruct.semantics.definitions import slug

    return slug(name)
