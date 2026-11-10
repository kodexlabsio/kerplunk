# Performance

On generated dbt projects (`.kodex/scripts/benchmark.py`): models in layers, each joining two earlier models through CTEs, 20 columns each, with a manifest, a catalog and Airflow DAGs. Measured on a four-core Intel Xeon cloud machine at 2.1 GHz with Python 3.12 and four worker processes, where SQL is parsed and printed by sqlglot's compiled build. A cold build parses every model and writes every output, `graph.html` included with every column's composed formula. `--update` reuses the analysis of anything unchanged, and after an edit it recomputes only the formulas downstream of the change.

| Models | Columns | Column edges | Cold build | `--update`, no change | `--update`, one model edited | impact | trace | explain | graph.html |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 2,200 | 2,540 | 1.32 s | 0.24 s | 0.17 s | 0.2 ms | 0.1 ms | 0.8 ms | 1.4 MB |
| 500 | 10,200 | 12,940 | 5.44 s | 1.33 s | 1.11 s | 0.4 ms | 0.1 ms | 1.0 ms | 6.7 MB |
| 1,000 | 20,200 | 25,940 | 9.61 s | 3.0 s | 2.58 s | 1.4 ms | 0.1 ms | 1.4 ms | 13.4 MB |
| 2,000 | 40,200 | 51,940 | 20.72 s | 7.85 s | 5.94 s | 1.3 ms | 0.1 ms | 1.3 ms | 27.1 MB |

A cold build analyses dbt models in worker processes, one per CPU up to eight, a level of the DAG at a time, with the same results as analysing them one by one; `KONSTRUCT_WORKERS` sets the number, and `KONSTRUCT_WORKERS=1` keeps the analysis in the build's own process. Fewer than 64 models ready to analyse at once, as in small projects and most `--update` builds, are analysed in the build's own process. Query times are medians over five runs on the deepest model. In headless Chromium, the 1,000-model `graph.html` shows its overview in about a second and draws its full lineage graph in about 2.5 seconds; the 2,000-model page takes about 2.3 and 5.7 seconds. The benchmark measures both when Playwright's Chromium is installed. Run `uv run .kodex/scripts/benchmark.py 300 3000` to measure other sizes on your machine. `uv run .kodex/scripts/map_benchmark.py --gpu` measures the map page on the same 2,000-model project: its layout, the time to its first frame, its frame time and the memory it holds.

Plugins cost nothing in commands that cannot run one: only `build`, `export`, `gate`, `hook`, `mcp`, `status` and `plugins` (and a query command that builds a missing graph) read the installed distributions' entry points, once for each repository a build reads. With 170 distributions installed, reading them takes about 40 ms and building the registry from them (`Plugins.discover()`) about 85 ms, and a plugin is imported only when its group is first used. `uv run .kodex/scripts/plugin_startup.py` measures it on your machine.

## The Coding Agent Hook

`konstruct hook post-edit`, which the Claude Code plugin runs after an agent edits SQL or YAML, runs `konstruct build --update` into a copy of the graph, from files alone, and then the gate, so it takes about as long as an `--update` build of the project. An edit to a file the plugin does not watch starts no process, and an edit to a YAML file outside the dbt project's resource paths starts no build. `.kodex/scripts/benchmark.py` reports the hook's time beside the other measurements.

## Questions After the Graph Is Loaded

On the 2,000-model project, after the graph is loaded:

| Question | Time |
|---|---:|
| `path` from a column of the 11th model to one of the last | 4.0 ms |
| `describe` of a model | 0.7 ms |
| `conflicts` over every candidate column | 3.2 s |

The first question after Konstruct loads a graph also builds the index it needs: about 50 ms for walks and paths on 2,000 models, and the search index for `search` and `query`. Loading `graph.json` itself takes about 1.3 s at that size, so a single CLI command spends most of its time reading the graph; the MCP server, the Python API and `konstruct diff` and `conflicts`, which ask many questions of one graph, gain the most.

## Search by Meaning

A build makes no index for search by meaning; the first search by meaning does, and saves it as `vectors.bin` (see [Searching by Meaning](guide/search.md)). On the 2,000-model project:

| Step | Time |
|---|---:|
| Making the index, on a loaded graph with no `vectors.bin` | 4.6 s of CPU time, on up to eight threads |
| `konstruct search --meaning revenue` in a fresh process, reading `vectors.bin` | 1.9 s of CPU time, against 1.5 s for `konstruct search revenue` |
| Each search by meaning after the first in a process | 1.6 ms |

`vectors.bin` is 17.8 MB at that size: the index and every node's blocks, so a process whose graph is the one the file was made from reads them rather than computing them.

