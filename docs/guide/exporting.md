# Exporting

`konstruct export` hands the lineage to other tools:

| Format | What it writes | Use |
|--------|----------------|-----|
| `openlineage` | One OpenLineage `JobEvent` per built table, as JSON lines: inputs, outputs with schema and column lineage facets, each column's input fields with `DIRECT` transformations (`IDENTITY`, `TRANSFORMATION` or `AGGREGATION`, with the expression), and the columns used in joins, filters and grouping as dataset-wide `INDIRECT` dependencies (`JOIN`, `FILTER`, `GROUP_BY`) | Load into Marquez, DataHub, OpenMetadata or any OpenLineage consumer |
| `mermaid` | A `flowchart` of tables, models and tasks, or of columns with `--columns` | Documentation, pull requests, GitHub renders it inline |
| `dot` | A Graphviz graph, with column ports under `--columns` | Large static diagrams |
| `csv` | One row per column edge: relation, column, upstream relation and column, transform, verification and expression | Spreadsheets, or a lineage table in the warehouse |
| `ossie` | The [ontology](ontology.md) as an Apache Ossie semantic model: datasets, relationships, and each measure as a metric with its composed formula and a `KONSTRUCT` extension for status, owner and verification (see [Apache Ossie Models](ossie.md)) | Semantic layers and BI tools that read Ossie |
| `owl` | The ontology as OWL 2 in Turtle: entities as classes, their columns as data properties, relationships as object properties, and measures as annotations (see [below](#owl-and-turtle)) | Knowledge graphs, ontology editors and platforms that import RDF |
| `semantic-view` | The ontology as one Snowflake `CREATE OR REPLACE SEMANTIC VIEW` statement: tables, relationships, dimensions and metrics (see [below](#snowflake-semantic-view)) | Snowflake semantic views and Cortex Analyst |
| `metric-view` | The ontology as Databricks metric views, one `CREATE OR REPLACE VIEW ... WITH METRICS` statement per table (see [below](#databricks-metric-views)) | Unity Catalog metric views |

A [plugin](plugins.md#an-export-format) the project enables can add a format, which `konstruct export <format>` writes as it does these.

```bash
konstruct export openlineage -o lineage.jsonl --namespace duckdb://analytics
konstruct export mermaid -s orders.amount --columns > docs/orders-amount.mmd
```

The tests validate every OpenLineage event, and its column lineage and schema facets, against the OpenLineage 2-0-2 JSON schemas.

## The Ontology for Each Platform

`ossie`, `owl`, `semantic-view` and `metric-view` write the same [ontology](ontology.md), so one ontology, derived from the code and `definitions.yml`, feeds each platform in its own format. They carry the same entities: each relation that holds an entity is a dataset, a logical table or a metric view keyed by the entity's key, and in OWL the entity is a class. A project with no `definitions.yml` exports the entities and relationships Konstruct derives from keys, tests and joins, and the entities and measures of any [ontology pack](ontology-packs.md) it matches. Nothing is invented: a description, synonym, grain or filter is written only when the ontology holds it. Each export takes the whole ontology, so `--select` does not apply, and an ontology with no entities or measures, or whose every measure is left out, exports nothing, says why, and exits with status 1.

The `owl`, `semantic-view` and `metric-view` files open with comments that list what each leaves out and why; the `ossie` export names each measure it leaves out on standard error (see [Apache Ossie Models](ossie.md)). The `ossie`, `semantic-view` and `metric-view` exports leave out, and `owl` writes without its formula, a measure whose composed formula, by what the graph records of the queries on the way, would count other rows than the measure does. Each relation on the way from the measure's column down to source columns is checked:

- a `WHERE`, `HAVING`, `QUALIFY`, `LIMIT`, `OFFSET`, `DISTINCT`, anti join, `UNION` that removes duplicates, `INTERSECT` or `EXCEPT` changes the rows;
- a join keeps them only when the joined side's column is known to be unique (a `unique` test, a primary key or an identifier field), so that it matches at most one row;
- an inner join, or an outer join that reads the measure's table only on its nullable side (or joins it through such a table), also drops rows unless a `not_null` test and a `relationships` test on the foreign key, targeting the joined column, show that every row matches;
- a self join keeps them only when both its columns are unique, a join between two tables the measure reads is checked both ways, and a table read with no such join condition, as a cross join reads it, changes them;
- a source that makes rows of its own anywhere in the relation's query, a table function (`generate_series`, `range`, a Snowflake `TABLE(FLATTEN(...))`), `VALUES`, `UNNEST`, `LATERAL` or `LATERAL VIEW`, changes them.

Konstruct does not record the rows a `PIVOT` or `UNPIVOT` makes or a recursive CTE adds, so a measure composed through one is not left out for them; a query whose SQL Konstruct could not analyse records nothing to check: its formula does not reach source columns, so its measure is left out for that reason.

A measure is composed from another column bound to it when its first column's way down changes the rows. A measure that aggregates an aggregate (`AVG(SUM(...))`), as a formula composed across two grains does, or that combines the rows of a `UNION ALL`, is left out too.

```text
konstruct export owl -o shop.ttl --namespace https://example.com/shop
konstruct export semantic-view -o semantic_view.sql
konstruct export metric-view -o metric_views.sql
```

### OWL and Turtle

| Ontology | OWL |
|----------|-----|
| Entity | An `owl:Class`, labelled with its name (`rdfs:label`) and term (`skos:prefLabel`), with the term's definition (`skos:definition`), its key as `owl:hasKey` when a test, primary key or identifier field shows it to be unique, and the relations that hold it and the evidence for it as annotations |
| Property | An `owl:DatatypeProperty` for each column of the entity's relations, with the class as its domain, a range from the OWL 2 datatype map when every column of that name has the same known type, its description, and the columns it is read from |
| Relationship | An `owl:ObjectProperty` from one class to the other: functional when many-to-one, and inverse functional as well when one-to-one, with the joining columns, whether a test vouches for it, and its evidence |
| Measure | A named individual with its definition, synonyms (`skos:altLabel`), formula composed to source columns, grain, unit, currency, filters, status, owner, approval date and columns; its entity's class names it with `konstruct:measure` |
| Broader term | `rdfs:subClassOf`, when both terms are entities |

IRIs are stable: each name is under the ontology's IRI, `urn:konstruct:<project>` unless `--namespace` gives another, and an entity or measure whose term was read from a [SKOS or OWL vocabulary](definitions.md) keeps that concept's IRI. `--namespace` must be an absolute IRI. Konstruct's annotation properties are declared in the file under `https://kodexlabsio.github.io/konstruct/vocabulary#`. Left out: the columns of relations that hold no entity, which appear only in formulas; a key not known to be unique, such as one only a term names; the range of a date or time property, since the OWL 2 datatype map has no date or time; the ontology's rules and violations (`konstruct ontology` lists them); and a broader term that is not an entity. Konstruct reads the file back as a vocabulary, each class an entity term.

### Snowflake Semantic View

| Ontology | Semantic view |
|----------|---------------|
| Relation holding an entity, or read by a measure | A logical table under `TABLES`, with the entity's name and term as `WITH SYNONYMS` and the relation's description as `COMMENT`. A relation that holds the keys of several entities holds the one named for it (`orders` holds *order*), as relationships read it; that entity's key is its `PRIMARY KEY` when a test, primary key or identifier field shows it to be unique, and the other entities' unique keys are `UNIQUE` |
| Column | A dimension under `DIMENSIONS`, with its description |
| Relationship to a primary key or unique key | `RELATIONSHIPS`: `orders (customer_id) REFERENCES customers (customer_id)` |
| Aggregate measure that reads one table | A metric of that table under `METRICS`, its formula composed to source columns in Snowflake SQL, the term's name and synonyms as `WITH SYNONYMS`, and a `COMMENT` with its definition, grain, required filters and unit |

Names that differ only by case are kept apart, since unquoted Snowflake names are not case sensitive, and a reserved word is quoted. Left out: a key not known to be unique; a measure that is a per-row formula, whose formula does not reach source columns, that reads more than one table, or that the rule above leaves out; a relationship whose key is not known to be unique, that refers to a column the target does not key, that joins a table to itself, or that would close a cycle, which Snowflake does not allow; facts, since Konstruct does not tell facts from dimensions; and data types, owners, status and verification, which a semantic view has no place for.

### Databricks Metric Views

Each table above is the `source` of one metric view, `<table>_metrics`, written as YAML version 1.1 inside a `CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE YAML AS $$ ... $$` statement:

| Ontology | Metric view |
|----------|-------------|
| Relation | `source`, with its description as `comment` |
| Relationship to a primary key or unique key | A join under `joins` to each table the source refers to, named after that table (or after the referring column, such as `billing_customer`, when the source refers to the table more than once), `on` the joining columns, with `rely: at_most_one_match`; the tables a joined table refers to have views of their own and are not joined again |
| Column | A field under `fields`: each column of the source, as `source.<column>`, then each column of every joined table, named `<join>_<column>` |
| Aggregate measure that reads the source | A measure, its formula in Databricks SQL with each column as `source.<column>`, the term's name as `display_name`, up to ten synonyms, and a `comment` with its definition, grain, required filters and unit |

Names that differ only by case are kept apart, and keywords are quoted. Left out: the same measures and relationships as the semantic view, apart from cycles, since a view joins only the tables its source refers to; a synonym or display name over 255 characters; a comment, synonym or display name that holds `$$`, which would end the statement; and data types, owners, status and verification.
