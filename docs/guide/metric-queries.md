# Compiling Metric Queries

`konstruct compile-metric` writes the SQL for a metric asked for by the business's entities and properties rather than by tables: lifetime value by customer, order amount by a customer's first name and month. It compiles the question through the [ontology](ontology.md) and never runs anything. The SQL joins only along relationships the project shows many-to-one, onto keys shown unique, never adds up a value repeated from a coarser grain, always applies the filters the metric's definition requires, and comes with every fact it rests on and how far to trust each one. What cannot be compiled that way is refused, with the reason.

```text
$ konstruct compile-metric order amount --by customer.first_name --by month --where "status = 'completed'" --since 2018-02-01
Compiled Order amount by customer.first_name, month

  relation    orders  one row per order
  read        added up with SUM
  confidence  declared
  dialect     duckdb

Path (1)
  order -> customer  many-to-one  orders.customer_id = customers.customer_id  declared

Facts (5)
  metric        Order amount                                declared
  column        orders.amount                               declared
  grain         orders: one row per order                   declared
  relationship  orders.customer_id = customers.customer_id  declared
  key           customers.customer_id                       declared

SQL
  SELECT
    customers.first_name AS first_name,
    DATE_TRUNC('MONTH', orders.order_date) AS order_date_month,
    SUM(orders.amount) AS order_amount
  FROM jaffle_shop.main.orders AS orders
  LEFT JOIN jaffle_shop.main.customers AS customers
    ON customers.customer_id = orders.customer_id
  WHERE
    orders.status = 'completed' AND orders.order_date >= CAST('2018-02-01' AS DATE)
  GROUP BY
    customers.first_name,
    DATE_TRUNC('MONTH', orders.order_date)
  ORDER BY
    customers.first_name,
    DATE_TRUNC('MONTH', orders.order_date)
```

## Asking

| Part | Written as | Means |
|------|------------|-------|
| Metric | `order amount`, `lifetime value`, `orders.amount` | A metric as [`konstruct metric`](ontology.md#entities-metrics-and-relationships) finds it: a name, a synonym, or a column that holds it. A name that finds several defined metrics is refused until one is named |
| `--by` | `customer` | An entity, grouped by its key |
| | `customer.first_name`, `first_name` | A property of an entity: a column of a relation holding it. A bare name is a column of the metric's own relation, else the property of the one entity reached that has it; one that several entities have is refused, naming them |
| | `month`, `order_date:month` | A time grain (`day`, `week`, `month`, `quarter` or `year`) of the metric's time property, or of the one named before the colon. Weeks start on Monday in every dialect, and a timestamp is truncated as a timestamp |
| `--where` | `"customer.first_name = 'Ann'"` | A condition on properties, as SQL, each property named as in `--by`: a comparison, `IN`, `BETWEEN`, `LIKE` or `IS`, or a `NOT`, `AND` or `OR` of them. A bare property, a subquery, an aggregate or a window is refused |
| `--since`, `--until` | `2024-01-01` | The dates covered, on the metric's time property whatever `--by` groups by: from `--since`, up to and not including `--until`, each written as a value of the property's own type |
| `--time` | `order.order_date` | The metric's time property, for a bare grain and for the range. By default it is the one date or time column of the metric's relation; with none or several, a bare grain or a range is refused until one is named |
| `--dialect` | `snowflake` | The dialect to write, any sqlglot reads; by default the project's |

`--format sql` prints only the SQL, for a pipe; a refusal then goes to standard error. `--json` (or `--format json`) gives the whole result as data.

## How the SQL Is Written

**The metric's rows.** A metric that a column holds (a business term's column, or an aggregate or a measure column Konstruct proposes) is read from that column's relation. Of its columns, the one on a relation that holds the metric's grain, with a key shown unique, comes first, and a mart before its staging model. When the query groups by a key of that relation shown unique and never null (a `unique` and a `not_null` test that fail on any row, a primary key something enforces, or `konstruct verify`), such as `--by customer` for a metric held per customer, each row is one group and the column is read as it is. Otherwise its values are added up with `SUM`, which is right only for values that add up: a sum or a count does, an average, a distinct count, a price or a balance does not, and such a metric is refused unless the query keeps one row per row of its relation. A metric held at another grain than its relation's rows is refused: a value per customer on every order row, whether the definition states the grain or the column copies the value from a relation of another grain, would be counted once per order. A metric whose own formula aggregates the rows of one relation (a dbt semantic layer metric, an entity count) is that formula over those rows, grouped.

