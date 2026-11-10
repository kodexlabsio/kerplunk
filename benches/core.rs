//! Times the Rust core's indexes on a generated project of 2,000 models with 20 columns each,
//! the size `.kodex/scripts/benchmark.py` measures from Python, without the cost of crossing
//! into Python. Each line is the median of several runs.
//!
//! ```bash
//! cargo bench --bench core
//! ```

use std::hint::black_box;
use std::time::{Duration, Instant};

use _core::graph::{EdgeKind, GraphIndex, NodeKind};
use _core::keys;
use _core::names::{Relation, RelationNames};
use _core::search::{Document, SearchIndex, Standing};
use _core::vector::VectorIndex;
use _core::vector::embed::{Link, Node as VectorNode};
use _core::words::Words;

const MODELS: usize = 2000;
const COLUMNS: usize = 20;
const RUNS: usize = 15;

/// Words that data projects name things with.
const VOCABULARY: &[&str] = &[
    "account",
    "active",
    "address",
    "amount",
    "balance",
    "billing",
    "campaign",
    "cart",
    "channel",
    "city",
    "code",
    "cohort",
    "count",
    "country",
    "created",
    "currency",
    "customer",
    "daily",
    "date",
    "deleted",
    "device",
    "discount",
    "email",
    "event",
    "first",
    "fiscal",
    "gross",
    "invoice",
    "item",
    "last",
    "lifetime",
    "line",
    "margin",
    "monthly",
    "net",
    "order",
    "payment",
    "plan",
    "price",
    "product",
    "quantity",
    "rate",
    "refund",
    "region",
    "revenue",
    "session",
    "shipping",
    "sku",
    "source",
    "status",
    "store",
    "subscription",
    "tax",
    "total",
    "updated",
    "user",
    "value",
    "weekly",
    "id",
    "at",
    "usd",
    "type",
    "name",
    "flag",
    "is",
    "has",
    "pct",
];

/// A small deterministic generator (xorshift64*), so every run measures the same project.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> usize {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        (self.0.wrapping_mul(0x2545_f491_4f6c_dd1d) >> 33) as usize
    }

    fn word(&mut self) -> &'static str {
        VOCABULARY[self.next() % VOCABULARY.len()]
    }

    fn snake(&mut self, words: usize) -> String {
        (0..words)
            .map(|_| self.word())
            .collect::<Vec<_>>()
            .join("_")
    }

    fn sentence(&mut self) -> String {
        let length = 6 + self.next() % 14;
        let mut text = String::from("the");
        for _ in 0..length {
            text.push(' ');
            text.push_str(self.word());
        }
        text.push_str(", in lower case.");
        text
    }
}

/// One node's text, lower case as Python passes it.
struct Node {
    id: String,
    kind: &'static str,
    name: String,
    label: String,
    names: String,
    description: String,
    relation: Option<String>,
    parent: Option<usize>,
}

fn project() -> Vec<Node> {
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    let mut nodes = Vec::new();
    for model in 0..MODELS {
        let layer = ["stg", "int", "fct", "dim", "rpt"][model % 5];
        let name = format!("{layer}_{}_{model:04}", rng.snake(2));
        let relation = format!("analytics.main.{name}");
        let model_ordinal = nodes.len();
        nodes.push(Node {
            id: format!("model.shop.{name}"),
            kind: "model",
            label: name.clone(),
            names: format!("{name} {relation}"),
            description: rng.sentence(),
            relation: Some(relation),
            parent: None,
            name,
        });
        for _ in 0..COLUMNS {
            let words = 1 + rng.next() % 3;
            let column = rng.snake(words);
            let label = format!("{}.{column}", nodes[model_ordinal].name);
            let description = if rng.next().is_multiple_of(3) {
                String::new()
            } else {
                rng.sentence()
            };
            nodes.push(Node {
                id: format!("{}#{column}", nodes[model_ordinal].id),
                kind: "column",
                names: label.clone(),
                label,
                description,
                relation: None,
                parent: Some(model_ordinal),
                name: column,
            });
        }
    }
    nodes
}

fn documents(nodes: &[Node]) -> impl Iterator<Item = Document<'_>> {
    nodes.iter().map(|node| Document {
        id: &node.id,
        kind: node.kind,
        standing: if node.kind == "model" {
            Standing::Relation
        } else {
            Standing::Other
        },
        name: &node.name,
        label: &node.label,
        names: &node.names,
        description: &node.description,
        synonyms: Vec::new(),
    })
}

/// The project's lineage, as tests/test_scale.py lays it out: each model reads two earlier
/// ones, and each column derives from the same column of both.
fn graph(nodes: &[Node]) -> GraphIndex {
    let mut builder = GraphIndex::builder();
    for node in nodes {
        let kind = if node.kind == "column" {
            NodeKind::Column
        } else {
            NodeKind::Other
        };
        builder.node(&node.id, kind);
    }
    let stride = COLUMNS + 1;
    for model in 0..MODELS {
        let at = model * stride;
        for column in 1..stride {
            builder.edge(
                &nodes[at].id,
                &nodes[at + column].id,
                EdgeKind::Contains,
                false,
            );
        }
        if model >= 10 {
            for upstream in [model / 2, model / 3] {
                let from = upstream * stride;
                builder.edge(&nodes[from].id, &nodes[at].id, EdgeKind::Feeds, false);
                for column in 1..stride {
                    builder.edge(
                        &nodes[at + column].id,
                        &nodes[from + column].id,
                        EdgeKind::DerivedFrom,
                        column == 1,
                    );
                }
            }
        }
    }
    for (ordinal, node) in nodes.iter().enumerate() {
        if let Some(parent) = node.parent {
            builder.parent(ordinal as u32, &nodes[parent].id);
        }
    }
    builder.build()
}

