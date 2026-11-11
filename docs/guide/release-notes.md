# Release Notes

`konstruct release-notes` writes a change log of a data stack's formulas for the people who read its numbers: what changed in each formula and filter, in which commit, by whom, what it affects downstream, and whether the business term it holds was approved after the change.

```text
$ konstruct release-notes --since v1
Release notes since v1 (HEAD)
2 changes in 2 of 2 commits since v1.

  2026-11-03  53bd699  Sam  Leave gift cards out of lifetime value
    customers.customer_lifetime_value: formula changed
      was  SUM(stg_payments.amount)
      now  SUM(CASE WHEN stg_payments.payment_method <> 'gift_card' THEN stg_payments.amount ELSE 0 END)
      - it was the sum of [amount]; it is now the sum of ([amount] if [payment method] has a value other than 'gift_card', else 0)
      - affects 1 column, 1 relation downstream
      - 'Customer lifetime value' (bound to it) is approved, but not since this change
```

For every commit in the range, each dbt model and SQL file it changed is compared with its parent the way [`konstruct diff`](reviewing-changes.md) compares a branch, by canonical expressions, so a commit that only reformats SQL or renames a CTE is not listed. Each entry is one of:

- **A formula** added, removed or changed, with the column's own expression before and after, as SQL and in plain English (see [In Plain English](explain.md#in-plain-english)).
- **Rows**: a relation whose joins, filters or grouping changed, with each condition removed and added, in SQL's place worded in English.
- **A term**: a change to a business term's status, approval date, owner or definition in `definitions.yml`.

Each formula and row change lists what is downstream of it (columns, relations, and dbt exposures with their owners) and, for each business term bound to it or downstream of it, the first commit at or after the change that approved the term, with the commit's author and date, or that none has. A term from an ontology pack is not listed, as it has no approval of its own.

A file a commit moves is compared with itself at its old path, as git pairs moves; a SQL file deleted and another added in one commit that build the same tables are taken as one file moved too. In a shallow clone, the oldest commit read cannot be compared with its parent, so it is left out with a note saying to fetch more history. Where the SQL has templating Konstruct cannot render, it shows `'__konstruct__'`, and a note says so.

`--since` takes a commit, a tag or a date (`2026-10-01`); by default the notes start after the latest tag before `--until`, so the notes up to a release's tag cover that release, or cover the whole history when there is none. `--until` ends the range (default HEAD), `-n/--limit` caps the commits read (200 by default), and `--format markdown` gives the notes as Markdown to post to a wiki, a chat channel or a pull request; `--json` and `--format json` print JSON. dbt refs and macros are rendered as they are now for every version, as `konstruct history` does. Uncommitted changes are not listed.

## The Effect on Today's Data

A number moves when its definition changes or when its data does. `konstruct verify --effect --since v1` separates the first: for each formula and row change it runs the changed model's SQL as it was before the commit and as it was after on the same data, the warehouse as it is now, and adds the difference to the release notes:

```text
      - on today's data: 100 rows in both; the sum of customer_lifetime_value falls from 1,672 to 1,467 (-12.3%); 10 of 100 rows with the same customer_id change customer_lifetime_value
```

It runs in `verify`'s engines and sandbox (see [How Much to Trust the Lineage](verify.md)): each version must be a single query that only reads, the tables either version reads are copied, up to `--rows` rows each (a million by default), and nothing is written to the warehouse. Where a copy reaches that limit, the result says it may be on a sample. For each change it gives the rows each version returns, the sum of the changed column in each when its values are numbers, and where the relation has a key (a unique test or a primary key), how many rows of the same key changed value and how many keys appeared or disappeared; with no key, how many of the column's values only one version gives. A change to a relation's joins, filters or grouping changes no formula but can change every column, so every column both versions return is compared and those that moved are named; with no key to match rows by, the rows themselves are compared too, so values that move between rows show even where every sum stays the same. `-m/--model` limits it to some models and `--warehouse` picks the warehouse, as for `verify`. A version is run only as it would be run: one whose dbt Jinja renders only with the dbt artefacts, whose SQL file has Jinja blocks, or whose SQL still holds templating Konstruct could not render is not run. A change whose relation is no longer in the graph, or whose SQL is not a single query that reads, says why it has no effect too. With no formula or row change in the range, no warehouse is opened.

`Project.release_notes` and `Project.effect` give the same in Python, and the `release_notes` and `definition_effect` MCP tools give them to an agent.