**The joins.** From the metric's grain, the joins follow only the steps the project shows join each of its rows to at most one row: a relationship from its referring side to the key it refers to (many-to-one), a one-to-one either way, and a step between two relations holding the same entity, on that entity's key. Every key a join lands on is shown unique, by a test (one with no `where` config that fails on any row: not `severity: warn`, nor an `error_if` that lets rows fail), a primary key something enforces (not one on Snowflake, BigQuery, Redshift, Databricks, Spark, Hive, Trino, Athena, ClickHouse (whose primary key is a sort key) or T-SQL (SQL Server, Synapse and Fabric, taken together), a dbt constraint outside an enforced contract, or an Iceberg identifier field) or [`konstruct verify`](verify.md), and is listed among the facts. The SQL is as right as those statements are: a key that repeats in the data despite its test still repeats rows, which `konstruct verify` finds. Of those paths, the one with the fewest relationships comes first, then the most trusted. Joins are `LEFT JOIN`, so a row whose reference finds nothing still counts, under an empty group. A condition on a property of a joined relation that is not true of the NULL such a row has there leaves the row out, as it is in the `WHERE` clause: a `--where` (the payments of an order whose customer is missing, for `--where "customer.region <> 'North'"`), a filter the metric's definition requires on a joined relation, or `--since` and `--until` on a joined `--time` property. `flags` says so for each. To keep those rows, also allow the missing value, as `customer.region <> 'North' OR customer.region IS NULL`; a condition true of a missing value is not flagged. A relation whose single grouping column names its grain joins that entity's relation on its key.

**Refusals.** When every path to a property could repeat the metric's rows, the query is refused, naming the link: from a customer to its orders is one-to-many, so customer lifetime value by order status would count each customer once per order. A relationship whose cardinality is not known, and one verify found the data contradicting, are never walked; nor is a key verify found repeated. A refusal has no SQL, and `konstruct compile-metric` exits 1:

```text
$ konstruct compile-metric lifetime value --by order.status
Refused Customer lifetime value by order.status

  by order.status  customer -> order is one-to-many: each customer has many order rows, so joining them would repeat the metric on each, and count it more than once
    link  orders.customer_id = customers.customer_id
```

**Required filters.** Every filter the metric's definition requires (`filters` in [`definitions.yml`](definitions.md)) goes into the SQL's `WHERE` clause, on the relations the query reads: a column of another relation holding the same entity is joined on its key. A filter that the SQL of the metric's column already applies to the rows it aggregates (a model that sums only orders not returned) is listed as applied there, with the model, when it holds on every source row the column's value is computed from: on every input of the value (not on only one side of a COALESCE or a sum, nor on one branch of a union, nor in a CTE beside it), on the alias the value is read through (the ON of a `LEFT JOIN` holds only on the joined side, a filter on one alias of a table or CTE says nothing of another alias of it, and nor does a filter on a column copied from another reading of the table, in a CTE, a subquery or an upstream model, such as the status of the order before each order), before any window or aggregate the value comes from rather than above it, whether that window or aggregate is in the same SQL or in an upstream model, in every statement that writes the relation, and on every upstream column the value is derived from. That is Konstruct's reading of the SQL, so the filter's fact is inferred and flagged; one it cannot show held, such as one in a graph an earlier Konstruct built, is not taken as applied. A filter that can be placed neither way refuses the query.

Some filters that do hold on the data are not shown held, so the metric is refused (and `check-sql` reports the filter missing from a query of the column): a filter on a grouping column above the aggregate it groups, such as `WHERE status <> 'returned'` on a CTE grouped by customer and status; a filter on a grouping column of a grouped upstream model, in a model that reads it; a filter on another alias of the same table joined on its key, such as `WHERE o2.status <> 'returned'` with `o2.order_id = o.order_id`, for a sum of `o.amount`; an inner join to a subquery that applies the filter; `EXISTS` with the filter inside it; and a value whose unfiltered parts cancel out, such as `b.s + b2.s - b2.s`. Apply the filter to the rows before they are aggregated, in the model or in the query. In the same way, a value its relation's SQL copies across a `LEFT`, `RIGHT` or `FULL JOIN`, on either side of it, is refused unless the relation it is copied from is shown to hold the grain of the metric's relation, even where the join cannot repeat it.

**Facts and trust.** `facts` lists what the SQL rests on: the metric, its column, the grain of its relation, each relationship and key the joins use, and each required filter, each with its confidence (`verified`, `declared` or `inferred`, as in [Confidence](ontology.md#confidence)). The result's `confidence` is the weakest of them, and `flags` says in words which are only inferred: a metric Konstruct proposes from the code, a relationship read from a join alone, a grain a definition states for a relation that holds no key of it. A project with no `definitions.yml` still compiles its proposed metrics, flagged.

## Interfaces

| Interface | Use |
|-----------|-----|
| `konstruct compile-metric` | As above. Exit status 0 when the SQL is written, 1 when the query is refused and 3 when the name finds no metric |
| MCP `compile_metric` | `metric`, `by`, `where`, `since`, `until`, `time` and `dialect`, returning what `--json` gives, for an agent to answer a metric question with SQL that follows the ontology |
| Python | `Project.compile_metric(metric, by=..., where=..., since=..., until=..., time=..., dialect=...)` returns a `Compiled` with `sql`, `path`, `filters`, `facts`, `confidence`, `flags` and `refused` |
