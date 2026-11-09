---
name: new-input
description: Add a new input to Konstruct, such as an orchestrator, a warehouse connector or a file format, wired through discovery, the build, the graph, tests and docs. Use when Konstruct should read something it does not yet.
---

# Adding an input

1. **Parse without executing.** Orchestrator files are read with `ast` (see
   `konstruct/inputs/airflow.py`, `konstruct/inputs/dagster.py`); file formats with a safe parser
   reporting `path:line: message` problems (see `konstruct/semantics/definitions.py`). Warehouses
   read metadata only (see `konstruct/inputs/warehouse.py`; cloud drivers are optional extras
   raising `MissingDriver`).
2. **Discovery and configuration** in `konstruct/engine/config.py`: a `discover_*_files` function
   and an optional `[section] paths` setting, also listed by `konstruct status` and `konstruct init`
   (`konstruct/interfaces/status.py`).
3. **The build** in `konstruct/engine/build.py`: a `load_*` step in `Builder.run`, cached by content
   hash through `Cache.get_or_compute`, warnings through `self.graph.warn`, an entry in
   `self.sources` and a count in `self.inputs`. New node or edge kinds go in `NodeKind` and
   `EdgeKind` in `konstruct/core/graph.py`, with their meaning in its module docstring.
4. **Tests**: parser unit tests with fixtures, a build test, and parity with the tool itself where
   possible (as `tests/test_airflow_parity.py` and the Dagster parity test do), in its own CI job if
   the tool is heavy.
5. **Surfaces**: CLI and MCP if it adds a question, the viewer if it adds something to draw, and
   `docs/guide/inputs.md` plus the README's inputs table.
