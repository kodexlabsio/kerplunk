# The Ontology

Konstruct derives an ontology of the data stack from its graph: the business objects (entities), what is measured about them (measures), how they relate, where each measure comes from and how far to trust it, and the rules the build holds it to. Most of it is read from the code and the tests rather than declared; `definitions.yml` names what code cannot say. Every build writes it to `konstruct-out/ontology.yml`, and agents read it through the `ontology` MCP tool.

```text
$ konstruct ontology
3 entities, 2 measures, 2 relationships, 0 rule violations.

Entities
  customer  key customer_id: customers, stg_customers  (term 'Customer')
  order  key order_id: orders, stg_orders
  payment  key payment_id: stg_payments

Measures
  Customer lifetime value  [approved, Priya Shah (Head of Finance), one row per customer]
    customers.customer_lifetime_value = SUM(raw_payments.amount / 100)  verified 2/2
    customer_ltv.lifetime_value = SUM(raw_payments.amount / 100)  verified 3/3
  Order amount  [approved, Priya Shah (Head of Finance), one row per order]
    orders.amount = SUM(raw_payments.amount / 100)  verified 2/2

Relationships
  order -> customer  many-to-one  orders.customer_id = customers.customer_id  (tested)
  payment -> order  many-to-one  stg_payments.order_id = stg_orders.order_id
```

## What It Contains

| Part | Where it comes from |
|------|---------------------|
| **Entities** | A column named like an identifier (`customer_id`, `order_key`, `customer_uuid`, `CategoryId`, `id`, `uuid`) with a dbt `unique` test is the key of an entity named after it: `customer_id` is the key of *customer*, and a bare `id` or `uuid` takes the relation's name (`stg_customers.id` gives *customer*, `stg_stripe__payments.id` gives *payment*). A single-column primary key is a key whatever its name: a dbt `primary_key` constraint, a DuckDB or Postgres primary key, or an Iceberg identifier field. A unique column such as `email` is not a key, and neither is one column of a composite key. The relations keyed by it hold one row per instance. A term of kind `entity` names an entity explicitly, and a measure's grain (`one row per store`, or `one row per store per day`) makes an identifier named for that entity in the measure's relations its key |
| **Measures** | Business terms with a formula: a term of kind `measure`, or one whose bound columns are computed rather than copied. Each bound column carries its formula composed down to source columns (as [`explain`](explain.md) gives it), the sources it reaches, how many lineage edges on the way [`verify`](verify.md) confirmed, and its usage score when [usage](usage.md) is on. The measure's grain (`one row per customer`) links it to its entity |
| **Relationships** | A dbt `relationships` test, or a join that matches one entity's column to another entity's key, says that an order belongs to a customer. Each relationship has the columns that join them (those a test names, if one does), whether a test vouches for it (`tested`), its cardinality and the tests and joins that show it. The cardinality is many-to-one, or one-to-one when both columns are unique; it is left out (`cardinality unknown`) when nothing shows the key is unique |
| **Provenance and trust** | For each measure column: its sources, whether its formula reaches source, its verified edges and its usage; for each measure: its status, owner and approval date |
| **Rules** | What the build holds the ontology to. A violation is a build warning, so `konstruct build --strict` fails on it |

## Rules

| Rule | Requires |
|------|----------|
| `one-formula-per-measure` | Every column bound to a measure computes it the same way: the same sources, filters, joins and aggregation, compared as [`conflicts`](conflicts.md) does. A column derived from another bound column carries its definition and is not compared |
| `entity-keys-are-unique` | The key of an entity named in `definitions.yml` has a uniqueness test, is a primary key or is an identifier field |
| `entity-names-are-distinct` | A term of kind `entity` does not take the name of an entity whose keys it does not bind; bind those keys too, which makes them one entity, or choose another name |
| `bound-columns-exist` | Every column a business term names exists |
| `approved-terms-are-owned` | An approved term has an owner and an approval date |

## Naming Entities and Measures

Entities and measures are inferred when a term leaves `kind` out: a term bound only to entity keys is attached to that entity without renaming it, and a term bound to a computed column is a measure. `kind: entity` gives the entity the term's name, and the keys it binds become one entity (`stg_people.id` and `customers.customer_id` both under *customer*); `kind: measure` makes a measure; `kind: term` keeps a term out of both:

```yaml
version: 1
terms:
  - name: Customer
    kind: entity
    definition: A person with an account, whether or not they have ordered.
    columns: [customers.customer_id]
  - name: Customer lifetime value
    kind: measure
    definition: Total value of every payment a customer has made, in Australian dollars.
    grain: one row per customer
    columns: [customers.customer_lifetime_value, customer_ltv.lifetime_value]
```

## Using It

| Interface | Use |
|-----------|-----|
| `konstruct ontology` | The summary above. `--format yaml` for the file's contents, `--json` for structured output, `--strict` to exit 1 when a rule is broken |
| `ontology.yml` | Written by every build beside `graph.json`, and refreshed by `konstruct verify`. Its order is fixed, so it diffs cleanly in version control |
| MCP `ontology` | The whole ontology, or with `name` one entity (by its name or its term's) or measure and the relationships that touch it, for an agent to learn the shape of the business before it writes a query that joins or aggregates. Case, spaces and underscores are not told apart, and a plural finds the singular |
| Python | `Project.ontology()` returns an `Ontology` with `entities`, `measures`, `relationships`, `rules` and `violations`; `entity(name)` and `measure(name)` match names as the MCP tool does |
