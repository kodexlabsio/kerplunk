# Performance

On generated dbt projects (`.kodex/scripts/benchmark.py`): models in layers, each joining two earlier models through CTEs, 20 columns each, with a manifest, a catalog and Airflow DAGs. Measured on a four-core Intel Xeon cloud machine at 2.1 GHz with Python 3.12, where SQL is parsed and printed by sqlglot's compiled build. A cold build parses every model and writes every output, `graph.html` included with every column's composed formula. `--update` reuses the analysis of anything unchanged, and after an edit it recomputes only the formulas downstream of the change.

| Models | Columns | Column edges | Cold build | `--update`, no change | `--update`, one model edited | impact | trace | explain | graph.html |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 2,200 | 2,540 | 1.57 s | 0.18 s | 0.15 s | 0.5 ms | 0.4 ms | 1.4 ms | 1.4 MB |
| 500 | 10,200 | 12,940 | 8.11 s | 1.43 s | 1.29 s | 2.5 ms | 2.0 ms | 3.7 ms | 6.7 MB |
| 1,000 | 20,200 | 25,940 | 16.48 s | 3.1 s | 2.82 s | 5.2 ms | 3.5 ms | 7.2 ms | 13.4 MB |
| 2,000 | 40,200 | 51,940 | 37.23 s | 7.56 s | 6.43 s | 8.9 ms | 6.7 ms | 8.4 ms | 27.1 MB |

Time grows linearly with the size of the project. Query times are medians over five runs on the deepest model. In the browser, the 2,000-model `graph.html` opens in under half a second, and selecting a column isolates its lineage in about 0.2 seconds. Run `uv run .kodex/scripts/benchmark.py 300 3000` to measure other sizes on your machine.
