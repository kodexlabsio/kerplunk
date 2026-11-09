# Explaining a Column

Lineage says which columns a value comes from. `konstruct explain` says how it is calculated. Each column's SQL is kept with its CTEs, subqueries and aliases inlined, then composed through every model and SQL task upstream, so the formula is stated in terms of the source columns:

```text
$ konstruct explain customer_ltv.lifetime_value
Formula of customer_ltv.lifetime_value (DOUBLE)

  = customers.customer_lifetime_value

In terms of sources
  = SUM(raw_payments.amount / 100)

Sources (1)
  raw_payments.amount

Step by step (3)
  customer_ltv.lifetime_value = customers.customer_lifetime_value  verified
      WHERE  customers.customer_lifetime_value > 0
  customers.customer_lifetime_value = SUM(stg_payments.amount)  verified
      GROUP BY      stg_orders.customer_id
      LEFT JOIN ON  stg_customers.customer_id = stg_orders.customer_id
      LEFT JOIN ON  stg_payments.order_id = stg_orders.order_id
  stg_payments.amount = raw_payments.amount / 100  verified
```

That chain crosses an Airflow SQL file, a dbt mart and a dbt staging model. The composed formula describes the value; which rows feed it is decided by the joins, filters and grouping listed under each step, which is why they are listed rather than folded in. For chains without joins, filters or aggregation, a test evaluates the composed formula directly on the source table and checks it gives the final model's values row for row. `describe` shows a column's expression and a model's conditions too, and the `explain` MCP tool answers "how is this number calculated?" for an agent.
