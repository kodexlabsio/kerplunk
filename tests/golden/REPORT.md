# Konstruct report

Built <time> from `<root>` by Konstruct <version> in <seconds>s. SQL dialect: duckdb.

## Summary

| Input | Count |
|-------|-------|
| dbt projects | 1 |
| warehouses | 1 |
| dbt models | 5 |
| airflow dags | 1 |
| dagster assets | 0 |
| dagster ops | 0 |
| sql files | 0 |
| definitions | 5 |

| Node kind | Count |
|-----------|-------|
| column | 44 |
| dag | 1 |
| exposure | 1 |
| model | 5 |
| seed | 3 |
| table | 1 |
| task | 6 |
| term | 5 |
| test | 24 |

| Edge kind | Count |
|-----------|-------|
| contains | 44 |
| defines | 6 |
| derived_from | 36 |
| feeds | 21 |
| precedes | 5 |
| relates_to | 3 |
| runs | 38 |
| tests | 27 |

Column lineage covers 32 of 32 columns of models and SQL-built tables (100%).

## Most used columns

Columns with the most direct dependants (derived columns and joins).

| Column | Dependants |
|--------|------------|
| `stg_payments.amount` | 6 |
| `stg_orders.order_id` | 4 |
| `stg_payments.payment_method` | 4 |
| `stg_orders.order_date` | 3 |
| `customers.customer_lifetime_value` | 2 |
| `customers.number_of_orders` | 2 |
| `stg_customers.customer_id` | 2 |
| `stg_orders.customer_id` | 2 |
| `stg_payments.order_id` | 2 |
| `customers.customer_id` | 1 |

## Business terms

From definitions.yml. Problems with them are listed under Warnings.

| Term | Status | Owner | Columns |
|------|--------|-------|---------|
| Completed order | agreed | Tom Okafor (Operations Manager) | `orders.status` |
| Customer | approved | Tom Okafor (Operations Manager) | `customers.customer_id` |
| Customer lifetime value | approved | Priya Shah (Head of Finance) | `customer_ltv.lifetime_value`, `customers.customer_lifetime_value` |
| Order amount | approved | Priya Shah (Head of Finance) | `orders.amount` |
| Payment method | draft | none | `stg_payments.payment_method` |

4 columns hold an approved definition.

## Orphans

Relations with nothing upstream or downstream, and no task that runs them.

None.

## Untested models

dbt models and snapshots with no test on the model or any of its columns.

None.

## Columns with no lineage

Columns of models and SQL-built tables with no upstream column: constants, columns Konstruct could not resolve, or SQL it could not parse (see Warnings).

None.

## Documented columns missing from the warehouse

Columns described in dbt YAML that the warehouse does not have.

- `customers.total_order_amount`

## Warnings

None.
