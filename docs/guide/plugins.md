# Writing a Plugin

A plugin extends Konstruct without a fork. It can add:

- an **input**, such as an orchestrator, a BI tool or a loader, read into the graph;
- an **export format** for `konstruct export`;
- a **check** for `konstruct gate`;
- **ontology packs**, as data with no code;
- **tools** for the MCP server.

A plugin is a Python distribution installed in the same environment as Konstruct. It declares
[entry points](https://packaging.python.org/en/latest/specifications/entry-points/) in the groups
below, and Konstruct finds them through `importlib.metadata`. Konstruct's own Looker input, export
formats, gate rules and ontology packs are in the same registry and run through the same code, so a
plugin is read, validated and merged exactly as they are.

`examples/plugin` in the repository is a complete plugin, `konstruct-yaml-pipelines`: an input for
pipelines written as YAML, whose steps' SQL Konstruct analyses for column lineage, a gate check
that a changed pipeline names an owner, a data-only ontology pack and an MCP tool.

## Enabling Plugins

Installing a plugin does not make it run. A project lists the distributions it runs in
`konstruct.toml`:

```toml
[plugins]
enabled = ["konstruct-yaml-pipelines"]   # distribution names, as installed
```

`konstruct plugins` lists every installed plugin with its group, version and plugin API version,
and whether it runs for the project and why; `konstruct status` lists them too, without loading
any. `--no-plugins`, or `KONSTRUCT_NO_PLUGINS=1` in the environment, runs no plugin whatever the
configuration says. Konstruct's own components always run.

```text
$ konstruct plugins
Plugins (plugin API 1.0)

konstruct-yaml-pipelines 0.1.0: enabled
  inputs     pipelines                API 1.0
  checks     pipelines                API 1.0
  packs      pipelines                API 1.0
  mcp_tools  pipelines                API 1.0

Built in: inputs looker; exports openlineage, mermaid, dot, csv, ossie; checks breaking, approved-definition-changed, conflicting-definition; packs konstruct.
```

## The Security Model

Konstruct never imports or runs code from the repository it analyses. Plugins follow from that:

- **Plugins come only from installed distributions.** Konstruct reads entry points through
  `importlib.metadata` and nothing else. There is no plugin folder in a project, and
  `konstruct.toml` cannot name a path or module to load.
- **The enabled list chooses among installed distributions; it cannot add code.** A repository can
  name a plugin, but only one the operator has already installed runs.
- **Plugins are off until a project enables them.** A distribution can arrive as a dependency of
  something installed for another reason, in a shared CI image or a project's requirements, and
  its entry points stay inert until a project names it. The enabled list also records which
  plugins a graph depends on, so two machines with different packages installed build the same
  graph from the same repository, and a listed plugin that is missing is a build warning rather
  than a silent difference.
- **A change cannot turn off the plugins that judge it.** `konstruct gate` runs the plugins
  enabled at the base it compares with as well as those enabled in the change, both their inputs
  and their checks, so a pull request that takes a plugin out of `[plugins] enabled` is still
  checked by it. A change can still enable another installed plugin for itself.
- **A plugin runs with Konstruct's permissions, in Konstruct's process.** Installing a
  distribution already lets it run code in that environment; enabling it is not a sandbox. A
  plugin shares the process's global state: it can change modules Konstruct has imported, the
  environment and the working directory, and so the behaviour of everything after it in the
  same command. A plugin input reads the repository's files, so install plugins you trust to
  read the repositories you build, and run with `--no-plugins` where a branch's `konstruct.toml`
  is not trusted, such as a pull request from a fork.
- **A plugin is not timed.** Konstruct does not stop a plugin that runs too long; one that never
  returns stops the command with it. Give CI jobs a timeout of their own.
- **A plugin's errors do not break the build.** A plugin that raises, returns something invalid,
  cannot be imported or targets a plugin API version Konstruct does not run is reported by name,
  with the error, and the build goes on without it (see [Isolation](#isolation)).

## Entry Points

| Group | The entry point names | Protocol |
|-------|-----------------------|----------|
| `konstruct.inputs` | An object that discovers files and reads them into a fragment of the graph | {class}`~konstruct.plugins.InputPlugin` |
| `konstruct.exports` | An object that writes the graph in a format | {class}`~konstruct.plugins.ExportPlugin` |
| `konstruct.checks` | An object that returns the findings that stop a change | {class}`~konstruct.plugins.CheckPlugin` |
| `konstruct.packs` | A package directory of ontology packs, read without importing anything | (data) |
| `konstruct.mcp_tools` | An object that returns tools bound to a project | {class}`~konstruct.plugins.ToolsPlugin` |

```toml
# pyproject.toml of the plugin
[project.entry-points."konstruct.inputs"]
pipelines = "konstruct_yaml_pipelines:PIPELINES"

[project.entry-points."konstruct.checks"]
pipelines = "konstruct_yaml_pipelines:OWNERS"

[project.entry-points."konstruct.packs"]
pipelines = "konstruct_yaml_pipelines.packs"

[project.entry-points."konstruct.mcp_tools"]
pipelines = "konstruct_yaml_pipelines:TOOLS"
```

A code entry point names an instance (`module:attribute`), not a class. The entry point's name is
the plugin's name in that group: lower-case letters, digits, `-` and `_`. It may not be the name of
one of Konstruct's own components in the group, and an input may not take a name Konstruct uses
for its own inputs, configuration tables or node ids (`dbt`, `airflow`, `table`, `task` and so
on). Two distributions that provide the same name are both refused.

## The Plugin API Version

Every code plugin object declares the version of the plugin API it targets, and a pack directory
declares it in a `plugin.toml`:

```python
from konstruct.plugins import API_VERSION


class PipelineInput:
    api_version = API_VERSION
```

A version is `(major, minor)`; an integer such as `1` means `(1, 0)`, and `plugin.toml` takes
`api_version = 1` or `api_version = "1.0"`. The minor grows when the plugin API gains something a
plugin may use, and the major only when a type or protocol changes incompatibly. Konstruct runs a
plugin that targets its own major and a minor no higher than its own, so a plugin written for
1.0 runs on a Konstruct that runs 1.2, but not on one that runs 2.0, and a plugin for 1.2 is
refused by a Konstruct that runs only 1.0. A refused plugin is reported with the version it
targets and the versions Konstruct runs, rather than being run against types it was not written
for. `konstruct plugins` prints the version a release runs (plugin API 1.0 at present). The
plugin API is the `konstruct.plugins` module: the protocols and types below, documented in the
[Python API](../reference/python-api.md#plugins).

## An Input

An input has file-name `patterns` and a `read` method. Konstruct discovers the files whose names
match, as it does its own inputs (skipping `exclude` patterns, `konstruct-out` and folders such as
`target/` and `.venv/`), or reads the files and directories `[<name>] paths` lists in
`konstruct.toml`. It passes each file's path and bytes, and a context with the project root, the
SQL dialect, the input's own `konstruct.toml` table and a cache. An input with no patterns is
called once with no files, for one that reads from elsewhere.

`read` returns a {class}`~konstruct.plugins.Fragment`:

| Field | What it holds |
|-------|---------------|
| `nodes` | {class}`~konstruct.plugins.PluginNode`: DAGs, tasks, exposures and tests. A node's id in the graph is the input's name, a dot and its `id`, so `pipeline.daily_revenue` from the `pipelines` input is `pipelines.pipeline.daily_revenue` |
| `edges` | {class}`~konstruct.plugins.PluginEdge`: `feeds`, `derived_from`, `runs`, `precedes` and `tests` edges between the fragment's nodes, warehouse relations named by a {class}`~konstruct.plugins.Relation` and their columns named by a {class}`~konstruct.plugins.Column` |
| `sql` | {class}`~konstruct.plugins.Sql` that Konstruct analyses for column lineage as it does SQL files; with `task`, the task feeds the tables the SQL writes and the tables it reads feed the task |
| `warnings` | Problems with the input's files, given as build warnings with the input's name in front |
| `sources` | One {class}`~konstruct.plugins.Source` per file read, for the build summary |
| `counts` | Counts for the build's `inputs`, each named `<input>_<key>`; a name another input's count or one of Konstruct's own (such as `landed_tables`) already has is left out with a warning |

A {class}`~konstruct.plugins.Relation` resolves to the node the build already has for that warehouse
object, from dbt, the warehouse or SQL, so an input joins the lineage the rest of the build made. A
{class}`~konstruct.plugins.Column` of a relation whose columns the build knows from something that
defines it, and that is not one of them, leaves its edge out with a warning (`missing` replaces the
default text); otherwise the column is created. A SQL statement's `task` names a task node of the
fragment. Every node and edge records the input as its origin.

Konstruct checks the whole fragment as graph.json would hold it before it merges anything. Every
text field is a plain `str` (not a subclass, such as a string enum) of valid Unicode text, with no
lone surrogates. Node ids, relation and column names may not contain `#`, which separates a column
from its relation in node ids, and node ids, relation and column names, node paths and SQL paths may
not contain a line break. Node names, warnings, `missing` texts and sources are put on one line, a
line break becoming a space, so that they cannot start a heading in REPORT.md or break a diagram
label. Count names are lower-case letters, digits and `_`, and counts are integers within 64 bits,
not booleans.

Attributes are JSON data: dicts with string keys, lists, tuples (stored as lists), strings, integers
within 64 bits, floats, booleans and None, nested at most {data}`~konstruct.plugins.api.MAX_DEPTH`
(64) levels deep, with no NaN or infinity. Anything else, such as a dataclass, a date, an enum
member or a UUID, is refused, so a date that `yaml.safe_load` gives must be written as a string,
such as `value.isoformat()`. A plugin's attributes may not use a name Konstruct reads from a node or
an edge, so that what it claims about lineage, usage and the warehouse comes only from Konstruct:
{data}`~konstruct.plugins.api.RESERVED_NODE_ATTRS` lists the names a node may not set (such as
`expression`, `conditions`, `usage`, `inferred`, `in_warehouse` and `sql_path`), and
{data}`~konstruct.plugins.api.RESERVED_EDGE_ATTRS` those an edge may not set (such as `origin`,
`kind`, `verified`, `transform`, `via` and `role`). A node may set the names in
{data}`~konstruct.plugins.api.PLUGIN_NODE_ATTRS`, each a string with the meaning Konstruct gives it:
`test` on a test node (such as `unique` or `not_null`), `owner`, and `schedule`, `operator` and
`orchestrator`, which the graph viewer shows. Other names, such as `type`, `label` or `fields`, are
the plugin's own. An `Sql` dialect must be one sqlglot knows.

```python
from konstruct.plugins import API_VERSION, Fragment, PluginEdge, PluginNode, Source, Sql


class PipelineInput:
    api_version = API_VERSION
    patterns = ('*.pipeline.yml',)

    def read(self, files, context):
        fragment = Fragment()
        for file in files:
            data = yaml.safe_load(file.text)  # never yaml.load: the files come from the repository
            dag = f'pipeline.{data["pipeline"]}'
            fragment.nodes.append(PluginNode(dag, 'dag', data['pipeline'], path=file.path))
            for step in data['steps']:
                task = f'task.{data["pipeline"]}.{step["name"]}'
                fragment.nodes.append(PluginNode(task, 'task', step['name'], path=file.path))
                fragment.edges.append(PluginEdge(dag, task, 'runs'))
                fragment.sql.append(Sql(step['sql'], file.path, task=task))
            fragment.sources.append(Source(file.path, f'{len(data["steps"])} steps'))
        return fragment
```

`context.cached(key, compute)` keeps the result of expensive parsing for `konstruct build --update`:
`key` is a tuple of JSON data, such as a hash of the file's content, and the value `compute()`
returns must be JSON data too, as attributes are: `cached` raises on a key or value that is not, and
keeps nothing. Each call gives a fresh copy of the value as JSON reads it back, so a cold build and
an update see the same value (a tuple is a list in both), and changing the copy changes nothing
kept. The plugin's name and version are part of the key.

Like Konstruct's own inputs, a plugin input should parse what it reads and never run it: no
`import`, `exec` or `eval` of repository files, and `yaml.safe_load` rather than `yaml.load`.

## An Export Format

An export format has a `description` and an `export(graph, request)` method that returns text.
The entry point's name is the format's name, so `konstruct export <name>` runs it. The
{class}`~konstruct.plugins.ExportRequest` carries the options: `select` and `keep` (the node given
with `--select` and the ids of the relations and tasks in its lineage), `columns` and `namespace`.
A format that cannot write what was asked raises {class}`konstruct.outputs.export.ExportError`
with a message for the user.

## A Gate Check

A check has a `description` and a `check(context)` method that returns a list of
{class}`~konstruct.plugins.Finding`, each with a `rule`, a `message`, the `path` to change and a
`fix`. The {class}`~konstruct.plugins.CheckContext` holds the graph at the head, the
{class}`~konstruct.analysis.diff.Diff` of the change and the conflicting definitions. Konstruct
names a plugin's rules `<entry point>.<rule>`, so the example's rule is
`pipelines.owner-required`.

A finding's `rule`, `message` and `fix` are non-empty strings and its `path` a string or None, all
plain `str` of text JSON can hold. The gate puts each on one line and escapes `|` in the markdown
table. A check that raises, returns something other than such findings or cannot be loaded fails the
gate with a `<entry point>.failed` finding that says why: a check that could not run cannot say the
change is safe. The gate runs the checks of the plugins enabled at the base as well as those the
change enables (see [The Security Model](#the-security-model)).

## Ontology Packs

A pack entry point names a package directory (`konstruct_yaml_pipelines.packs`), not an object.
Konstruct finds the directory through the import system's search path without importing the
package, and reads no code from it. The directory holds a `plugin.toml` with the plugin API version
and one YAML file per pack, in the format of [Konstruct's own packs](ontology.md):

```text
konstruct_yaml_pipelines/packs/
  plugin.toml    # api_version = 1
  helpdesk.yml   # name, description, schemas, match and terms
```

A plugin's packs bind as Konstruct's do, and `[ontologies] packs` can choose them by name. A pack
whose name Konstruct or an earlier plugin already has is left out with a warning.

## MCP Tools

A tools plugin has a `tools(project)` method that returns each tool's short name with its function,
bound to the {class}`konstruct.Project` the server answers for. The server names each tool
`<entry point>_<name>`, so the example's `owners` is `pipelines_owners`; a tool whose name
another plugin's tool already has (`a_b` and `c` against `a` and `b_c`) is left out with a
warning. The function's typed
parameters are the tool's input, its docstring is the description agents read, and it returns
JSON-ready data. An error it raises is returned to the agent as a tool error.

## Isolation

- A plugin that raises, or returns a fragment that is not valid, is reported in the build's
  warnings and summary by name, with the error, and the build goes on without it. Nothing from an
  invalid fragment is merged: ids, kinds, edge ends and attributes are checked first.
- A plugin that cannot be imported, declares no `api_version`, targets another version or lacks
  part of its protocol is refused with the reason, in `konstruct plugins` and as a build warning.
- An export that fails is an error naming the plugin, and `konstruct export` exits with status 1.
- An MCP tool the server cannot describe, such as one with a parameter type that has no JSON
  schema, is left out with a warning naming the plugin; the other tools are served. So are a tool
  whose name is not a lower-case string or whose value is not a function, and every tool of a
  plugin whose `tools` raises or returns something other than a mapping.
- Entry points are read only by the commands that can run a plugin (`build`, `export`, `gate`,
  `mcp`, `status`, `plugins` and query commands that build a missing graph), and a plugin is
  imported only when its group is first used.

## Testing a Plugin

Install the plugin beside Konstruct in a virtual environment, enable it in a small project and
build it. Konstruct is installed from its repository, as it is not on PyPI: a bare `konstruct`
requirement could resolve to another package of that name. A plugin's own `pyproject.toml` names
it the same way (`"konstruct @ git+https://github.com/kodexlabsio/konstruct"`, with
`allow-direct-references` in `[tool.hatch.metadata]` for hatchling).

```bash
uv venv && uv pip install "konstruct @ git+https://github.com/kodexlabsio/konstruct" -e path/to/plugin
printf '[plugins]\nenabled = ["konstruct-yaml-pipelines"]\n' > project/konstruct.toml
uv run konstruct plugins -C project     # enabled, API 1.0
uv run konstruct build project --json   # the plugin's sources and counts, and no warnings
```

In pytest, a distribution can be installed without building it: write its `.dist-info` directory
into a temporary directory on `sys.path`, as pip writes one into `site-packages`, and Konstruct
finds its entry points as it finds any installed package:

```python
def install(site, name, version, entry_points):
    info = site / f'{name.replace("-", "_")}-{version}.dist-info'
    info.mkdir(parents=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    (info / 'entry_points.txt').write_text(
        ''.join(
            f'[{group}]\n' + ''.join(f'{k} = {v}\n' for k, v in entries.items())
            for group, entries in entry_points.items()
        )
    )


def test_the_plugin_reads_pipelines(tmp_path, monkeypatch):
    install(
        tmp_path / 'site',
        'konstruct-yaml-pipelines',
        '0.1.0',
        {'konstruct.inputs': {'pipelines': 'konstruct_yaml_pipelines:PIPELINES'}},
    )
    monkeypatch.syspath_prepend(str(tmp_path / 'site'))
    ...
    graph = konstruct.Project(project).build(warehouses=False).graph
    assert 'pipelines.pipeline.daily_revenue' in graph.nodes
```

`Fragment.problems()` returns what would stop a fragment being merged, so a unit test of `read`
can assert it is empty.
