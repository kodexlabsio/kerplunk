# The Konstruct Manifesto

## Truth in Data

Every decision an organisation makes rests on numbers, and few of those numbers come with an explanation anyone can check. A figure on a dashboard is the last link of a chain that runs through loaders, SQL models, orchestration code and semantic layers, written by many hands in many tools. When someone asks where it came from, what it includes, or whether it is right, the honest answer is usually: somebody would have to read all of that code to know.

We think that is the wrong way for data to work, and we wrote Konstruct to change it.

This manifesto says what we believe, what Konstruct does about it, and what Konstruct promises to the people who use it. Every promise below is true of the code in this repository today, and each one says how to check it.

## What We Believe

### 1. No one should have to trust a number they cannot trace and prove.

A number is a claim. A claim without its derivation is an opinion with a decimal point. Anyone who is asked to act on a number has the right to see, in full, how it was calculated: from which columns, through which joins and filters, by which formula, and with what evidence that the account is true.

Tracing is not enough. Lineage drawn by a tool is itself a claim, and a claim nobody checks is a guess that looks like a fact. Lineage must be measured against the data, and the measurement must be repeatable by anyone.

### 2. Lineage and meaning belong to the people who own the data.

The map of how your data flows and the definitions of what it means are knowledge about your organisation. They should live where your code lives, in plain files you can read, review, diff and keep, and never only inside a service you cannot see into. If the tool disappears tomorrow, the knowledge must remain.

### 3. The tool that tells you what your data means must itself be open, local and verifiable.

You cannot ask people to trust their numbers because a closed box says so. The tool that explains your data must show its own working: its source open to read, its claims testable on your data, its results reproducible on your machine. It must run where your data is, without sending your code, your schemas or your questions anywhere.

### 4. No telemetry. No lock-in. MIT.

A tool that reads your data stack holds the most sensitive map you have. It has no business reporting on you. It should not hold your knowledge hostage in a format only it can read. And it should be free in the fullest sense the MIT licence gives: to use, study, change and share, for any purpose, by anyone.

### 5. Open standards, in and out.

Knowledge about data is only useful if it can move. Lineage should be written in the open format the industry shares, OpenLineage. Semantic models should be read and written in the open specification for them, Apache Ossie, and come back exactly as they were authored. Vocabularies should be read as SKOS and OWL. No one should have to rebuild their understanding of their data because they changed tools.

### 6. Agents must be grounded in proven facts.

AI agents now write SQL, change models and answer questions about data. An agent that guesses at a schema produces numbers that are confident, plausible and wrong. Agents must be given facts: the lineage, the formulas, the approved definitions and the evidence for each, and they must be stopped before they ship a change that breaks what others rely on. The more autonomy we give machines over data, the more the facts they act on must be proven rather than presumed.

## What Konstruct Is

Konstruct reads a data repository and its warehouse (dbt, SQL, Airflow, Dagster, PySpark, Looker, Power BI, Fivetran and Airbyte) and builds one graph of every table, column, model, task, test, dashboard and business term, with column-level lineage parsed from the SQL that connects them. It composes each column's formula through every model upstream, so a number reads as an expression over its sources. It derives an ontology of the business from the code and its tests. And it checks its own lineage against the data: `konstruct verify` perturbs each input, reruns each model and reports which of the graph's claims the data confirms, which it did not exercise, and which dependencies it observed that the graph does not explain.

It answers from the command line, a Python API, an MCP server for coding agents, a GitHub Action, and one offline HTML page. It runs in one command, on your machine.

## Our Commitments

1. **Konstruct is MIT licensed.** Every release is published under the MIT licence in [LICENSE](LICENSE), which lets anyone use, study, change and share it, for any purpose.

2. **Konstruct collects no telemetry.** It makes no network requests of its own beyond the warehouses you configure; when it runs dbt, it turns dbt's anonymous usage statistics off, and on DuckDB `konstruct verify` runs a model's SQL against copies of its inputs with DuckDB's external access off, so a model that reads files or URLs directly is skipped rather than run. `graph.html` loads nothing from the network, and the browser tests fail if it requests anything. See [SECURITY.md](SECURITY.md).

3. **Konstruct runs locally.** No server, no account, no sign-in. To check, run `konstruct build --no-dbt --no-warehouse` in a repository on a machine with no network: it builds the graph from the files alone.

