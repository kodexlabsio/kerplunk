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

An expression is read from `sql`, or else from its `OSSIE_SQL_2026` or `ANSI_SQL` dialect, or from a SQL dialect Konstruct knows (Snowflake, Databricks, BigQuery, Trino, ClickHouse, Postgres, Redshift), each in its own dialect. These are build warnings, so `konstruct build --strict` fails on them: a dataset whose source does not exist, a field with no expression Konstruct can read or that reads a column its source does not have, a metric that names a field the model does not declare, and a relationship that does not resolve, has unequal `from_columns` and `to_columns`, or joins on several fields. A metric with the same name as a term in `definitions.yml` keeps the definition from `definitions.yml`.

## Writing a Model

```bash
konstruct export ossie -o semantic.ossie.yaml
```

The export is one 0.2.0.dev0 model:

- a dataset for each relation that holds an entity, is joined by a relationship, or is a source of a measure, with its fields, their data types and descriptions, and the entity key as its primary key;
- the relationships between the entities whose key is known to be unique (the ontology gives them a cardinality); Ossie's `to_columns` are a key, so a relationship without one is left out;
- each aggregate measure as a metric. Its expression is its formula composed down to its source datasets, or for a metric read from an Ossie model its own expression, each field it names replaced by the field's expression over source columns. A metric that was scoped to a dataset, such as `COUNT(*)` on `emp`, is written under that dataset's `metrics`; the rest are model-scoped. The expression's `sql` is ANSI SQL, and when the warehouse's dialect has an Ossie name (Snowflake, Databricks, BigQuery, Trino, ClickHouse, Postgres, Redshift) the same expression in that dialect is under `dialects`. A metric read from Ossie as portable SQL keeps only its portable form, since translating it could change its meaning. Each metric has the term's definition as its description, its synonyms and grain in `ai_context`, and a `KONSTRUCT` custom extension holding its status, owner, approval date, entity, and each bound column with its formula, sources, verified lineage edges and usage score.

A measure whose formula does not reach source columns, or is a per-row or window formula rather than an aggregate, is left out, since an Ossie metric aggregates; each one left out is reported on standard error with the reason. Field data types map to Ossie's logical types; a type outside them, such as a struct, array, JSON or interval, is `Opaque` with the physical type in a `KONSTRUCT` custom extension. Names are made Ossie identifiers and kept distinct, so two metrics or relationships never share one. Two relations with the same name, such as `raw.orders` and `main.orders`, become two datasets, the second named after its full relation (`raw_orders`).

An ontology with no entities or measures has nothing to export: the command writes nothing and exits with status 1. The export is always the whole ontology, so it does not take `--select`.
