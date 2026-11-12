# Timeline

`konstruct timeline` shows how the model of the business changed, one commit at a time. For each commit it builds the world (the entities, their properties and relationships, and the metrics) as the commit's files hold it, and compares it with the world at the commit before. Each entry lists what was added, removed, renamed or changed, in plain English, with the commit's author, co-authors and date.

```text
$ konstruct timeline --since v1
Timeline since v1 (HEAD)
4 changes to the business in 3 of 3 commits since v1.

  2026-02-01  c23fc4e  Sam  Count returned orders in revenue
    metric 'Revenue': filters changed
      - answers no longer have to keep only rows where [status] has a value other than 'returned'

  2026-02-20  5749f3b  Ada  Test that orders belong to customers
    relationship 'order -> customer (orders.customer_id = customers.customer_id)': added
      - each order now refers to a customer through orders.customer_id = customers.customer_id, many-to-one, trusted as declared
    property 'order.customer_id': refers_to changed
      - it now refers to customer

  2026-03-05  da5c347  Priya  Approve customer lifetime value
    metric 'Customer lifetime value': approved, status changed
      - approved on 2026-03-05
      - its status changed from draft to approved
```

## What Counts as a Change

The timeline compares what the build makes of each commit, not the text of its files, so a commit that only reformats SQL, renames a CTE, moves `definitions.yml` or puts a model that only passes columns on between two others has no entry. It reports:

- **Entities**: added, removed or renamed (the same keys under another name), and changes to their keys, the relations that hold them, the business term that names them with its status, owner and approval date, and how far they are trusted.
- **Relationships**: one entity referring to another through a pair of columns, added or removed, and changes to its cardinality and trust.
- **Properties**: the columns that describe an entity, added, removed or renamed (the same formula under another name), and changes to their formula composed down to source columns, their description, the terms bound to them and the entity they refer to.
- **Metrics**: every metric, those Konstruct proposes from the code included, added, removed or renamed (the same columns, formula and definition under another name), and changes to the formula composed down to source columns, the joins, filters and grouping it is computed over, its required filters, its grain, unit and aggregation, its status, owner and approval date, and how far it is trusted.

Trust is a fact's confidence (`verified`, `declared` or `inferred`) and what `konstruct verify` found of it, with the evidence. Removing a uniqueness test, for instance, lowers the trust of the entity whose key it tested, and can remove the relationships that relied on it.

Commits are taken in the order of history, so a commit rebased onto a later one still comes after it, and its date is the date its author wrote it. Merges are left out: what a merge brings is the commits it merges, which are read themselves.

## Release Notes

Each metric change also gives a release note: what changed, in which commit and by whom, and whether the metric was approved since and by whom. An approval counts only when the approving commit contains the change; one made on a branch that does not contain it approved something else.

```text
Revenue: the required filter that keeps only rows where [status] has a value other than 'returned' was removed in c23fc4e (Sam); agreed, not approved since
Customer lifetime value: renamed from 'Lifetime value' in fe1f53f (Sam); approved in a commit by Priya on 2026-03-05 (da5c347)
```

`konstruct verify --effect --timeline` adds what each change to a metric's formula, or to the rows it is computed over, does on today's data. It runs the change in the same commit to one of the metric's columns or relations, or to one upstream of them, as it was before the commit and as it was after, on the same data, as [`verify --effect`](release-notes.md#the-effect-on-todays-data) does: read-only, in `verify`'s engines, and nothing is written to the warehouse. The release note ends with the difference:

```text
Lifetime value: the formula changed in b2034a5 (Linus); it was the sum of [amount]; it is now the sum of ([amount] times 2); approved in a commit by Grace on 2026-03-05 (0c203a7); on today's data: 20 rows in both; the sum of total rises from 8,729 to 17,458 (+100.0%); 20 values of total only before and 20 only after
```

## Options

`--since` takes a commit, a tag or a date (`2026-10-01`), and the timeline starts after it; by default it reads the whole history. `--until` ends the range (default HEAD). `--file PATH` reads only the commits that changed that file or directory, relative to the project, and can be given more than once; each commit is still compared with the whole world before it. `-n/--limit` caps the commits read, the most recent (50 by default), and a note says when there were more. `--format markdown` gives the timeline as Markdown to post to a wiki, a chat channel or a pull request; `--json` and `--format json` print JSON. With `--effect --timeline`, `verify` takes `--since`, `--until`, `--format`, `--warehouse` and `--rows` as `--effect` does.

`Project.timeline` and `Project.timeline_effect` give the same in Python, and the `timeline` MCP tool gives the timeline to an agent.

## How Each Commit Is Built

Each commit's world is the ordinary build of that commit's own files, read from git's objects without checking anything out, so a `.gitattributes` the commit sets cannot hide or change a file. Nothing runs dbt, connects to a warehouse or runs a plugin, and the project's `konstruct.toml` at that commit is read as an untrusted one is. dbt's artefacts (`target/manifest.json` and `catalog.json`) are not in git, so build the project first: each commit's manifest is made from the manifest as it is now.

- A model's SQL is its file at the commit, rendered with the commit's own macros and with the values its `dbt_project.yml` gives `var()`, so a change to a project macro or a var is a change to every model that uses it. Where the commit's `dbt_project.yml`, package files, YAML and macros are all today's, a model whose file, and the file of every model upstream of it, is today's keeps the SQL dbt compiled. The macros of installed packages (`dbt_packages/`) are not in git, so a model rendered from a commit's files goes without them, and a note names those models. A package's models keep the SQL dbt compiled today, and a note names those that read an ephemeral model the commit changed.
- A source's table, schema and database come from the commit's YAML.
- A model, seed, snapshot or test whose file is gone is left out, and a new model or seed file is a new node, with the dependencies its refs and sources name. Tests, documented columns, descriptions, constraints, configuration and sources come from that commit's YAML. A snapshot defined in YAML keeps today's SQL. A seed's columns are its CSV header at the commit. A model the commit holds in another folder than today keeps the configuration and relation dbt gives it today, not what `dbt_project.yml` configures for the commit's folder, and a note names it.
- A model whose file at the commit differs from today's takes its columns from its SQL at that commit rather than from dbt's catalog, and a note says so; the others keep the catalog's columns and types, which are today's. A property's type is not compared, for that reason.
- dbt's semantic layer metrics and exposures are read as they are now.
- A dbt project whose `dbt_project.yml` at the commit names a target or model directory outside the project is not read, and a note says so.
- The other repositories `konstruct.toml` lists are not read at each commit.

What `konstruct verify` found of keys and relationships in today's data holds at every commit, and a note says so; a change of trust between commits comes from the evidence a commit changed. Where a commit's world cannot be built, it is listed, and the next commit is compared with the last commit before it whose world could be.

Each commit's world is kept in your user cache directory (`$XDG_CACHE_HOME/konstruct/timeline`, otherwise `~/.cache`, `~/Library/Caches` or `%LOCALAPPDATA%`), by repository, commit, the project's directory, the versions of Konstruct, sqlglot and the installed plugins, and the dbt manifest and catalog each world is made from (apart from when dbt wrote them and the catalog's row counts, sizes and modification times, so a dbt run or a load of data that changes nothing else keeps them), so a second run builds only new commits, with the analyses of SQL it made before. The cache is never in the repository; one that is a link or that someone else can write is not used, nor is one for a work tree whose `.git` file no git directory names back (as `git init --separate-git-dir` leaves it), and in CI (`CI` or `GITHUB_ACTIONS` set) there is no cache.
