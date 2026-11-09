# Business Definitions

Lineage says where a number comes from; it does not say what the business means by it. `definitions.yml` records that in the repository, next to the models, reviewed in pull requests like code:

```yaml
version: 1
terms:
  - name: Customer lifetime value
    kind: measure                 # entity, measure or term; inferred when left out
    synonyms: [lifetime value, LTV, CLV]
    definition: Total value of every payment a customer has made, in Australian dollars.
    owner: {name: Priya Shah, role: Head of Finance}
    status: approved              # draft, agreed, approved or retired
    approved: 2026-10-30
    grain: one row per customer
    columns: [customers.customer_lifetime_value, customer_ltv.lifetime_value]
    filters: ["orders.status <> 'returned'"]   # filters any answer must apply
    unit: currency
    currency: AUD
    related: [Order amount]
    exceptions: Refunds are not netted off.
```

The build attaches each term to the columns it names, so the definition travels with the lineage:

```text
$ konstruct define lifetime value
Customer lifetime value  [approved, Priya Shah (Head of Finance), approved 2026-10-30]
  Total value of every payment a customer has made, across all orders and payment methods, in Australian dollars.
  also called: lifetime value, LTV, CLV
  grain: one row per customer
  unit: currency AUD
  exceptions: Refunds are not netted off; returned orders still count.
  related: Order amount, Customer
  defined in: definitions.yml:5
  columns:
    customer_ltv.lifetime_value  DOUBLE
    customers.customer_lifetime_value  DOUBLE
```

- **Agents get the agreed answer.** The `define` MCP tool returns the approved definition, owner, required filters and columns for a term or a column, and `query` puts definitions first, so an agent asked "what is our lifetime value?" starts from the wording the business approved.
- **Changes show what they touch.** `impact` lists the business terms a change reaches, so changing `stg_payments.amount` names *Customer lifetime value* and *Order amount*. `describe` shows the terms that define a column. graph.html searches terms by name and synonym and highlights their columns.
- **Definitions cannot drift silently.** The build warns, and fails under `--strict`, when a bound column or a column in a required filter no longer exists (naming the likely rename), when an approved term has no owner or approval date, when an agreed or approved term is bound to nothing, when two terms share a name or synonym, and when a related term is not defined. `REPORT.md` lists every term with its status, owner and columns.
- **Pull requests review them.** `konstruct diff` reports terms added, removed and changed field by field, and flags an approved term whose definition, grain, columns or filters changed while it stayed approved with the same date.

- **They feed the ontology.** A term of kind `entity` names a business object, and a term of kind `measure` (or one bound to a computed column) is a measure, with each column's formula and verification in `ontology.yml`. The build warns when a measure's columns compute it differently. [The Ontology](ontology.md)

Konstruct reads every `definitions.yml` (or `.yaml`) in the repository; `[definitions] paths` in `konstruct.toml` pins the files. The example project has five terms in [`examples/jaffle_shop/definitions.yml`](https://github.com/kodexlabsio/konstruct/tree/main/examples/jaffle_shop/definitions.yml).
