# Finding Conflicting Definitions

Most data stacks compute their key measures more than once: revenue in a finance mart, again in a marketing table, again in a dashboard extract. `konstruct conflicts` finds those columns and shows exactly how they disagree, before the numbers do.

```text
$ konstruct conflicts
1 conflicting definition among 5 computed columns.

revenue
  daily_revenue.revenue  vs  daily_performance.total_revenue
    Compared because they have names that mean the same thing; downstream weight 6.
    a  daily_revenue.revenue = SUM(orders.amount)
       in finance/revenue.sql; feeds 1 columns
    b  daily_performance.total_revenue = SUM(orders.amount)
       in marketing/daily.sql; feeds 1 tasks, 2 columns
       carried into weekly.revenue
    - filter only in daily_revenue.revenue: WHERE orders.status = 'completed'  (finance/revenue.sql)
```

- **Which columns are compared.** Computed columns whose names mean the same thing once qualifiers and synonyms are folded together (`revenue`, `total_revenue`, `sales_amount`), columns named like a business term or its synonyms in `definitions.yml`, and columns bound to the same term. Keys, plain copies and columns derived from one another are left out: a column built from another inherits its definition and is listed as *carried into* it.
- **What is compared.** Each column's formula in terms of source columns (from `explain`), the source columns themselves, the filters and joins on its whole route back to source with the file each is in, and the aggregation at the top. Pairs that agree on all of these are not reported. Formulas that compute the same thing count as the same however they are written (`SUM(price * quantity)` and `SUM(quantity * price)`), and a formula not shown to be the same comes with values at which the two give different results, or the name of the function they apply differently. Values of source columns alone show that the two differ; a witness that also sets an expression they share (a `CASE` condition, a subquery) reads *they differ if* those expressions *take these values*, which they may never do. Two formulas written the same are never a formula difference.
- **What "the same" means.** The same result for every value of the source columns, NULL included, and for every set of rows, none included. Arithmetic is exact, so floating-point rounding is not taken into account, and a formula that divides by zero is compared only where it is defined. Any other function is compared by its name and arguments. A column with a `not_null` test is never NULL, except where a relation with an outer join (`LEFT`, `RIGHT` or `FULL`) reads it: there it may be NULL where no row matches, on either side of the join. Two formulas that differ only in that are reported as the join alone, when only one route has it. A volatile function such as `RANDOM()` is a different function at each place it is called. Two formulas that agree only when no source value is NULL and every group has rows, such as `SUM(amount) - SUM(discount)` and `SUM(amount - discount)`, are reported as a formula difference noted *the same unless a source value is NULL or there are no rows*, with the values that show it.
- **What comes first.** Conflicts are ranked by what depends on each side: exposures and dashboards, approved business terms, orchestration tasks and downstream columns.

`CONFLICTS.md` (one section per conflict, with a table of both formulas and a list of differences) and `conflicts.json` are written beside the graph, ready to take into a meeting where the definition gets agreed and recorded in `definitions.yml`. The `conflicts` MCP tool gives an agent the same list, so it can check whether a measure already exists before adding another.
