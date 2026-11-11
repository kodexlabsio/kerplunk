# The Ontology

Konstruct derives an ontology of the data stack from its graph: the business objects (entities), what is measured about them (measures), how they relate, where each measure comes from and how far to trust it, and the rules the build holds it to. Most of it is read from the code and the tests rather than declared; `definitions.yml` names what code cannot say. Every build writes it to `konstruct-out/ontology.yml`, and agents read it through the `ontology`, `entity`, `metric` and `relate` MCP tools.

```text
$ konstruct ontology
Ontology: 3 entities, 2 measures, 2 relationships and no rule violations.

Entities (3)
  customer  key customer_id  customers, stg_customers  declared  term 'Customer'
  order     key order_id     orders, stg_orders        declared
  payment   key payment_id   stg_payments              declared

Measures (2)
  Customer lifetime value  approved  verified  Priya Shah (Head of Finance), one row per customer
    customer_ltv.lifetime_value        = SUM(raw_payments.amount / 100)  verified 3/3
    customers.customer_lifetime_value  = SUM(raw_payments.amount / 100)  verified 2/2
  Order amount  approved  verified  Priya Shah (Head of Finance), one row per order
    orders.amount  = SUM(raw_payments.amount / 100)  verified 2/2

Relationships (2)
  order → customer  many-to-one  orders.customer_id = customers.customer_id   declared  tested
  payment → order   many-to-one  stg_payments.order_id = stg_orders.order_id  inferred
```

## What It Contains

| Part | Where it comes from |
|------|---------------------|
| **Entities** | A column named like an identifier (`customer_id`, `order_key`, `customer_uuid`, `CategoryId`, `id`, `uuid`) with a dbt `unique` test is the key of an entity named after it: `customer_id` is the key of *customer*, and a bare `id` or `uuid` takes the relation's name (`stg_customers.id` gives *customer*, `stg_stripe__payments.id` gives *payment*). A single-column primary key is a key whatever its name: a dbt `primary_key` constraint, a DuckDB or Postgres primary key, an Iceberg identifier field, or the primary key of a dataset in an [Apache Ossie model](ossie.md). A unique column such as `email` is not a key, and neither is one column of a composite key. The relations keyed by it hold one row per instance. A term of kind `entity` names an entity explicitly, and a measure's grain (`one row per store`, or `one row per store per day`) makes an identifier named for that entity in the measure's relations its key |
| **Measures** | Business terms with a formula: a term of kind `measure`, a metric from an [Apache Ossie model](ossie.md), or a term whose bound columns are computed rather than copied. Each bound column carries its formula composed down to source columns (as [`explain`](explain.md) gives it), the sources it reaches, how many lineage edges on the way [`verify`](verify.md) confirmed, and its usage score when [usage](usage.md) is on. The measure's grain (`one row per customer`) links it to its entity |
| **Relationships** | A dbt `relationships` test, a relationship in an [Apache Ossie model](ossie.md), or a join that matches one entity's column to another entity's key, says that an order belongs to a customer. Each relationship has the columns that join them (those a test names, if one does), whether a test vouches for it (`tested`), its cardinality and the tests and joins that show it. The cardinality is many-to-one, or one-to-one when both columns are unique; it is left out (`cardinality unknown`) when nothing shows the key is unique |
| **Provenance and trust** | For each measure column: its sources, whether its formula reaches source, its verified edges and its usage; for each measure: its status, owner and approval date |
| **Concepts** | The hierarchy of business terms: each term's `broader` terms in `definitions.yml` or a SKOS or OWL [vocabulary](definitions.md#vocabularies), with the terms that are kinds of it, so *net revenue* is a kind of *revenue* and *revenue* a kind of *measure* |
| **Rules** | What the build holds the ontology to. A violation is a build warning, so `konstruct build --strict` fails on it |

## Confidence

Every entity, measure and relationship carries a `confidence`: the strongest its evidence supports, and never more. Its `evidence` lists what supports it.

