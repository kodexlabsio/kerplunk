# Python API

Everything the command line and the MCP server do is available from Python, through one class,
{class}`konstruct.Project`, and typed results.

```python
import konstruct

project = konstruct.Project('path/to/repo')
project.build(update=True)

walk = project.impact('stg_payments.amount')
print(walk.summary())  # 7 columns, 3 relations, 5 tests, 1 exposure, 2 tasks and 2 business terms downstream.
for column in walk.columns:
    print(column.depth, column.label, column.transform, column.path)
```

Every result is a dataclass. `str(result)` is the text the CLI prints to a pipe, `result.text(style)`
the same result drawn for a terminal (with a `konstruct.interfaces.layout.Style` giving colour,
Unicode and width), and `result.to_dict()` the JSON that `--json` and the MCP server return, so a
script can move between them freely (`--json` and MCP add the result's `format_version`, see
[Compatibility](outputs.md#compatibility)). A name that matches nothing, or several things, raises
{class}`konstruct.QueryError`, whose `suggestions` are the closest names and `hint` a command to try,
and a `graph.json` written by a newer Konstruct raises {class}`konstruct.FormatError`.

`konstruct.Project`, the result types it returns and {class}`konstruct.QueryError` are imported from
`konstruct` itself. The modules behind them are grouped by role, for code that needs one directly:

| Package | Modules |
|---------|---------|
| `konstruct.core` | `graph` (the graph and its traversal), `sql` (column lineage from SQL), `mutate` (mutants of a query), `jsonio` |
| `konstruct.inputs` | `dbt`, `jinja`, `airflow`, `dagster`, `spark`, `looker`, `powerbi`, `loaders`, `ingestion`, `hcl`, `warehouse`, `usage` |
| `konstruct.engine` | `build`, `config`, `worker` |
| `konstruct.semantics` | `definitions`, `vocabulary`, `ontology`, `world`, `ossie`, `rows` (whether a composed formula counts its column's rows), `registry` |
| `konstruct.analysis` | `query`, `semantic`, `explain`, `english`, `conflicts`, `diff`, `gate`, `verify`, `integrity`, `mutation`, `why`, `release_notes`, `effect`, `laws`, `history`, `receipts`, `lock` |
| `konstruct.outputs` | `report`, `html`, `export` |
| `konstruct.interfaces` | `cli`, `project`, `mcp_server`, `skill`, `console`, `status` |
| `konstruct.plugins` | `api` (the plugin API: protocols and types), `registry` (finding, enabling and loading plugins) |

## Opening and Building a Project

A `Project` points at a repository. It reads nothing until asked: {meth}`~konstruct.Project.build`
writes `konstruct-out/`, and every question reads the graph from there, rereading it whenever a
build changes it.

```python
project = konstruct.Project('analytics')  # or any directory inside a built project
if not project.has_graph() or project.changed_inputs():
    result = project.build(update=True)  # reuse the analysis of anything unchanged
    print(result.inputs)  # {'dbt_projects': 1, 'warehouses': 1, ...}
    print(result.graph.warnings)  # problems with the inputs, if any
```

The repository's own warehouse connections and paths outside it are used, and dbt is run, once
{meth}`~konstruct.Project.trust` has recorded them, as `konstruct trust` does (see
[Trusting a Project's Connections](configuration.md#trusting-a-projects-connections)).

```{eval-rst}
.. autoclass:: konstruct.Project
   :no-members:

.. automethod:: konstruct.Project.build
.. automethod:: konstruct.Project.has_graph
.. automethod:: konstruct.Project.changed_inputs
.. automethod:: konstruct.Project.config
.. automethod:: konstruct.Project.plugins
.. automethod:: konstruct.Project.trust
.. autoproperty:: konstruct.Project.graph
.. autoproperty:: konstruct.Project.root
.. autoproperty:: konstruct.Project.graph_path
.. autofunction:: konstruct.interfaces.project.find_out_dir
```

## Impact, Trace and Paths

```python
downstream = project.impact('orders.amount')
upstream = project.trace('customer_ltv.lifetime_value')
for column in upstream.columns:
    print(column.depth, column.label, column.transform, column.verified)

path = project.path_between('raw_payments', 'customer_dashboard')
print(' -> '.join(step.label for step in path.steps))
```

```{eval-rst}
.. automethod:: konstruct.Project.impact
.. automethod:: konstruct.Project.trace
.. automethod:: konstruct.Project.path_between
```

## Finding and Describing Nodes

```python
project.search('lifetime value', kinds=['column']).results
project.search('revenue', mode='semantic').results  # by meaning: synonyms, formulas, lineage
project.describe('orders').columns
project.list(['model', 'table']).items
project.query('what breaks if stg_payments.amount changes').impact
```

```{eval-rst}
.. automethod:: konstruct.Project.search
.. automethod:: konstruct.Project.describe
.. automethod:: konstruct.Project.list
.. automethod:: konstruct.Project.query
```

## Formulas, Definitions, Conflicts and the Ontology

```python
explanation = project.explain('orders.credit_card_amount')
print(explanation.composed)
# SUM(CASE WHEN raw_payments.payment_method = 'credit_card' THEN raw_payments.amount / 100 ELSE 0 END)
print(project.english('orders.credit_card_amount').english)
# the sum of ([amount] divided by 100 if [payment method] is 'credit_card', else 0)

term = project.define('lifetime value').terms[0]
print(term.definition, term.owner, [column.label for column in term.columns])

for conflict in project.conflicts().conflicts:
    print(conflict.idea, conflict.a.label, conflict.b.label, [d.detail for d in conflict.differences])

history = project.history('customers.customer_lifetime_value')
print([(change.commit.author, change.before, change.after) for change in history.formulas])
print(history.last_approval.commit.author if history.last_approval else None)

ontology = project.ontology()
print([(entity.name, entity.key) for entity in ontology.entities])
print(ontology.measure('customer lifetime value').columns[0].formula)
```

```{eval-rst}
.. automethod:: konstruct.Project.explain
.. automethod:: konstruct.Project.english
.. automethod:: konstruct.Project.history
.. automethod:: konstruct.Project.define
.. automethod:: konstruct.Project.conflicts
.. automethod:: konstruct.Project.ontology
.. automethod:: konstruct.Project.entity
.. automethod:: konstruct.Project.metric
.. automethod:: konstruct.Project.relate
```

## Checking SQL

```python
result = project.check_sql(open('query.sql').read())
if not result.passed:
    for finding in result.findings:
        print(finding.severity, finding.rule, finding.message, finding.fix)
    print(result.corrected)  # the SQL with the safe fixes applied, or None

history = project.check_history(days=30, pseudonymise=True)
for metric in history.shadow:
    print(metric.term, metric.detail, metric.queries, metric.users)
```

```{eval-rst}
.. automethod:: konstruct.Project.check_sql
.. automethod:: konstruct.Project.check_history
```

## Reviewing Changes and Checking Lineage

```python
change = project.diff(base='origin/main')
if change.breaking:
    for relation, column in change.breaking:
        print(f'{relation}.{column.column} is still read by {", ".join(column.breaks)}')
print(change.selection.command())  # dbt build --select <only what changed>

result = project.verify(seed=1729)
print(result.totals())  # {'claimed': 36, 'confirmed': 36, ...}

tests = project.mutate(models=['orders'], max_mutants=10)
print(tests.score)  # killed / (killed + survived)
for survivor in tests.models[0].survivors:
    print(survivor.mutant.description, survivor.suggestions[:1])

rows = project.why('customer_ltv.lifetime_value', {'customer_id': '3'})
print(rows.value, [c.values for c in rows.contributions])

notes = project.release_notes('v1.4')
print(notes.markdown())
effects = project.effect('v1.4', models=['customers'])
print([(c.headline(), c.effect.summary()) for c in effects.changes if c.effect])

receipt = project.receipt(['customer_ltv.lifetime_value'], question='LTV of customer 3?')
print(receipt.citation)
print([(r.id, r.status) for r in project.check_receipts().receipts])

laws = project.laws(confirm=True)  # writes konstruct-out/laws.json, which the gate reads
for law in laws.laws:
    print(law.status, law.statement, law.detail)

project.lock()  # pin the upstream columns this project reads in konstruct.lock
pins = project.check_lock()
for pinned in pins.changes:
    print(pinned.column, pinned.reasons, pinned.accept)
```

```{eval-rst}
.. automethod:: konstruct.Project.diff
.. automethod:: konstruct.Project.gate
.. automethod:: konstruct.Project.verify
.. automethod:: konstruct.Project.mutate
.. automethod:: konstruct.Project.why
.. automethod:: konstruct.Project.release_notes
.. automethod:: konstruct.Project.effect
.. automethod:: konstruct.Project.receipt
.. automethod:: konstruct.Project.receipts
.. automethod:: konstruct.Project.check_receipts
.. automethod:: konstruct.Project.laws
.. automethod:: konstruct.Project.lock
.. automethod:: konstruct.Project.check_lock
.. automethod:: konstruct.Project.export
```

## Results

### Walks and Items

```{eval-rst}
.. autoclass:: konstruct.Walk
   :members:
   :exclude-members: SECTIONS

.. autoclass:: konstruct.Item
   :members:

.. autoclass:: konstruct.PathResult
   :members:

.. autoclass:: konstruct.SearchResult
   :members:

.. autoclass:: konstruct.Listing
   :members:

.. autoclass:: konstruct.Description
   :members:
   :exclude-members: LISTS, HIDDEN_ATTRS

.. autoclass:: konstruct.QueryResult
   :members:
   :exclude-members: EDGE_LIMIT

.. autoclass:: konstruct.EdgeItem
   :members:
```

### Formulas

```{eval-rst}
.. autoclass:: konstruct.Explanation
   :members:

.. autoclass:: konstruct.analysis.explain.Step
   :members:

.. autoclass:: konstruct.analysis.english.English
   :members:

.. autoclass:: konstruct.analysis.english.Rows
   :members:

.. autoclass:: konstruct.analysis.english.Term
   :members:

.. autoclass:: konstruct.analysis.english.Name
   :members:
```

### History

```{eval-rst}
.. autoclass:: konstruct.analysis.history.History
   :members:

.. autoclass:: konstruct.analysis.history.FormulaChange
   :members:

.. autoclass:: konstruct.analysis.history.TermChange
   :members:

.. autoclass:: konstruct.analysis.history.Commit
   :members:
```

### Business Terms

```{eval-rst}
.. autoclass:: konstruct.Definitions
   :members:

.. autoclass:: konstruct.TermDefinition
   :members:
```

### Ontology

```{eval-rst}
.. autoclass:: konstruct.semantics.ontology.Ontology
   :members:

.. autoclass:: konstruct.semantics.ontology.Entity
   :members:

.. autoclass:: konstruct.semantics.ontology.Measure
   :members:

.. autoclass:: konstruct.semantics.ontology.MeasureColumn
   :members:

.. autoclass:: konstruct.semantics.ontology.Relationship
   :members:

.. autoclass:: konstruct.semantics.world.EntityDetail
   :members:

.. autoclass:: konstruct.semantics.world.Property
   :members:

.. autoclass:: konstruct.semantics.world.Metrics
   :members:

.. autoclass:: konstruct.semantics.world.Metric
   :members:

.. autoclass:: konstruct.semantics.world.Connection
   :members:

.. autoclass:: konstruct.semantics.world.Hop
   :members:

.. autoclass:: konstruct.semantics.ontology.Rule
   :members:
```

### Conflicts

```{eval-rst}
.. autoclass:: konstruct.analysis.conflicts.Conflicts
   :members:

.. autoclass:: konstruct.analysis.conflicts.Conflict
   :members:

.. autoclass:: konstruct.analysis.conflicts.Side
   :members:

.. autoclass:: konstruct.analysis.conflicts.Difference
   :members:
```

### Changes

```{eval-rst}
.. autoclass:: konstruct.analysis.diff.Diff
   :members:

.. autoclass:: konstruct.analysis.diff.RelationChange
   :members:

.. autoclass:: konstruct.analysis.diff.ColumnChange
   :members:

.. autoclass:: konstruct.analysis.diff.DagChange
   :members:

.. autoclass:: konstruct.analysis.diff.YamlChange
   :members:

.. autoclass:: konstruct.analysis.diff.DefinitionsChange
   :members:

.. autoclass:: konstruct.analysis.diff.Selection
   :members:
```

### The Gate

```{eval-rst}
.. autoclass:: konstruct.analysis.gate.Gate
   :members:

.. autoclass:: konstruct.analysis.gate.Finding
   :members:
```

### SQL Checks

```{eval-rst}
.. autoclass:: konstruct.analysis.checksql.SqlCheck
   :members:

.. autoclass:: konstruct.analysis.checksql.Finding
   :members:

.. autoclass:: konstruct.analysis.checksql.HistoryCheck
   :members:

.. autoclass:: konstruct.analysis.checksql.ShadowMetric
   :members:

.. autoclass:: konstruct.analysis.checksql.Adoption
   :members:
```

### Verification

```{eval-rst}
.. autoclass:: konstruct.analysis.verify.Verification
   :members:

.. autoclass:: konstruct.analysis.verify.ModelResult
   :members:

.. autoclass:: konstruct.analysis.integrity.KeyCheck
   :members:

.. autoclass:: konstruct.analysis.integrity.LinkCheck
   :members:
```

### Mutation Testing

```{eval-rst}
.. autoclass:: konstruct.analysis.mutation.MutationTesting
   :members:

.. autoclass:: konstruct.analysis.mutation.ModelMutation
   :members:

.. autoclass:: konstruct.analysis.mutation.MutantResult
   :members:

.. autoclass:: konstruct.analysis.mutation.TestCheck
   :members:

.. autoclass:: konstruct.analysis.mutation.Suggestion
   :members:

.. autoclass:: konstruct.analysis.mutation.ColumnScore
   :members:

.. autoclass:: konstruct.core.mutate.Mutant
   :members:

.. autofunction:: konstruct.core.mutate.mutants
```

### The Rows Behind a Value

```{eval-rst}
.. autoclass:: konstruct.analysis.why.Why
   :members:

.. autoclass:: konstruct.analysis.why.Contribution
   :members:

.. autoclass:: konstruct.analysis.why.Exclusion
   :members:

.. autoclass:: konstruct.analysis.why.SourceRow
   :members:

.. autoclass:: konstruct.analysis.why.Aggregate
   :members:
```

### Data Laws

```{eval-rst}
.. autofunction:: konstruct.analysis.laws.propose

.. autofunction:: konstruct.analysis.laws.confirm

.. autofunction:: konstruct.analysis.laws.violations

.. autofunction:: konstruct.analysis.laws.load

.. autofunction:: konstruct.analysis.laws.recorded

.. autoclass:: konstruct.analysis.laws.Laws
   :members:

.. autoclass:: konstruct.analysis.laws.Law
   :members:

.. autoclass:: konstruct.analysis.laws.Term
   :members:

.. autoclass:: konstruct.analysis.laws.Step
   :members:

.. autoclass:: konstruct.analysis.laws.Violation
   :members:

.. autoclass:: konstruct.analysis.laws.Failure
   :members:

.. autoclass:: konstruct.analysis.laws.Blocked
   :members:
```

### Answer Receipts

```{eval-rst}
.. autoclass:: konstruct.analysis.receipts.Receipt
   :members:

.. autoclass:: konstruct.analysis.receipts.Build
   :members:

.. autoclass:: konstruct.analysis.receipts.ReceiptCheck
   :members:

.. autoclass:: konstruct.analysis.receipts.CheckedReceipt
   :members:
```

### The Lineage Lockfile

```{eval-rst}
.. autofunction:: konstruct.analysis.lock.lock

.. autofunction:: konstruct.analysis.lock.check

.. autofunction:: konstruct.analysis.lock.check_consumers

.. autoclass:: konstruct.analysis.lock.LockResult
   :members:

.. autoclass:: konstruct.analysis.lock.LockCheck
   :members:

.. autoclass:: konstruct.analysis.lock.PinChange
   :members:

.. autoclass:: konstruct.analysis.lock.Attribution
   :members:

.. autoclass:: konstruct.analysis.lock.Lockfile
   :members:

.. autoclass:: konstruct.analysis.lock.Pin
   :members:

.. autoclass:: konstruct.analysis.lock.Rows
   :members:

.. autoclass:: konstruct.analysis.lock.PinnedTerm
   :members:

.. autoclass:: konstruct.analysis.lock.LockError
```

### Builds

```{eval-rst}
.. autoclass:: konstruct.engine.build.BuildResult
   :members:
```

## The Graph

`project.graph` is the {class}`konstruct.Graph` behind every answer, for anything the questions
above do not cover. Nodes and edges are plain dataclasses; the edge kinds and their directions are
listed in [Outputs and the Graph Format](outputs.md).

```python
graph = project.graph
orders = graph.resolve('orders')[0]
for column in graph.columns_of(orders.id):
    upstream = [graph.label(graph.nodes[e.target]) for e in graph.out_edges(column.id, 'derived_from')]
    print(column.name, column.attrs.get('expression'), upstream)
```

```{eval-rst}
.. autoclass:: konstruct.Graph
   :members: read, from_json, from_dict, write, to_dict, resolve, label, node_for_relation, nodes_for_relation, columns_of, column, parent_of, out_edges, in_edges, impact, trace, path, add_node, add_edge, warn

.. autoclass:: konstruct.Node
   :members:

.. autoclass:: konstruct.Edge
   :members:

.. autoclass:: konstruct.core.graph.Step
   :members:
```

## Lower-Level Modules

These are stable enough to build on, but `Project` is the supported entry point.

### SQL Lineage

```{eval-rst}
.. autofunction:: konstruct.core.sql.analyse

.. autoclass:: konstruct.core.sql.StatementLineage
   :members:

.. autoclass:: konstruct.core.sql.ColumnSource
   :members:
```

### Release Notes

```{eval-rst}
.. autoclass:: konstruct.analysis.release_notes.ReleaseNotes
   :members:

.. autoclass:: konstruct.analysis.release_notes.Change
   :members:

.. autoclass:: konstruct.analysis.release_notes.Affected
   :members:

.. autoclass:: konstruct.analysis.release_notes.Approval
   :members:

.. autoclass:: konstruct.analysis.effect.Effect
   :members:

.. autoclass:: konstruct.analysis.effect.ColumnEffect
   :members:
```

### Search by Meaning

```{eval-rst}
.. autofunction:: konstruct.analysis.semantic.search

.. autofunction:: konstruct.analysis.semantic.similar

.. autoclass:: konstruct.analysis.semantic.SimilarResult
   :members:

.. autofunction:: konstruct.analysis.semantic.vector_index
```

### Formulas, Conflicts and Export

```{eval-rst}
.. autoclass:: konstruct.analysis.explain.Explainer
   :members: explain, compose, steps, summaries

.. autofunction:: konstruct.analysis.explain.explain

.. autofunction:: konstruct.analysis.conflicts.find_conflicts

.. autofunction:: konstruct.analysis.conflicts.core

.. autofunction:: konstruct.outputs.export.export
```

### Business Definitions

```{eval-rst}
.. autofunction:: konstruct.semantics.definitions.parse

.. autofunction:: konstruct.semantics.definitions.parse_file

.. autofunction:: konstruct.semantics.definitions.validate

.. autoclass:: konstruct.semantics.definitions.Term
   :members: name, definition, synonyms, owner, status, approved, grain, columns, filters, unit, currency, related, exceptions, slug, names

.. autoclass:: konstruct.semantics.definitions.Owner
```

### Configuration, Usage and Builds

```{eval-rst}
.. autofunction:: konstruct.engine.config.load_config

.. autoclass:: konstruct.engine.trust.Trust
   :members:

.. autofunction:: konstruct.engine.trust.check

.. autofunction:: konstruct.engine.trust.trust

.. autofunction:: konstruct.engine.trust.revoke

.. autofunction:: konstruct.engine.trust.trust_file

.. autoclass:: konstruct.inputs.usage.UsageSettings
   :members:

.. autofunction:: konstruct.engine.build.build
```

## Plugins

The plugin API (see [Writing a Plugin](../guide/plugins.md)): what a plugin provides for each
entry-point group, and what Konstruct gives it. Everything here is imported from
`konstruct.plugins`.

```{eval-rst}
.. autodata:: konstruct.plugins.api.API_VERSION

.. autodata:: konstruct.plugins.api.RESERVED_NODE_ATTRS
   :no-value:

.. autodata:: konstruct.plugins.api.PLUGIN_NODE_ATTRS

.. autodata:: konstruct.plugins.api.PLUGIN_LIST_ATTRS

.. autodata:: konstruct.plugins.api.PLUGIN_MAP_ATTRS

.. autodata:: konstruct.plugins.api.RESERVED_EDGE_ATTRS
   :no-value:

.. autodata:: konstruct.plugins.api.MAX_DEPTH

.. autoclass:: konstruct.plugins.InputPlugin
   :members:

.. autoclass:: konstruct.plugins.InputFile
   :members:

.. autoclass:: konstruct.plugins.InputContext
   :members:

.. autoclass:: konstruct.plugins.Fragment
   :members:

.. autoclass:: konstruct.plugins.PluginNode
   :members:

.. autoclass:: konstruct.plugins.PluginEdge
   :members:

.. autoclass:: konstruct.plugins.Relation
   :members:

.. autoclass:: konstruct.plugins.Column
   :members:

.. autoclass:: konstruct.plugins.Sql
   :members:

.. autoclass:: konstruct.plugins.Source
   :members:

.. autoclass:: konstruct.plugins.ExportPlugin
   :members:

.. autoclass:: konstruct.plugins.ExportRequest
   :members:

.. autoclass:: konstruct.plugins.CheckPlugin
   :members:

.. autoclass:: konstruct.plugins.CheckContext
   :members:

.. autoclass:: konstruct.plugins.ToolsPlugin
   :members:

.. autoclass:: konstruct.plugins.Plugins
   :members: plugins, missing, off, problems, discover, for_config, builtin, get, group, installed, names, tools

.. autoclass:: konstruct.plugins.Plugin
   :members:

.. autoexception:: konstruct.plugins.PluginError
```

## Errors

```{eval-rst}
.. autoexception:: konstruct.QueryError

.. autoexception:: konstruct.FormatError

.. autoexception:: konstruct.analysis.diff.DiffError

.. autoexception:: konstruct.engine.config.ConfigError

.. autoexception:: konstruct.engine.config.UntrustedConfig

.. autoexception:: konstruct.inputs.warehouse.MissingDriver
```
