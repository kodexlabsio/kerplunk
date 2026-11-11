# Answer Receipts

An answer about the data rests on columns, their formulas and the business terms that say what they mean. `konstruct receipts issue` records that basis when the answer is given, and `konstruct receipts check` says later, after any number of changes and builds, whether it still holds.

```bash
konstruct receipts issue customer_ltv.lifetime_value --question "What is customer 3's lifetime value?" --answer "120"
konstruct receipts check
```

## What a Receipt Records

`konstruct receipts issue` takes what the answer rests on: columns, relations (each of their columns), exposures (the columns they read) and business terms (the columns they define, and the term itself). For each column it records:

- **The formula**, composed down to source columns as `konstruct explain` composes it, in a canonical form, so that reformatting, renamed CTEs and passthrough models do not count as changes.
- **The rows**: the joins, filters and grouping of every relation on the way, composed the same way.
- **The sources** the formula reaches, and the column's type.
- **The business terms** bound to the column, from `definitions.yml`, vocabularies and ontology packs, with their status, owner, approval date and a version of their meaning.
- **Verification**: whether `konstruct verify` confirmed the column's lineage, partly confirmed it, ran without exercising it, or was never run.

It records each business term the answer names, and each term bound to the relation of a column it rests on, in the same way; the version of a measure defined by a formula, such as an Ossie metric, includes the formula.

It also records the build it answered from (the SHA-256 of `graph.json`, when it was built and by which release), the commit checked out, whether tracked files had uncommitted changes, the inputs changed since the build, and the question and answer when they are given. It prints a citation to put under the answer. On the example project:

```text
Relied on customer_ltv.lifetime_value = SUM(raw_payments.amount / 100); term 'Customer lifetime value' (approved 2026-10-30, owner Priya Shah). Lineage not checked against the data. Receipt r-7f3a9c1d2e4b.
```

A receipt says only what Konstruct recorded. A formula is derived from the SQL, not proven by running it; the lineage is called confirmed only where `konstruct verify` confirmed it; a formula with a gap (a Python model, SQL that could not be read) is marked incomplete; and a graph older than its inputs is named in the receipt.

## Checking Receipts

`konstruct receipts check` compares every receipt (or those it names, by id or the start of one) with the current graph and gives each a status. When inputs changed since the last build, it brings the graph up to date first, as `konstruct build -u` does; with `--no-refresh` it does not, and a receipt it cannot find changed in the older graph is `unchecked` rather than holding.

| Status | Meaning |
|--------|---------|
| `holds` | Everything it rests on is as it was. A rebuild, a reformatting or a change elsewhere in the project leaves a receipt holding |
| `stale` | A column it rests on was removed, or its formula, row conditions, sources or type changed, or an approved term it rests on changed its meaning or status, is no longer bound to the column or is no longer defined. Each change is shown with the formula before and after and, from git history, the commit that made it |
| `unchecked` | The graph is older than its inputs (with `--no-refresh`), so it cannot say whether the receipt holds |
| `tampered` | The record does not match its digest: it was changed after it was issued |
| `unreadable` | The line is not a receipt (not UTF-8, not JSON, or missing what a receipt holds), a newer Konstruct wrote it, or it is in a file that is not a regular file |

What changed without making a receipt stale is a note: a lineage no longer confirmed by `konstruct verify`, or a draft term reworded. A receipt issued by a Konstruct that prints formulas in another canonical form (after an upgrade of the SQL parser) cannot be compared on its formulas and conditions: those differences are set aside with a note, and the rest are compared. That a receipt holds says its basis is unchanged, not that the answer was right. The check exits with status 1 when any receipt does not hold, so it can run in CI; `--no-history` skips reading git history, and `--json` prints the result. `konstruct receipts show ID` prints one receipt as it was issued, and needs no graph.

## Where Receipts Are Kept

Receipts are JSON lines in `konstruct-out/receipts/`, one file for each day (UTC) of issue, so old receipts can be removed a day at a time. Only a newline ends a line. Each line carries `format_version` and a `digest`: `sha256:` and the SHA-256 of the UTF-8 bytes of the record without `digest`, written as JSON with its keys sorted, `,` and `:` as separators with no spaces, and every character written as itself rather than escaped. A reader ignores fields it does not know, which a later release may add. Receipts are written and read only as regular files in a real folder: a symbolic link in place of the folder or of a day's file is removed before a receipt is written, and is not followed when receipts are read. The digest finds a record edited by hand or damaged; it is not a signature, as whoever can write the file can write a new record with a matching digest, and a removed line is not found. Nothing is sent anywhere.

## From an Agent or Python

The MCP server's `receipt` tool issues a receipt as `konstruct receipts issue --json` does, and the server's instructions ask the agent to call it after answering a question about the data and to put the citation under its answer. {meth}`konstruct.Project.receipt`, {meth}`konstruct.Project.receipts` and {meth}`konstruct.Project.check_receipts` do the same from Python.