4. **Konstruct never imports or runs your orchestration code.** Airflow, Dagster and PySpark files are parsed with Python's `ast` module, and dbt Jinja is rendered in a sandbox. dbt itself is the exception: by default, when a project's artefacts are missing or stale, `konstruct build` runs `dbt docs generate`, which runs the project's macros and on-run hooks with its own profile. With `--no-dbt --no-warehouse`, the GitHub Action's defaults, dbt never runs and no connection is opened, and Konstruct is then safe to run on a pull request you have not reviewed. [SECURITY.md](SECURITY.md) lists what runs and when.

5. **Konstruct never writes to your warehouse.** It reads metadata. `konstruct verify`, the only command that runs a model's SQL, works on copies or inside a transaction that is always rolled back. This holds as worded with `--no-dbt`; without it, the dbt run of commitment 4 runs your project's own hooks, which may write.

6. **What Konstruct learns is yours, in plain files.** The graph, the report, the ontology and the verification results are written to `konstruct-out/` as JSON, YAML and Markdown in your repository, and your definitions stay in `definitions.yml`. All of it is readable without Konstruct: open the files after a build, and see [the outputs reference](https://kodexlabsio.github.io/konstruct/reference/outputs.html) for their formats.

7. **Konstruct speaks open formats.** `konstruct export` writes OpenLineage events, which the tests validate against the OpenLineage schemas, Apache Ossie models (a model Konstruct reads is written back as a valid model, with its additions in a custom extension), Mermaid, Graphviz DOT and CSV. It reads Apache Ossie models and SKOS and OWL vocabularies.

8. **Every lineage claim Konstruct makes can be checked by you, on your data.** `konstruct verify` is seeded, so a run repeats exactly, and it writes its evidence to `VERIFY.md`. The verification results for the bundled example and for dbt Labs' jaffle-shop, the comparison of Konstruct's own dbt rendering with dbt's compiled SQL, and the Apache Ossie portability grades are tests that run on every change, and every measurement in [the paper](https://kodexlabsio.github.io/konstruct/paper/konstruct.pdf) is regenerated from the code each time it is built.

9. **Agents get facts with their evidence.** The MCP server answers with formulas, sources, approved definitions and which edges the data confirmed. `konstruct gate` fails a change that removes a column something still reads, alters what an approved term means, or computes a business idea differently from how it is already computed, and says how to fix it. [The MCP reference](https://kodexlabsio.github.io/konstruct/reference/mcp.html) lists every tool and what it returns, and `tests/test_gate.py` shows each failure the gate catches.

10. **Konstruct shows its limits.** What static analysis cannot follow is a warning in every build, and what Konstruct does not do is listed in [the documentation](https://kodexlabsio.github.io/konstruct/reference/limitations.html). A tool that claims to tell the truth about data must first tell the truth about itself.

## Answers to Objections

**"Our lineage tool already draws the graph."** Drawing is the start. The question is whether the drawing is right, and how each value on it is calculated. Konstruct measures its lineage against the data and states each column as a formula. It writes OpenLineage, so it adds to the tools you have rather than asking you to leave them.

**"Perturbation cannot prove everything."** It cannot, and we say so. When a perturbation changes an output's values, the data has shown a real dependency, reproducible from the recorded seed; some comparisons also lean on the graph's own claims of independence, and the paper says which. When an edge is not exercised, the data simply did not test it. Evidence that is honest about its reach is worth more than certainty that is assumed.

**"Why does it have to be local?"** Because your schemas, your SQL and your questions describe your organisation, and the safest place for them is where they already are. A tool that needs nothing but your repository can be run by anyone, audited by anyone and trusted for reasons rather than reputation.

**"Why make it free?"** Because the way to make traceable, checked numbers normal is to let everyone use, study, change and share the tool that produces them, and to let anyone confirm that it does what it says.

**"Will agents not simply get better on their own?"** Better models still cannot know what a column means in your organisation unless someone tells them, and they cannot know whether a lineage claim is true unless something checks it. Grounding is not a crutch for weak models; it is what turns a capable model into a trustworthy one.

## What We Ask

If you work with data, ask of every number you are given: where did it come from, how was it calculated, and how do we know? Expect an answer you can check.

If you build tools for data, show your working, speak open formats, and let people keep what they learn about their own systems.

If you build agents, ground them in facts that have been proven, and stop them before they break what others depend on.

Konstruct is our contribution. It is open, it is local, it checks its lineage against your data, and it is yours.
