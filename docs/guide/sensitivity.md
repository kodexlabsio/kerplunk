# Sensitive Data and Erasure

Konstruct follows sensitive data, such as personal data, through the column lineage: from the columns marked sensitive to every column, model and exposure computed from them, stopping only where a transform removes the sensitivity. `konstruct sensitivity` shows where the data goes, `konstruct gate` fails a change that moves it into a model or exposure not marked to hold it, and `konstruct erasure` lists every relation that holds one person's rows.

## Marking Sensitive Columns

Konstruct reuses the classification a project already has. A column is marked sensitive by:

- **A business term** with a `sensitivity` in `definitions.yml`. Each column the term binds is marked; a relation the term binds is marked to hold such data. The value is a word for the kind of data, such as `personal`:

  ```yaml
  - name: Customer name
    definition: A customer's first and last name, which identify a person.
    sensitivity: personal
    columns: [raw_customers.first_name, raw_customers.last_name]
  ```

- **An ontology pack.** A pack's term can carry a `sensitivity` too, and marks the columns it binds in the schema it matches, as the pack's other terms do (see [Ontology Packs](ontology-packs.md)).
- **The warehouse.** A tag that classifies data marks the column it is on, and every column of the table it is on: `PII`, `PHI`, `PCI`, `SENSITIVE`, `CONFIDENTIAL`, `GDPR`, `PERSONAL_DATA`, Snowflake's `PRIVACY_CATEGORY` and `SEMANTIC_CATEGORY`, a `SENSITIVITY` or `CLASSIFICATION` tag (its value names the kind of data), Databricks' `class.*` tags, and any tag `[sensitivity] tags` names. A tag whose value is `none`, `public` or `insensitive` marks nothing. A masking policy marks the column too. Konstruct reads Snowflake's object tags and masking policies from the account usage views, which need the SNOWFLAKE database's imported privileges and lag the account by up to two hours; a tag a column inherits from its schema or database is not read. It reads Databricks' column tags and column masks, and BigQuery's policy tags. Governance a role cannot read is a build warning, and marks no column.

## How Sensitivity Flows

A column computed from a sensitive column is sensitive too, with the same kind of data, whether it copies the value or transforms it: through a rename, `UPPER`, `CONCAT`, `LEFT`, a `CASE` branch or condition, `COALESCE`, either side of a `UNION`, a window function, a CTE, a join that brings the column in, or a count. Every field of a sensitive struct or map is sensitive (`profile.email`, `profile['email']`), and so is a struct with a sensitive field.

A hash does not stop it. `MD5`, `SHA1`, `SHA2`, `SHA256`, `HASH`, `FARM_FINGERPRINT`, `STANDARD_HASH`, `XXHASH64` and the like are undone by hashing a guess, which finds a PIN, the first letters of an address or a known email address at once, so a column whose every read is hashed stays sensitive, shown as `pseudonymised`.

Sensitivity stops only where every read of the column in the expression is inside one of:

| Transform | When |
|-----------|------|
| A function `[sensitivity] sanitisers` names | always: a tokenisation or masking function you trust |
| `SUM`, `AVG`, `COUNT`, `STDDEV`, `VARIANCE` and their kin | when the SQL requires every group to hold at least *k* distinct values of a key (`[sensitivity] k`, 10 by default): `HAVING COUNT(DISTINCT customer_id) >= 10` |

A count of rows proves nothing: one person can have any number of rows, so `HAVING COUNT(*) >= 10` keeps the column sensitive. Neither does a count of the input's own row or primary key (`COUNT(DISTINCT payment_id)` over payments), which is unique per row. The key counted must be a foreign key to the key of an entity whose own relation has a column marked sensitive, such as users with a name rather than orders, as a dbt `relationships` test, an ontology pack's or an Ossie model's relationship, or a join to the entity's key says. In the gate, a test, a join or an entity term in a file the change touches is no such evidence, so a change cannot make its own key count people. The proof holds only where the statement reads one relation, with no join (a comma between tables is a join), no `WHERE`, at most one `GROUP BY` and that on no identifier, unique or sensitive column and with no `ROLLUP`, `CUBE` or `GROUPING SETS`, and a `HAVING` that is that count and nothing else; and where the aggregate is the expression's only one, is not a window function, has no `FILTER`, and reads the sensitive column alone, with no `CASE`, `IF` or comparison inside it. Anything less leaves room for a group, or a difference of two aggregates, that is one person's value.

Anything else keeps the column sensitive: `MIN`, `MAX`, `MEDIAN`, `ANY_VALUE`, `ARRAY_AGG` and `STRING_AGG` (they return or collect individual values), a conditional count (`COUNT(CASE ...)`, `COUNT_IF`), `md5(email) || email`, and an expression Konstruct cannot read. A column that only joins, filters or groups a relation's rows does not pass its values on: it is listed, and the relation's columns stay as they are. A relation fed by one with sensitive columns through SQL whose columns Konstruct does not know (a `SELECT *` over a table of unknown columns, say) is listed as one that may hold the data.

```text
$ konstruct sensitivity
Sensitive data in the graph
7 sensitive columns in 4 relations (2 marked, 5 through lineage); 5 in built relations not marked to hold the data; 1 exposure showing it, 1 not marked to.

Columns (7)
  customer_ltv.first_name   personal  direct from raw_customers.first_name  not marked to hold it
  customers.first_name      personal  direct from raw_customers.first_name  not marked to hold it
  ...
```

`konstruct sensitivity <table or column>` reports on one table or column, with the path from the marked column for one column, and the reads of it a sanitiser stops. `--strict` exits with status 1 when sensitive data reaches a built model or exposure not marked to hold it, or a table copies a masked column in clear.

