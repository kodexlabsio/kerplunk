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

## Consumers' Lockfiles in the Gate

In a producer that lists its consumers in `repositories`, the gate checks only the pins on the columns a change alters, and reads no lockfile when it alters none. With a consumer pinning every column of 2,000 SQL tables of six columns each (12,000 pins), checking every pin takes 10.1 s; the gate's check after a change to one table takes 2.0 s, most of it preparing to compose formulas, and after a change that alters no column (documentation, say) it takes no measurable time. `uv run .kodex/scripts/benchmark_consumer_pins.py DIR 2000` measures it on your machine.

## Questions After the Graph Is Loaded

On the 2,000-model project, after the graph is loaded:

| Question | Time |
|---|---:|
| `path` from a column of the 11th model to one of the last | 4.0 ms |
| `describe` of a model | 0.7 ms |

The index walks and paths use comes with the graph as Konstruct reads it, so the first `impact` or `path` is as fast as the next; the first `search` or `query` builds the search index, about 70 ms on 2,000 models. Reading `graph.json` takes about 0.36 s at that size (see [Reading the Graph](#reading-the-graph)), so a single CLI command still spends much of its time reading the graph; the MCP server, the Python API and `konstruct diff` and `conflicts`, which ask many questions of one graph, gain the most.

## Reading the Graph

Every command reads `graph.json` first. Konstruct reads it in two passes: the first finds where every token of the file starts, 64 bytes at a time with the processor's vector instructions, and the second checks the JSON over those positions and builds the index that walks and paths use straight from the file. The nodes become Python objects at once; an edge becomes one only when something reads it, so a command that asks one question makes only the edges its answer reaches. On the 2,000-model project, whose `graph.json` is 29.7 MB (`uv run .kodex/scripts/benchmark.py 2000`):

| Step | Time |
|---|---:|
| Reading `graph.json` (`Graph.read`) | 0.36 s |
| The first impact after reading it | 0.7 ms |
| `konstruct impact m010.c01` in a fresh process | 440 ms of CPU time |
| `konstruct status` in a fresh process | 624 ms of CPU time |
| `konstruct search revenue` in a fresh process | 532 ms of CPU time |

Making every edge a Python object, when a caller asks for all of them (`Graph.edges`, `to_dict`) or changes the graph, takes about 0.2 s more. `cargo bench --bench core` times the two passes without Python on a 45.5 MB `graph.json` of the same shape: finding the tokens takes 35 ms, finding them and checking the JSON 70 ms, and the graph's shape and index from those another 65 ms.

## Writing the Graph

A build writes `graph.json` once, and every command that changes the graph (`konstruct verify`, `konstruct lock`) writes it again. Konstruct writes it straight from the graph, in one pass, with no intermediate copy of the data: strings are escaped and integers turned into digits by routines written for each processor, and floats are the shortest digits that read back. The text is the one the graph has always been written as, byte for byte, whether the graph was built or read from `graph.json`. The file is put in place through a renamed temporary file, so a reader sees the old file or the new one; it is not synced to disk before the rename, so a crash of the machine, not of Konstruct, can lose the new file.

`uv run .kodex/scripts/benchmark.py 2000` times both ways of writing the 30.1 MB `graph.json` of the 2,000-model project, in the second table: writing the graph takes about 0.07 s, and converting it to plain data and encoding that (how it was written before) about 0.16 s. A build writes the file once, so it saves under 0.1 s of a cold build of 14 to 30 s; the viewer's data is still made from the graph's plain data, so a build gains no more than that. `tests/test_scale.py` writes the graph of a 2,000-model project with 15 columns per model (23 MB) and fails if that takes more than 0.6 s.

## Comparing Versions

`konstruct diff` and `konstruct release-notes` analyse each changed model's SQL before and after a change. On the 2,000-model project, with a commit that changes 100 models:

| Command | Time |
|---|---:|
| `Project.diff` between the tag before the change and the change | 18.8 s |
| `Project.release_notes` since that tag | 19.3 s |

`uv run .kodex/scripts/history_benchmark.py` measures both on your machine, and `--models` and `--changed` set the sizes.

## The Ontology's Questions

The generated projects have no keys or measures, so the benchmark gives each model of the 2,000-model project an entity, with a primary key on its first column, and a measure, a `SUM` around its third. The ontology then has 2,000 entities and 2,652 relationships, and `metric` lists 4,000 proposals: a sum and an entity count for each model.

| Question | Time |
|---|---:|
| Deriving the ontology | 0.52 s |
| `metric`, every metric | 1.48 s |
| `metric` of one column | 499 ms |
| `entity` of one model's entity | 30.2 ms |
| `relate` across 11 relationships | 5.3 ms |

The times of `metric`, `entity` and `relate` do not include deriving the ontology. A `konstruct entity`, `metric` or `relate` command derives it and then answers, so it takes both. The Python API and the MCP server keep the ontology they derive and derive it again only when the graph changes: when `graph.json` is reread, a node or an edge is added, or `meta` changes, as `konstruct verify` changes it; a script that edits a node or an edge in place calls `graph.changed()`, as every query's index needs. Their first question, and the first after a change, takes both. A question about one metric reads the SQL only of columns the name could mean, and composes the formula of those it finds.

`uv run .kodex/scripts/benchmark.py 2000` measures them with the rest, and `tests/test_scale.py` checks them on 300 models.

## The World Page

graph.html carries every metric of the world model, defined and proposed, for its [World page](guide/viewer.md#the-world-map): each metric's columns by their ids, since the page has every column's formula already. On the map benchmark's 2,000-model project, of 25 columns a model, with an entity and a measure per model as above, the page draws 2,000 entities, 2,652 relationships and 4,000 metrics:

| | |
|---|---:|
| Listing the metrics for the page, during the build | 1.01 s |
| Size the metrics add to graph.html | 0.66 MiB |
| Laying out the entities | 132 ms |
| First frame, from opening the World page | 480 ms |
| First frame, from loading graph.html | 1.55 s |

The first frames are medians of five loads in headless Chromium, at 1600 by 1000. `uv run .kodex/scripts/map_benchmark.py --world` measures them on your machine, and `--models` sets the size.

## Conflicts and the Gate

`konstruct conflicts` compares every two columns that appear to be one idea. What it compares of each column (its formula composed down to source, the keys of the filters and joins on its route, what depends on it downstream and the aggregate it applies last) is worked out once per column, so each pair costs one comparison of two formulas. The gate's own rule reports only conflicts of a column the change alters, so `konstruct gate`, the MCP `gate` tool and the coding agent hook compare only the pairs with such a column, and their cost grows with the change rather than with the number of pairs. When a plugin's gate check runs, the gate finds every conflict, as the check is given them all.

On a repository of SQL tables with the same five measures, each table reading its own source, so that every two tables conflict on every measure (`uv run .kodex/scripts/benchmark.py --tables 100 400`; the gate's checks are timed after an edit to one measure of one table, given the diff), in CPU time:

| Tables | Conflicts | `conflicts` | Gate checks |
|---:|---:|---:|---:|
| 100 | 24,750 | 0.8 s | 0.09 s |
| 400 | 399,000 | 9.55 s | 0.34 s |

On the 2,000-model project of the first table, `conflicts` over every candidate column takes 0.85 s of wall time after the graph is loaded (`uv run .kodex/scripts/benchmark.py 2000`).

When a sensitive term binds nothing, as a term bound to a column only the warehouse knows does in a build from files alone, the gate builds the graph of the base to compare marks (see [Sensitive Data and Erasure](guide/sensitivity.md#the-gate)), and keeps it in the user's cache directory (`$XDG_CACHE_HOME/konstruct/base`, otherwise `~/.cache/konstruct/base`, `~/Library/Caches/konstruct/base` or `%LOCALAPPDATA%\konstruct\base`), by repository, commit and project, for the gates that follow on the same machine. In CI (`CI` or `GITHUB_ACTIONS` set) the gate keeps nothing and builds the base each time it needs it. On the 2,000-model project with such a term, from files alone and with no change (`uv run .kodex/scripts/gate_base_benchmark.py`):

| Step | Time |
|---|---:|
| Building the graph of the base | 15.2 s |
| The first gate on a base, which builds it | 23.4 s |
| Each gate after it on the same base, which reads the kept graph | 5.5 s |

These measurements are on a four-core Intel Xeon cloud machine at 2.3 GHz with Python 3.12.

## Search by Meaning

A build makes no index for search by meaning; the first search by meaning does, and saves it as `vectors.bin` (see [Searching by Meaning](guide/search.md)). On the 2,000-model project:

| Step | Time |
|---|---:|
| Making the index, on a loaded graph with no `vectors.bin` | 4.6 s of CPU time, on up to eight threads |
| `konstruct search --meaning revenue` in a fresh process, reading `vectors.bin` | 1.9 s of CPU time, against 1.5 s for `konstruct search revenue` |
| Each search by meaning after the first in a process | 1.6 ms |

`vectors.bin` is 17.8 MB at that size: the index and every node's blocks, so a process whose graph is the one the file was made from reads them rather than computing them.

## Finding Files

A build walks the repository once, on every CPU, and each input finds its files in that walk: the directories are read by a pool of threads, put in name order afterwards so the answer does not depend on which thread was first, and matched against suffixes, name patterns and `exclude` patterns as they are found. The record a build keeps of each file it read (size, times, device and inode) and of the folders beside it is made in the same native code. On the 2,000-model project of `tests/test_scale.py` (four cores, a release build), the time spent finding files in an `--update` build fell from about 0.22 s to 0.016 s, the median of three builds, and the whole build from about 3.3 s to 2.8 s; on the 500-model project discovery fell from about 55 ms to 6 ms. `.kodex/scripts/benchmark.py` prints the time to discover every input of each size it builds. The rest of an `--update` build is spent reading dbt's selectors for each Airflow task, building nodes and edges and writing graph.json, and the viewer's formulas.

## Loaders

Recognising which loader landed each table checks a table only against the marks its own columns could satisfy. On 50,000 warehouse relations of twelve columns each, one in ten landed, detection takes about 0.05 s of CPU and the whole landing step about 1 s, most of it reading each relation's columns from the graph (`test_detection_over_fifty_thousand_relations_stays_fast` in `tests/test_loaders.py`). Finding loader configuration files reads each Python, YAML, JSON, Terraform and `.properties` file once and parses only those whose name and text show a known format: about 0.2 s of CPU over 3,000 files (`test_discovery_over_thousands_of_files_stays_cheap` in `tests/test_ingestion.py`).

## Airflow and Dagster Files

Konstruct finds the files that declare Airflow DAGs or Dagster definitions by looking at the bytes of every candidate file for the words that mark them, and reads each file it finds without importing it. `uv run .kodex/scripts/orchestrator_benchmark.py` measures both on a generated repository of 2,000 DAG files, 500 Dagster definition files and 5,000 other files (one in twenty mentions Airflow without defining a DAG), each step the median of five runs; its arguments set the sizes:

| Step | Time |
|---|---:|
| Looking at the 7,500 files to see which hold DAGs or Dagster definitions | 0.05 s |
| Reading the 2,000 DAG files (12,000 tasks) | 0.28 s |
| Reading the 500 Dagster files (1,000 assets) | 0.013 s |

Listing the files, which takes about 0.5 s and is done once for DAG files and once for Dagster files, is the largest part of finding them. The look at a file's bytes runs at about 20 GB/s on a megabyte of source (`cargo bench --bench core scan`).
