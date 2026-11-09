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

Every result is a dataclass. `str(result)` is the text the CLI prints and `result.to_dict()` the
JSON that `--json` and the MCP server return, so a script can move between the three freely. A name
that matches nothing, or several things, raises {class}`konstruct.QueryError` with suggestions.

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

```{eval-rst}
.. autoclass:: konstruct.Project
   :no-members:

.. automethod:: konstruct.Project.build
.. automethod:: konstruct.Project.has_graph
.. automethod:: konstruct.Project.changed_inputs
.. automethod:: konstruct.Project.config
.. autoproperty:: konstruct.Project.graph
.. autoproperty:: konstruct.Project.root
.. autoproperty:: konstruct.Project.graph_path
.. autofunction:: konstruct.project.find_out_dir
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

term = project.define('lifetime value').terms[0]
print(term.definition, term.owner, [column.label for column in term.columns])

for conflict in project.conflicts().conflicts:
    print(conflict.idea, conflict.a.label, conflict.b.label, [d.detail for d in conflict.differences])

ontology = project.ontology()
print([(entity.name, entity.key) for entity in ontology.entities])
print(ontology.measure('customer lifetime value').columns[0].formula)
```

```{eval-rst}
.. automethod:: konstruct.Project.explain
.. automethod:: konstruct.Project.define
.. automethod:: konstruct.Project.conflicts
.. automethod:: konstruct.Project.ontology
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
```

```{eval-rst}
.. automethod:: konstruct.Project.diff
.. automethod:: konstruct.Project.verify
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

.. autoclass:: konstruct.explain.Step
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
.. autoclass:: konstruct.ontology.Ontology
   :members:

.. autoclass:: konstruct.ontology.Entity
   :members:

.. autoclass:: konstruct.ontology.Measure
   :members:

.. autoclass:: konstruct.ontology.MeasureColumn
   :members:

.. autoclass:: konstruct.ontology.Relationship
   :members:

.. autoclass:: konstruct.ontology.Rule
   :members:
```

### Conflicts

```{eval-rst}
.. autoclass:: konstruct.conflicts.Conflicts
   :members:

.. autoclass:: konstruct.conflicts.Conflict
   :members:

.. autoclass:: konstruct.conflicts.Side
   :members:

.. autoclass:: konstruct.conflicts.Difference
   :members:
```

### Changes

```{eval-rst}
.. autoclass:: konstruct.diff.Diff
   :members:

.. autoclass:: konstruct.diff.RelationChange
   :members:

.. autoclass:: konstruct.diff.ColumnChange
   :members:

.. autoclass:: konstruct.diff.DagChange
   :members:

.. autoclass:: konstruct.diff.YamlChange
   :members:

.. autoclass:: konstruct.diff.DefinitionsChange
   :members:

.. autoclass:: konstruct.diff.Selection
   :members:
```

### Verification

```{eval-rst}
.. autoclass:: konstruct.verify.Verification
   :members:

.. autoclass:: konstruct.verify.ModelResult
   :members:
```

### Builds

```{eval-rst}
.. autoclass:: konstruct.build.BuildResult
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
   :members: read, write, to_dict, resolve, label, node_for_relation, nodes_for_relation, columns_of, column, parent_of, out_edges, in_edges, impact, trace, path, add_node, add_edge, warn

.. autoclass:: konstruct.Node
   :members:

.. autoclass:: konstruct.Edge
   :members:

.. autoclass:: konstruct.graph.Step
   :members:
```

## Lower-Level Modules

These are stable enough to build on, but `Project` is the supported entry point.

### SQL Lineage

```{eval-rst}
.. autofunction:: konstruct.sql.analyse

.. autoclass:: konstruct.sql.StatementLineage
   :members:

.. autoclass:: konstruct.sql.ColumnSource
   :members:
```

### Formulas, Conflicts and Export

```{eval-rst}
.. autoclass:: konstruct.explain.Explainer
   :members: explain, compose, steps, summaries

.. autofunction:: konstruct.explain.explain

.. autofunction:: konstruct.conflicts.find_conflicts

.. autofunction:: konstruct.conflicts.core

.. autofunction:: konstruct.export.export
```

### Business Definitions

```{eval-rst}
.. autofunction:: konstruct.definitions.parse

.. autofunction:: konstruct.definitions.parse_file

.. autofunction:: konstruct.definitions.validate

.. autoclass:: konstruct.definitions.Term
   :members: name, definition, synonyms, owner, status, approved, grain, columns, filters, unit, currency, related, exceptions, slug, names

.. autoclass:: konstruct.definitions.Owner
```

### Configuration, Usage and Builds

```{eval-rst}
.. autofunction:: konstruct.config.load_config

.. autoclass:: konstruct.usage.UsageSettings
   :members:

.. autofunction:: konstruct.build.build
```

## Errors

```{eval-rst}
.. autoexception:: konstruct.QueryError

.. autoexception:: konstruct.diff.DiffError

.. autoexception:: konstruct.config.ConfigError

.. autoexception:: konstruct.warehouse.MissingDriver
```