| Confidence | Means | Entity | Relationship | Measure |
|------------|-------|--------|--------------|---------|
| `verified` | [`konstruct verify`](verify.md) checked it against the warehouse's data | Every one of its key columns is unique and never null | Every value of the referring column is found in the key and the key is unique; a one-to-one relationship whose referring column verify finds repeated is many-to-one | It is declared, its columns compute it one way, and every lineage edge from them to source is confirmed |
| `declared` | A person stated it | Every one of its keys has a `unique` test, a primary key, an identifier field, an Ossie primary key or a term in `definitions.yml` | A dbt `relationships` test or an Ossie relationship | A term in `definitions.yml`, a vocabulary or an Ossie model |
| `inferred` | Konstruct read it from the code alone | A key that only a grain or an ontology pack's binding shows | A join in SQL, or an ontology pack | An ontology pack's term, whatever its lineage |

An entity is only as trusted as its least trusted key: one key from a pack makes it inferred, and verify confirming some of its keys but not the others leaves it declared, with evidence naming the keys it did not check. Lineage confirms a measure's SQL, not a pack's binding by table names or an Ossie metric's own expression, so neither becomes verified.

A check that fails never raises a confidence: the entity or relationship keeps the confidence its other evidence gives, `check` is `failed`, and the text output shows `check failed` beside it. A key verify finds repeated also takes away the cardinality it gave: a relationship to it has its cardinality left out and its `check` failed, and one claimed one-to-one whose referring column repeats becomes many-to-one, which does not fail it.

## Entities, Metrics and Relationships

Three commands, and the MCP tools of the same names, work through the ontology one question at a time, so a person or an agent can reason about customers, orders and revenue rather than about tables. Every fact they give keeps its confidence and evidence.

`konstruct entity <name>` gives one entity: its key, the relations holding one row per instance, its confidence and evidence, its properties (the other columns of those relations, gathered by name across them, with their types, descriptions and business terms, and for a referring column the entity it refers to), the relationships it takes part in and the metrics whose grain it is.

`konstruct relate <from> <to>` gives how two entities connect. When one relation holds one row per instance of two entities (orders keyed by both `order_id` and `invoice_id`, each unique there by a test, a primary key or an identifier field, or as verify confirmed), each of its rows is one of both, so a step between them along that row is one-to-one, as trusted as the less trusted of those two key columns, joins nothing and counts no relationship, whether it is the whole answer or a step in the middle: a payment reaches its own order's invoice through the order's row, not through the customer's latest invoice. A key column with no uniqueness guarantee there, such as a referring column a definition also lists, gives no such step, and one verify found repeated gives a step whose check failed. It gives the path with the fewest relationships verify found the data contradicting, then the fewest relationships, then the most trusted, each walked with its cardinality in that direction, its columns, its confidence and its check; the cardinality of the whole path (`one-to-many` means one row of the first can match several of the second, so aggregate before joining); its confidence, the weakest of the relationships on the way, so a path through an inferred join is inferred however many verified ones it also crosses; its `check`, `failed` when verify contradicted a relationship on the way, which the text output says can lose or repeat rows; and the SQL joins that follow it, over the warehouse's relation names, with an alias each time a relation is joined (`orders`, then `orders_2` when the path comes back to it). Each join is on the columns of a relationship; where the next relationship starts from another relation holding the same entity, that relation is joined first on the entity's key in both, never on a column's name alone:

```text
$ konstruct relate customer payment
How customer → payment (2 relationships)

  cardinality  one-to-many
  confidence   inferred
  One customer can match several payment rows: aggregate them before joining.

Path (2)
  customer → order  one-to-many  orders.customer_id = customers.customer_id   declared
  order → payment   one-to-many  stg_payments.order_id = stg_orders.order_id  inferred

Join
  FROM jaffle_shop.main.customers AS customers
  JOIN jaffle_shop.main.orders AS orders ON orders.customer_id = customers.customer_id
  JOIN jaffle_shop.main.stg_orders AS stg_orders ON stg_orders.order_id = orders.order_id
  JOIN jaffle_shop.main.stg_payments AS stg_payments ON stg_payments.order_id = stg_orders.order_id
```

It exits with status 3 when either name is no entity or no relationship connects them; `konstruct path` then shows how their relations connect in the lineage.

`konstruct metric` lists every metric (`-n` for at most so many, with the number found), and `konstruct metric <name>` gives the metrics a name, a synonym or a column holding one finds, each with its definition, formula, aggregation and whether it adds up across groups (a sum or a count does; an average, a distinct count or a measure its definition makes non-additive does not; for a price or a balance it cannot be said), grain, required filters, unit, owner, status, source, columns with their verified edges, confidence and evidence. A metric's grain and entity are `declared` (`grain_confidence`) only when its own definition states the grain (a pack's grain, written for the source system's table, is inferred); otherwise Konstruct read them from the relation that holds it, and the text output says `inferred` beside the grain. Like `define`, it exits with status 3 when a name finds nothing, as `konstruct entity` does. Metrics come from:

