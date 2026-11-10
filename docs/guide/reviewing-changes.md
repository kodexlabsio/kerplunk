# Reviewing Changes

`konstruct diff` answers "what does this branch do to the data?" For each dbt model, SQL file and DAG file that changed, it analyses the base and current versions and compares each output column's canonical expression: the expression with every CTE, subquery and alias inlined down to physical columns. Reformatting, renaming a CTE or reordering columns is therefore not a change, and a real change is shown in full. Join, filter and grouping conditions are compared the same way. A removed column is breaking when a downstream model's current SQL still reads it, directly or through `SELECT *`, whether or not the warehouse has the relation yet, so a change that also updates its consumers is not flagged.

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

## When a Formula Changed

`konstruct history <table.column>` answers "when did this number's calculation change, and who approved it?" It reads the git history of the file that defines the column (a dbt model, a SQL file, or the SQL file a DAG task runs), following renames, and works out the column's formula at each commit and at its parent with the same analysis as the build, so a commit that only reformats the SQL, or a merge, is not listed. It also reads the history of every `definitions.yml` that binds a business term to the column, and lists each change to the term's status, approval date, owner and definition with the commit's author and date. Uncommitted edits come last.

```text
$ konstruct history ltv.total
ltv.total: 2 formula changes; 'lifetime value' last approved by Grace in 745d991, before the latest formula change
  defined in ltv.sql
  2026-01-05 c9e560a Ada: Add lifetime value
    added: SUM(orders.amount)
    term 'lifetime value': definition none -> What a customer has paid us.
    term 'lifetime value': owner none -> Ada
    term 'lifetime value': status none -> draft
  2026-02-01 745d991 Grace: Approve lifetime value
    term 'lifetime value': owner Ada -> Grace
    term 'lifetime value': status draft -> approved
  2026-04-01 d71c2ce Linus: Net of refunds
    was: SUM(orders.amount)
    now: SUM(orders.amount - orders.refund)
```

The summary says whether a bound term was approved after the latest formula change, so a formula that changed after its approval stands out. Commits are ordered by ancestry, not by date, so a change rebased onto a branch after an approval comes after it. A term is approved by a commit that makes its status `approved`, or that moves its `approved` date while it stays approved; a term that first appears already approved, such as a renamed one, is not an approval. Where the history is incomplete (a shallow clone, or more commits than `--limit`, default 50), or a version's SQL cannot be analysed, a note says so. `--json` gives the same result for scripts and agents, which can also ask through the `history` MCP tool.