/// The project's nodes and lineage as search by meaning reads them.
fn vector_nodes(nodes: &[Node]) -> (Vec<VectorNode<'_>>, Vec<Link>) {
    let vector_nodes = nodes
        .iter()
        .map(|node| VectorNode {
            id: &node.id,
            kind: node.kind,
            name: &node.name,
            qualifier: node
                .parent
                .map_or(node.relation.as_deref().unwrap_or(""), |p| &nodes[p].label),
            description: &node.description,
            synonyms: Vec::new(),
            expression: None,
        })
        .collect();
    let stride = COLUMNS + 1;
    let mut links = Vec::new();
    let link = |source: usize, target: usize, kind| Link {
        source: source as u32,
        target: target as u32,
        kind,
    };
    for model in 0..MODELS {
        let at = model * stride;
        for column in 1..stride {
            links.push(link(at, at + column, EdgeKind::Contains));
        }
        if model >= 10 {
            for upstream in [model / 2, model / 3] {
                let from = upstream * stride;
                links.push(link(from, at, EdgeKind::Feeds));
                for column in 1..stride {
                    links.push(link(at + column, from + column, EdgeKind::DerivedFrom));
                }
            }
        }
    }
    (vector_nodes, links)
}

/// Times `run` unless `filter` (the bench's first argument) names other lines. The fastest run
/// is the one least disturbed by the rest of the machine; the median shows the spread.
fn bench(filter: Option<&str>, what: &str, run: impl FnMut()) {
    bench_runs(filter, what, RUNS, run);
}

/// [`bench`] over `runs` runs, for what takes seconds.
fn bench_runs(filter: Option<&str>, what: &str, runs: usize, mut run: impl FnMut()) {
    if filter.is_some_and(|f| !what.contains(f)) {
        return;
    }
    let mut times: Vec<Duration> = (0..runs)
        .map(|_| {
            let started = Instant::now();
            run();
            started.elapsed()
        })
        .collect();
    times.sort_unstable();
    let micros = |t: Duration| t.as_secs_f64() * 1e6;
    println!(
        "{what:<44} {:>10.1} µs fastest {:>10.1} µs median",
        micros(times[0]),
        micros(times[runs / 2])
    );
}

fn main() {
    let filter = std::env::args().nth(1).filter(|a| !a.starts_with("--"));
    let filter = filter.as_deref();
    let nodes = project();
    let bytes: usize = nodes
        .iter()
        .map(|n| n.names.len() + n.description.len())
        .sum();
    println!(
        "{} nodes, {} bytes of names and descriptions",
        nodes.len(),
        bytes
    );

    bench(filter, "words of every name and description", || {
        let mut words = Words::default();
        let count: usize = nodes
            .iter()
            .map(|n| words.of(&n.names).count() + words.of(&n.description).count())
            .sum();
        black_box(count);
    });
    bench(filter, "keys of every id, name and label", || {
        for node in &nodes {
            black_box(keys::from_str(&node.id));
            black_box(keys::from_str(&node.name));
            black_box(keys::from_str(&node.label));
        }
    });
    bench(filter, "search index", || {
        black_box(SearchIndex::new(documents(&nodes)));
    });
    let index = SearchIndex::new(documents(&nodes));
    for query in [
        "customer lifetime value",
        "order",
        "revenue by region",
        "stg_order",
        "ustom",
        "zzz",
    ] {
        bench(filter, &format!("search {query:?}"), || {
            black_box(index.search(query, query, Some(20), &[]));
        });
    }

    let relations = || {
        nodes
            .iter()
            .filter(|n| n.relation.is_some())
            .map(|n| Relation {
                name: &n.name,
                id: &n.id,
                relation: n.relation.as_deref(),
                outside_warehouse: false,
            })
    };
    bench(filter, "relation names", || {
        black_box(RelationNames::new(relations()));
    });

    bench(filter, "graph index", || {
        black_box(graph(&nodes));
    });
    let lineage = graph(&nodes);
    let column = |model: usize| lineage.node(&nodes[model * (COLUMNS + 1) + 2].id).unwrap();
    bench(filter, "impact of a column of the 11th model", || {
        black_box(lineage.impact(&[column(10)], None, true));
    });
    bench(filter, "trace of a column of the last model", || {
        black_box(lineage.trace(&[column(MODELS - 1)], None, true));
    });
    bench(filter, "path from the 11th model to the last", || {
        black_box(lineage.path(column(10), column(MODELS - 1)));
    });

    let (vector_nodes, links) = vector_nodes(&nodes);
    let standings = vec![0; vector_nodes.len()];
    let threads = std::thread::available_parallelism().map_or(1, |n| n.get());
    bench_runs(filter, "vector index", 3, || {
        black_box(VectorIndex::new(
            &vector_nodes,
            &links,
            &standings,
            None,
            threads,
        ));
    });
    let vectors = VectorIndex::new(&vector_nodes, &links, &standings, None, threads);
    let saved = vectors.to_bytes();
    bench_runs(filter, "vector index, read from vectors.bin", 5, || {
        black_box(VectorIndex::new(
            &vector_nodes,
            &links,
            &standings,
            Some(&saved),
            threads,
        ));
    });
    for query in [
        "customer lifetime value",
        "revenue",
        "refund amount in pounds",
    ] {
        bench(filter, &format!("search by meaning {query:?}"), || {
            black_box(vectors.search(query, 20, &[]));
        });
    }
}