| Source | Confidence |
|--------|------------|
| The ontology's measures: `definitions.yml`, a vocabulary, an Ossie model | `declared`, or `verified` as above |
| An ontology pack's measures | `inferred` |
| dbt's semantic layer: each metric, with the aggregation, expression and model of the measure a simple metric names, its filters and its measure input's, and whether the measure is non-additive. A measure no metric names is not listed | `declared` |
| The measures of Looker views and Power BI semantic models, with the warehouse columns each reads and, for Looker, its `filters` as required filters | `declared` |
| The measures a plugin's input gives an exposure, named for that input | `inferred` |
| Proposals, read from the code where nothing defines a metric | `inferred` |

A formula is written as it is defined (an Ossie expression, a DAX measure, a LookML type over its SQL) or composed from SQL; an aggregation SQL cannot state without more, such as a percentile, has no formula rather than a wrong one. A column a business term, a dbt metric or a BI measure already reads is not proposed again. A proposal is one of: a column whose SQL aggregates with `SUM`, `COUNT` or `AVG` (through a `COALESCE`, cast or rounding) in a relation with a grain, either one that holds an entity or one grouped by a single column named for an entity; a numeric column of an entity's relation named as a measure (`amount`, `price`, `quantity`, `total`, `fee` and the like) whose SQL holds one value per row, with no aggregate or window, and whose lineage reaches no relation held at another entity's grain, since a value read from an order onto each of its payments is repeated and double counts when summed; and the count of each entity's instances by its key. A minimum, a maximum, a median, a rate, a ratio, a share, a date or a flag is not proposed, and nor is a key or a column described only in YAML. A proposal is marked `proposed`, has no status and no owner, and is never approved: the text output says `proposed` where a defined metric shows its status. On the example built without its `definitions.yml`, Konstruct proposes the customer lifetime value, the order amount and its parts by payment method, the number of orders per customer, the payment amount, and the counts of customers, orders and payments, each `inferred` with its formula and grain.

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

## Ontology Packs

Konstruct ships 274 ontology packs, one for each source system teams commonly land in a warehouse: billing and payments (Stripe, Chargebee, Recurly, Zuora), commerce (Shopify, Etsy, ShipHero), CRM and sales (Salesforce, HubSpot, Pipedrive, Copper), support (Zendesk, Intercom, Gorgias, Gladly), engineering (Jira, GitHub, GitLab, Linear), marketing and advertising (Google Ads, Facebook Ads, LinkedIn Ads, Klaviyo, Marketo, Mailchimp), product analytics (Amplitude, Pendo, GA4), finance and HR (NetSuite, QuickBooks, Xero, Workday, BambooHR, Greenhouse) and hundreds more. Each binds whenever the graph has its schema, so the entities, keys, measures and relationships every user of a source shares come without a `definitions.yml`. [The pack list](ontology-packs.md) gives each pack and what it holds; between them they hold 1,860 entities, 2,704 measures and 2,093 relationships.

The packs are made from connector schemas published under permissive licences: Fivetran's dbt packages, Singer and Meltano taps and dlt's verified sources. A pack knows its source's tables as the schemas it is made from name them (Fivetran's `ticket` and a Singer tap's `tickets`, say):

