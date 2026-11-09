# Reviewing Changes

`konstruct diff` answers "what does this branch do to the data?" For each dbt model, SQL file and DAG file that changed, it analyses the base and current versions and compares each output column's canonical expression: the expression with every CTE, subquery and alias inlined down to physical columns. Reformatting, renaming a CTE or reordering columns is therefore not a change, and a real change is shown in full. Join, filter and grouping conditions are compared the same way. A removed column is breaking when a downstream model's current SQL still reads it, so a change that also updates its consumers is not flagged.

The base is the merge base with main unless `--base` names another commit or a date: `konstruct diff --base 2026-10-01 --head HEAD` shows what changed in the data stack since the first of October, column by column, taking the last commit on the current branch before that date begins. A date and time (`2026-10-01 14:30`) is also accepted; both are read in the machine's local time zone, and a branch or tag named like a date is taken as that ref. JSON output keeps the date as `base` and gives the commit it stands for as `base_commit`.

![konstruct diff on a branch of the example project](../images/demo-diff.gif)

```text
$ konstruct diff
1 breaking change, 1 column changed, 1 column removed, 1 column added, row logic changed in 1 relation, 1 DAG changed, 1 exposure affected.

models/orders.sql  (model orders, modified)
  rows     + WHERE: jaffle_shop.main.stg_orders.status <> 'returned'
           affects 10 tests, 1 exposure and 1 task downstream (exposures: customer_dashboard)

models/staging/stg_payments.sql  (model stg_payments, modified)
  added    method
  changed  amount
             was  jaffle_shop.main.raw_payments.amount / 100
             now  jaffle_shop.main.raw_payments.amount / 100.0 * 1.2
           affects 7 columns, 3 relations, 5 tests, 1 exposure and 2 tasks downstream (exposures: customer_dashboard)
  removed  payment_method
           BREAKS orders, which still reads it
           affects 4 columns, 1 relation, 5 tests, 1 exposure and 1 task downstream (exposures: customer_dashboard)

dags/jaffle_shop_daily.py  (DAG jaffle_shop_daily, modified)
  runs     task `dbt_run_marts`: no longer runs orders
```

`diff` also works out what dbt has to rebuild: the changed models, the models whose columns, joins or filters are downstream of a changed column, and the tests on them. It prints that as a `dbt build --select` command (or just the selection, with `--format selector`) and compares it with what `state:modified+` would build: on the example above it builds 3 of the 4 models, because the reformatted `stg_customers` needs nothing. A test builds the project with dbt before and after a change and checks that every model whose data changed is in the selection. It also reads dbt YAML, reporting tests, sources and exposures added or removed and descriptions changed.

dbt models are compared by rendering their Jinja without dbt (see How Lineage Works), so `diff` needs no warehouse and no credentials. `--json-file` writes the result as JSON beside any format, including the list of breaking changes, for scripts.
