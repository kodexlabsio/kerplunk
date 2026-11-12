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

That chain crosses an Airflow SQL file, a dbt mart and a dbt staging model. The composed formula describes the value; which rows feed it is decided by the joins, filters and grouping listed under each step, which is why they are listed rather than folded in. Each condition is labelled with where it applies, as [Reviewing Changes](reviewing-changes.md) describes. A self-join's readings of a relation have names of their own, `"orders#1"` and `"orders#2"`, as [Reviewing Changes](reviewing-changes.md) describes. Composed through a reading of a model, a formula names what it reads through that reading: a model that reads `base` twice, where `base` reads `orders`, composes to `MAX(("orders#(base#1)".amount * 2) - ("orders#(base#2)".amount * 2))`, apart from the readings the model makes of `orders` itself. The same holds of a model read once where the query also reaches what it reads another way: a model that joins `m` to `orders` names the orders `m` reads `"orders#(m)"`. For chains without joins, filters or aggregation, a test evaluates the composed formula directly on the source table and checks it gives the final model's values row for row. `describe` shows a column's expression and a model's conditions too, and the `explain` MCP tool answers "how is this number calculated?" for an agent.

## In Plain English

`konstruct explain COLUMN --english` adds the same formula worded in plain English, for a reader who does not read SQL:

```text
$ konstruct explain orders.credit_card_amount --english
...
In plain English
  orders.credit_card_amount is the sum of ([amount] divided by 100 if [payment method] is 'credit_card', else 0).

  In orders
    - rows are grouped by [order id (stg_payments)], one row for each distinct value
    - rows are joined where [order id (stg_orders)] and [order id (stg_payments)] both have values and are equal, keeping every row of the left side, with no values from the right side where it has no match

  grain  one row per order: orders.order_id is its key, by the unique test on orders.order_id

Names (4)
  [amount]                   raw_payments.amount
  [order id (stg_orders)]    stg_orders.order_id
  [order id (stg_payments)]  stg_payments.order_id
  [payment method]           raw_payments.payment_method
```

The sentence is worded from the composed formula by a fixed grammar, not written by a language model, so it says what the SQL says and nothing more:

- Each kind of expression has one wording: `a * b` is *a times b*, `SUM(x)` *the sum of x*, `COUNT(DISTINCT x)` *the number of distinct values of x*, `COALESCE(a, 0)` *a, or 0 when a has no value*, and a `CASE` *r1 if c1, else r2 if c2, else r3*, whose first branch that holds wins.
- `NULL` is *no value* throughout: `x IS NULL` is *x has no value*. A test holds only where it is true, as in `WHERE` and `CASE`, so a negated test says what it needs: `x <> 'a'` is *x has a value other than 'a'* and `x NOT IN (1, 2)` *x has a value that is not one of (1, 2)*, which a row with no value does not meet. Two columns compared are worded the same way: `a = b` is *a and b both have values and are equal*. A column that is itself a test reads *is whether ...*, and where a value it tests can have none, the sentence adds that it can then have no value rather than be true or false.
- Division says how the dialect divides: in Postgres and T-SQL *rounded towards zero when both are whole numbers*, and where division by 0 gives no value (MySQL, BigQuery's `SAFE_DIVIDE`), *with no value where b is 0*. An ordered window or `ORDER BY` says where rows with no value go (*no value last*), which dialects decide differently when the SQL does not say.
- Columns are named in square brackets by their words (`customer_id` is *[customer id]*), with their relation where two columns would read alike, and always with the reading where a query reads a relation more than once (*[amount (the second reading of orders)]*), so no column name reads as part of the grammar. A column of a `UNION` reads by its branches: *[amount (a)] or, in the other branch, [amount (b)]*. *Names* maps each wording back to its column.
- Brackets keep the SQL's grouping wherever it matters: an operand that is itself an operation is bracketed unless it continues a chain of plus and minus, of times, or of followed by, and a phrase such as *the sum of x* is bracketed as an operand of arithmetic (*(the sum of x) plus 1*), so two formulas that compute different things never read alike.
- A window function names the rows it reads, their order and its frame, including SQL's default frame for an ordered window (from the first row to this row and the rows tied with it).
- The joins, filters and grouping of every relation on the way are worded the same way, nearest first. The grain comes from a bound term's `grain`, or from the key of the entity the relation holds (a unique test, a primary key). A bound term's required filters are worded too, and an approved measure's name leads the sentence.
- A part the grammar has no wording for, such as a subquery, a function it does not know, a `NOT` of anything but a comparison or a list that holds `NULL`, is quoted as SQL between backticks and listed under *Quoted as SQL*, and `verbatim` lists it in the JSON.

`konstruct explain COLUMN --english --json` adds the result under `english`; `Project.english` and the `explain` MCP tool's `english` argument give the same.