- each table keyed on `id`, `uuid` or `gid`, or on a column named after it (`ticket_id`), is an entity, keyed on that column, with the source's description of the table where there is one. A table is not an entity when it holds versions, snapshots, reports or statistics (Google Ads' `campaign_history`, `ad_group_stats`), when it has a row per day or per period (a `day`, `date` or `date_day` column beside a bare `id`, or a start and end time), when its source describes it as daily or as one row per one thing per another, when the source keys it on more than one column (NetSuite's `transactionline`, unique only within its transaction), or when its key repeats in the source's sample rows;
- each numeric column named as an amount, count, total, cost, duration or the like is a measure, with the source's description. A column described as a flag or as deprecated is not, nor is a setting such as a limit, a timeout or a `min_`, `max_` or `default_` value, nor any column of a history or snapshot table, which repeats each object's figures once per version;
- a `<name>_id` column that names another of the source's tables is a relationship to that table's entity.

Five packs (`stripe`, `shopify`, `salesforce`, `hubspot`, `ga4`) also hold hand-written terms, such as Stripe's charge amount in the currency's smallest unit; the Stripe and Shopify packs mark a customer's name, email address and phone number as personal data (see [Sensitive Data and Erasure](sensitivity.md)), and the GA4 pack reads the BigQuery export's newest `events_YYYYMMDD` table, not the intraday or fresh one.

A pack's match blocks name the tables a typical sync of its source lands, each with at least two columns that are not generic (not `id`, `name`, `email`, `status`, `created_at` and the like), in Fivetran's or a Singer tap's naming; Airbyte names most streams as the source's API does, as the taps do, so a schema Airbyte lands often matches the same blocks. A schema matches a pack when it holds one of the blocks and something shows it comes from that source: its name contains the source's (`stripe`, `fivetran_stripe`, a Singer target's `tap_stripe`, `analytics_123456` for GA4), or a loader landed its tables (any of those in [Loaders](inputs.md#loaders) except Census and Hightouch). A block of generic table names alone (`users`, `orders`, `tickets`) with fewer than three columns that are not generic needs the schema's name, so an application's own tables, even replicated by Fivetran, match nothing, while Shopify's `orders` and `customers`, with their `financial_status` and `verified_email`, match wherever Airbyte lands them. A schema named for a source (`tap_jira`, `hubspot_dataset`) also matches its pack when it holds two of the pack's entity tables with their keys, as any loader names them.

Every pack that matches a schema binds, so one schema that a loader fills from several sources (an Airbyte destination holding Stripe, Zendesk and HubSpot streams) gets each source's pack. Packs whose blocks hold in the same tables contend for them, and the best binds: the one the schema's name points to, by the most specific of the packs' patterns (`*salesforce_history*` before `*salesforce*`), then the one whose block requires more tables, then more columns, then the one whose name sorts first. A table that a pack names but its block does not require is left to the pack that requires it, and one that several bound packs name goes to the pack that names it exactly, or to none. The pack's terms are bound to that schema as drafts named after the source (`Zendesk Support ticket`), leaving out any whose table or column the schema does not have, and leaving entities and measures off tables that Fivetran's history mode lands, with a row for each version (those with `_fivetran_active` or `_fivetran_start`); and a pack matched in several schemas gets the shortest part of each schema's name that tells them apart (`Stripe customer (prod.stripe)`). Packs bind once, after every repository is merged, so repositories that share a warehouse share its pack terms. The project's own model of a source wins: a pack term is left out when a term in `definitions.yml` has its name or a synonym of it, or is bound to one of its columns. A pack's keys name entities but, untested, give no relationship cardinality. `[ontologies] packs` in `konstruct.toml` chooses the packs, and `packs = []` turns them off. To add or improve a pack, see Contributing.

## Using It

| Interface | Use |
|-----------|-----|
| `konstruct ontology` | The summary above. `--format yaml` for the file's contents, `--json` for structured output, `--strict` to exit 1 when a rule is broken |
| `ontology.yml` | Written by every build beside `graph.json`, and refreshed by `konstruct verify`. Its order is fixed, so it diffs cleanly in version control |
| `konstruct entity`, `metric` and `relate` | One entity, the metrics, and how two entities connect, as above; `--json` for structured output |
| MCP `entity`, `metric` and `relate` | The same, for an agent to learn an entity, take a metric's approved definition or see a proposal for what it is, and join two entities along their relationships |
| MCP `ontology` | The whole ontology, or with `name` one entity (by its name or its term's) or measure and the relationships that touch it, for an agent to learn the shape of the business before it writes a query that joins or aggregates. Case, spaces and underscores are not told apart, and a plural finds the singular |
| `impact` and `trace` | Each lists the business entities whose relations the walk reaches, with those relations, so a change to `stg_payments.amount` says it touches customers, orders and payments. A relation that holds one row per instance of an entity records it in its `entities` attribute in `graph.json` (a relation can hold the keys of more than one), which `describe` shows. A bridge table with a composite key names no entity |
| Python | `Project.ontology()` returns an `Ontology` with `entities`, `measures`, `relationships`, `rules` and `violations`; `entity(name)` and `measure(name)` match names as the MCP tool does. `Project.entity(name)`, `Project.metric(name)` and `Project.relate(start, end)` give an `EntityDetail`, `Metrics` and a `Connection` |
