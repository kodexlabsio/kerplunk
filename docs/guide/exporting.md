# Exporting

`konstruct export` hands the lineage to other tools:

| Format | What it writes | Use |
|--------|----------------|-----|
| `openlineage` | One OpenLineage `JobEvent` per built table, as JSON lines: inputs, outputs with schema and column lineage facets, each column's input fields with `DIRECT` transformations (`IDENTITY`, `TRANSFORMATION` or `AGGREGATION`, with the expression), and the columns used in joins, filters and grouping as dataset-wide `INDIRECT` dependencies (`JOIN`, `FILTER`, `GROUP_BY`) | Load into Marquez, DataHub, OpenMetadata or any OpenLineage consumer |
| `mermaid` | A `flowchart` of tables, models and tasks, or of columns with `--columns` | Documentation, pull requests, GitHub renders it inline |
| `dot` | A Graphviz graph, with column ports under `--columns` | Large static diagrams |
| `csv` | One row per column edge: relation, column, upstream relation and column, transform, verification and expression | Spreadsheets, or a lineage table in the warehouse |
| `ossie` | The [ontology](ontology.md) as an Apache Ossie semantic model: datasets, relationships, and each measure as a metric with its composed formula and a `KONSTRUCT` extension for status, owner and verification (see [Apache Ossie Models](ossie.md)) | Semantic layers and BI tools that read Ossie |

A [plugin](plugins.md#an-export-format) the project enables can add a format, which `konstruct export <format>` writes as it does these.

```bash
konstruct export openlineage -o lineage.jsonl --namespace duckdb://analytics
konstruct export mermaid -s orders.amount --columns > docs/orders-amount.mmd
```

The tests validate every OpenLineage event, and its column lineage and schema facets, against the OpenLineage 2-0-2 JSON schemas.
