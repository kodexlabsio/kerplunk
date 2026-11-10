# Searching by Meaning

Word search finds a column when the question uses its name. Search by meaning also finds it when the name says nothing: a column called `net_sales_amt` answers "revenue", because its name expands (`amt` is amount, sales are revenue), its formula reads columns named for revenue, and the columns upstream of it and the business terms that define it say the same.

```python
from pathlib import Path
from konstruct.analysis import query

out = Path('konstruct-out')
print(query.search(query.load_graph(out), 'revenue', limit=3, mode='semantic', out_dir=out))
```

```text
orders.revenue           table DOUBLE
stg_orders.rev           table                     sql/staging.sql
fct_sales.net_sales_amt  table         by lineage  sql/marts.sql
```

Here `raw.orders.revenue` is renamed `rev` in staging, and the mart computes `rev - refund_amt_gbp as net_sales_amt`: word search finds the first two, and only search by meaning the third.

Each result has a `score` and, in `to_dict()`, `matched`: what a match by meaning rests on most.

| `matched` | The match comes from |
|-----------|----------------------|
| `words` | The node's own name, synonyms or description, read with the abbreviations and synonyms of data work: `qty` is quantity, `gbp` and sterling are pounds, sales and turnover are revenue, a cancellation is churn, a surname is a last name. Revenue is also an amount, more faintly, so in a project that names nothing revenue, a question about revenue finds its amounts and totals |
| `lineage` | What its neighbours are called: the columns upstream and downstream of it, its table, and above all the business terms bound to it |
| `formula` | Its formula, composed through the columns upstream: the functions, operators and constants it uses and the columns it reads. A column that only copies or renames another computes nothing, so it never matches by formula |
| `structure` | Its place in the lineage: built from the same sources by the same path (in `similar` only) |

A node is found only through a word, concept or formula token it shares with the question, never through shared letters alone, and words the project never uses are left out of the question: a search for something the project does not hold finds nothing.

Every kind of node can be found: columns, models, tables, sources, tasks, tests, dashboards and business terms. `kinds=['column']` keeps only some kinds.

## Exact Names First

Search by meaning is fused with word search, so it never loses what word search finds: a node named exactly what was asked for comes first, and a node both searches rank highly comes before one only one of them finds. A question that reads as a formula, such as `price * quantity` or `coalesce(discount, 0)`, is answered by its formula: the columns that compute it, not the columns it names.

## Similar Columns

`semantic.similar(graph, 'fct_sales.line_total')` lists the nodes most like one node: named alike, near it in the lineage, computed by the same formula, or built from the same sources by the same path. `similar` is a way to find the other copies of a measure.

## The Index

The first search by meaning makes the index and saves it as `konstruct-out/vectors.bin` beside `graph.json`, so a build spends nothing on it. The file holds the index and every node's blocks, with a digest of the graph they were computed from: a later search on the same graph, in any process, reads them rather than computing them. After a build changes the graph, the first search computes the blocks again, reinserts only the nodes whose meaning changed (a renamed column, and the columns around it), and saves the file again; a file from another version of Konstruct is replaced. When the output directory cannot be written to, the index is made in memory each time.

Everything is computed locally from the graph: no model is downloaded, no service is called and nothing leaves the machine. The same graph gives the same results on every machine.
