# Apache Ossie Models

[Apache Ossie](https://ossie.apache.org/) (formerly the Open Semantic Interchange) is a YAML format for semantic models: datasets over physical tables, their fields, the relationships between them and the metrics computed over them. Konstruct reads Ossie models as an input and writes the [ontology](ontology.md) as one, adding what the format leaves out: where each metric comes from, who owns it and whether its lineage is verified.

## Reading Models

Every `*.ossie.yaml` (or `*.ossie.yml`) file in the repository is read on each build; `[ossie] paths` in `konstruct.toml` pins the files or directories. Both the released 0.1.1 shape, with models in a `semantic_model` list, and the 0.2.0 draft, with one model at the root, are read.

| Ossie | In Konstruct |
|-------|--------------|
| Dataset | The relation its `source` names, such as `main.orders` |
| Field | The source columns its expression reads |
| Metric | A business term of kind `measure`, with its description, its `ai_context` synonyms and its own expression. The source columns it reads through its fields are its inputs: `define` and `describe` do not list it as defining them, and `conflicts` does not compare them. A metric whose expression does not resolve is left out |
| Primary key | The key of an entity, so the dataset's relation holds one row per instance. A primary key of several fields is not an entity key, as no one field is unique |
| Relationship | A relationship between the entities of the two datasets, from `from` to `to`, on one field each |

An expression is read from `sql`, or else from its `OSSIE_SQL_2026` or `ANSI_SQL` dialect, or from a SQL dialect Konstruct knows (Snowflake, Databricks, BigQuery, Trino, ClickHouse, Postgres, Redshift), each in its own dialect. These are build warnings, so `konstruct build --strict` fails on them: a dataset whose source does not exist, a field with no expression Konstruct can read or that reads a column its source does not have, a metric that names a field the model does not declare, a metric defined twice in one model (the first is kept), a model-scoped metric that names a field without its dataset (the specification requires `dataset.field`, as in `SUM(sales.amount)`, even when only one dataset has the field), and a relationship that does not resolve, has unequal `from_columns` and `to_columns`, or joins on several fields. A metric with the same name as a term in `definitions.yml` keeps the definition from `definitions.yml`.

## Writing a Model

```bash
konstruct export ossie -o semantic.ossie.yaml
```

The export is one 0.2.0.dev0 model:

- each dataset read from an Ossie model, written back as authored: its name, source, fields and every other property, with a field added after them for any other column of its relation that a rewritten metric or relationship reads. When two models read the same relation, or give the same dataset name to two relations, the first file read, by path, is the one written back;
- a dataset for each other relation that holds an entity, is joined by a relationship, or is a source of a measure, with its fields, their data types and descriptions, the key of the entity it holds as its primary key when a test, primary key or identifier field shows it to be unique (of the entities keyed in it, the one named for the relation, as relationships read it), the other entities' unique keys as `unique_keys`, and the entity's name in a `KONSTRUCT` custom extension, since Ossie has no entities of its own;
- each relationship read from an Ossie model whose two datasets are written back from that model and which names only their fields, as authored, a relationship of a dataset to itself and one on several fields included;
- the other relationships between the entities whose key is known to be unique (the ontology gives them a cardinality); Ossie's `to_columns` are a key, so a relationship without one is left out. One read from an Ossie model keeps its name;
- each aggregate measure as a metric. A metric read from an Ossie model whose datasets are all written back under their own names from that same model is written as authored: its name (made an Ossie identifier when it is not one), expression and dialects, data type and other properties, with Konstruct's custom extension added; an `ai_context` written as a string stays one. Any other metric's expression is its formula composed down to its source datasets, or for a metric read from an Ossie model its own expression, each field it names replaced by the field's expression over source columns. A metric that was scoped to a dataset, such as `COUNT(*)` on `emp`, is written under that dataset's `metrics`; the rest are model-scoped. The expression's `sql` is ANSI SQL, and when the warehouse's dialect has an Ossie name (Snowflake, Databricks, BigQuery, Trino, ClickHouse, Postgres, Redshift) the same expression in that dialect is under `dialects`. A metric read from Ossie as portable SQL keeps only its portable form, since translating it could change its meaning. Each metric has the term's definition as its description, its synonyms and grain in `ai_context`, and a `KONSTRUCT` custom extension holding its status, owner, approval date, entity, and each bound column with its formula, sources, verified lineage edges and usage score.

A measure whose formula does not reach source columns, or is a per-row or window formula rather than an aggregate, is left out, since an Ossie metric aggregates, and so is a measure composed from its columns whose composed formula would count other rows than the measure does: one that aggregates an aggregate or combines the rows of a `UNION ALL`, or whose way down passes a filter, a set operation or a join that drops or repeats rows (see [Exporting](exporting.md)), unless another column bound to it passes none; each one left out is reported on standard error with the reason. Field data types map to Ossie's logical types; a type outside them, such as a struct, array, JSON or interval, is `Opaque` with the physical type in a `KONSTRUCT` custom extension. Names are made Ossie identifiers and kept distinct, so two metrics or relationships never share one. Two relations with the same name, such as `raw.orders` and `main.orders`, become two datasets, the second named after its full relation (`raw_orders`).

When every dataset and metric exported comes from one Ossie model, the document keeps that model's name and description; otherwise it is named after the project.

An ontology with no entities or measures, or whose every measure is left out, has nothing to export: the command writes nothing, says why, and exits with status 1. The export is always the whole ontology, so it does not take `--select`.

## Round Trips

A model that Konstruct reads and writes back keeps what it authored, except as the list above says. On Ossie's portability ladder (`compliance/portability` in the Ossie repository, rungs M0 to M3: one `SUM` metric, a calculated field, the core aggregates and a filtered aggregate), every field and metric of the qualified fixtures comes back with the dialect tag it was authored in (`ANSI_SQL` or `OSSIE_SQL_2026`) and the same expression, and the datasets come back unchanged. The ladder's bare variants write model-scoped metrics without their dataset, such as `SUM(amount)`, which the specification does not allow; each is a build warning and is left out, while the datasets and fields are written back.