## Marking What May Hold It

A built relation or an exposure that receives sensitive data should be marked to hold it, in one of three ways: listed in `[sensitivity] holders` in `konstruct.toml` (by name, label, warehouse name or id, each a glob pattern), bound to a sensitive term in `definitions.yml`, or, for one column, marked itself.

```toml
[sensitivity]
holders = ["stg_customers", "customers", "crm_*"]
sanitisers = ["tokenise"]
tags = ["restricted"]
k = 10
```

## The Gate

`konstruct gate` (and the GitHub Action with `gate: true`) fails a change that moves sensitive data where it is not marked to go, with three rules:

- `sensitive-flow`: a column the change adds or alters, or an exposure it adds or one that reads such a column, receives sensitive data, and its model or exposure is not marked to hold it; or a relation the change alters, or one a SQL file it changes builds, reads sensitive data through SQL whose columns Konstruct does not know, such as a statement whose column lineage fails. The data counts as sensitive when the relation it reads holds it at the base, even if it no longer does after the change.
- `unmasked-copy`: a table or materialised view the change alters copies, unsanitised, a column the warehouse masks, and has no masking policy of its own. A view is not a copy: the warehouse masks what it reads as it reads it.
- `sensitivity-removed`: the change takes a sensitivity mark away: a sensitive term removed from `definitions.yml`, its `sensitivity` removed or changed, a column taken out of it, or an ontology pack whose terms mark columns sensitive turned off in `[ontologies] packs`; or a column a sensitive term binds at the base and no longer binds, as when the model that creates it no longer has column lineage.

Only what the change alters is judged, so data that already flows does not fail the gate. Nothing in the change can mark what it alters to hold the data:

- The settings are the strictest of the base's and the working tree's: a holder or a sanitiser counts only when the base has it too. A base whose settings cannot be read, because it has no configuration, an invalid one, or is a commit a shallow clone lacks, counts as one with none.
- A column the change adds or alters is not held because of a mark on it, whether from `definitions.yml`, an ontology pack or the warehouse, and a term the change adds or rebinds marks no relation to hold the data.

When a term binds nothing or a relation the change touches reads another with no column lineage, the gate also builds the base from the files of its commit, whatever `.gitattributes` says, to compare the marks there with those after the change, and keeps that graph in the user's cache directory (`$XDG_CACHE_HOME/konstruct/base`, otherwise `~/.cache/konstruct/base`, `~/Library/Caches/konstruct/base` or `%LOCALAPPDATA%\konstruct\base`) for the next gate on the same commit. Nothing in the repository, and no setting in its configuration, decides where the gate reads that graph from, and it reads it only from a file and a directory that are the user's own, are not links and that no one else can write. The key covers the repository, the commit, the project's directory, and the versions of Konstruct, sqlglot and every installed plugin. In CI (`CI` or `GITHUB_ACTIONS` set) the gate keeps no graph: a fresh runner gains nothing from one, and a restored cache could hold one a pull request wrote. For the same reason, run the gate in CI before any step that runs the pull request's own code, such as its tests or its build scripts, so that nothing the change contains has run on the runner when the gate reads it. If the base cannot be built, each of these counts as a finding, and so does a term the change leaves alone that the base does not have.

So the gate fails such a change until its holder is marked on the base, in a change of its own, reviewed by the data's owner. A build from files alone, as the gate from the MCP server and the coding agent hook make, keeps the tags the last build read from the warehouse, and the masking policies on tables nothing in the project builds: rebuilding a table drops a policy set on its column, so a built table's policy is read from the warehouse again before it counts.

## The Erasure Map

`konstruct erasure <key>` answers where one person's data ends up. The key is a column (`customers.customer_id`), an entity of the [ontology](ontology.md) (`customer`), or a table, for the key of the entity it holds. Konstruct follows the key:

- up and down the column lineage, through copies, transformed copies (a cast or a trim) and hashed copies; a count of keys holds none;
- across every key column of its entity;
- through the foreign keys that refer to it, and the columns a join matches to it: a dbt `relationships` test, a join, an ontology pack's or an Ossie model's relationship;
- to the person's personal data copied on without the key, such as a list of email addresses;
- to the rows the person owns: the key of a relation whose rows refer to the person (an order's `order_id`) is followed in turn, for `--levels` steps (3 by default), so the payments of the person's orders are found too.

```text
$ konstruct erasure customer
Erasure map for customers.customer_id (customer)
9 relations hold the rows of a customer (3 delete, 3 none, 3 rebuild); 1 exposure shows it.

  relation       action   key                                        personal data
  customer_ltv   rebuild  customer_id (copy)                         first_name
  customers      rebuild  customer_id (key)                          first_name, last_name
  orders         rebuild  customer_id (foreign key); order_id (key)
  raw_customers  delete   id (copy)                                  first_name, last_name
  ...
```

Each relation's action is `delete` for a source, a seed or a table nothing in the graph builds, and for a snapshot, which keeps rows its source no longer has; `full refresh` for an incremental model; `rebuild` for another built table or materialised view; and `none` for a view, which holds no rows of its own. Relations that share a short name are shown by their warehouse names.

## Values Konstruct Shows

Only `konstruct verify` reads the warehouse's rows. `verify` itself reports which columns change, never a value. `verify --why` shows the source rows behind a value, and shows as `<masked: personal>` (the kinds of data) the value of every sensitive column in each source row, every row's share when any column the value is computed from is sensitive, even where the value is not (an aggregate over large groups), and the value itself when the column asked about is sensitive; the result's `masked` names what was masked. `verify --mutate` suggests no test that would quote a sensitive column's values: no `accepted_values`, `relationships` or range test on one, and no unit test built from the rows of a model that reads one.
