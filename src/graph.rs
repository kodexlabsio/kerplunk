//! A compact, read-only index of a graph's edges: breadth-first walks and shortest paths.
//!
//! Nodes and edge endpoints are interned to `u32` ordinals and the edges stored in compressed
//! sparse row form, outgoing and incoming, each in insertion order. Every traversal visits
//! neighbours in the order the Python graph's adjacency lists hold them, so walks and paths
//! report exactly what `konstruct.core.graph.Graph` documents.
//!
//! Edge directions follow `konstruct/core/graph.py`: `feeds` and `contains` run with data flow,
//! `derived_from`, `tests` and `runs` against it.

use std::collections::VecDeque;
use std::sync::OnceLock;

use crate::fnv::FnvHashMap;

/// The kind of an edge. The variants are in the alphabetical order of their names, so comparing
/// kinds compares the names, as Python does when it sorts by them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum EdgeKind {
    Contains,
    Defines,
    DerivedFrom,
    Feeds,
    Precedes,
    RelatesTo,
    Runs,
    Tests,
    /// A kind no traversal follows.
    Other,
}

impl EdgeKind {
    /// The kind named `name`, as `graph.json` writes it.
    pub fn parse(name: &str) -> Self {
        match name {
            "contains" => Self::Contains,
            "defines" => Self::Defines,
            "derived_from" => Self::DerivedFrom,
            "feeds" => Self::Feeds,
            "precedes" => Self::Precedes,
            "relates_to" => Self::RelatesTo,
            "runs" => Self::Runs,
            "tests" => Self::Tests,
            _ => Self::Other,
        }
    }
}

/// Which edges a traversal follows from a node.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flow {
    /// Where a change propagates: `feeds` forwards; `derived_from`, `tests` and `runs` backwards.
    Downstream,
    /// What a node depends on: `feeds` and `runs` backwards; `derived_from` forwards.
    Upstream,
    /// Both of those, then `contains` and `precedes` either way.
    Either,
}

/// One edge, by the ordinals of its endpoints.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Edge {
    pub source: u32,
    pub target: u32,
    pub kind: EdgeKind,
    /// Whether the edge joins, filters or groups (`role = "reference"`).
    pub reference: bool,
}

/// A node reached by a traversal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Visit {
    /// The node's ordinal.
    pub node: u32,
    /// Steps from the start.
    pub depth: u32,
    /// The ordinal of the edge that reached it; `None` for a start.
    pub via: Option<u32>,
    /// The node it was reached from; `None` for a start.
    pub previous: Option<u32>,
}

impl Visit {
    fn start(node: u32) -> Self {
        Self {
            node,
            depth: 0,
            via: None,
            previous: None,
        }
    }
}

/// Edges in compressed sparse row form: the edge ordinals of node `n` are
/// `edges[offsets[n]..offsets[n + 1]]`, in insertion order.
#[derive(Debug, Default)]
struct Adjacency {
    offsets: Vec<u32>,
    edges: Vec<u32>,
}

impl Adjacency {
    fn new(count: usize, edges: &[Edge], endpoint: impl Fn(&Edge) -> u32) -> Self {
        let mut offsets = vec![0u32; count + 1];
        for edge in edges {
            offsets[endpoint(edge) as usize + 1] += 1;
        }
        for index in 0..count {
            offsets[index + 1] += offsets[index];
        }
        let mut next: Vec<u32> = offsets[..count].to_vec();
        let mut list = vec![0u32; edges.len()];
        // Edges are placed in ordinal order, so each node's slice keeps insertion order.
        for (ordinal, edge) in edges.iter().enumerate() {
            let slot = &mut next[endpoint(edge) as usize];
            list[*slot as usize] = ordinal as u32;
            *slot += 1;
        }
        Self {
            offsets,
            edges: list,
        }
    }

    fn of(&self, node: u32) -> &[u32] {
        let node = node as usize;
        &self.edges[self.offsets[node] as usize..self.offsets[node + 1] as usize]
    }
}

/// Ids numbered in the order first seen.
#[derive(Debug, Default)]
struct Interner {
    ids: Vec<Box<str>>,
    ordinals: FnvHashMap<Box<str>, u32>,
}

impl Interner {
    fn intern(&mut self, id: &str) -> u32 {
        if let Some(&ordinal) = self.ordinals.get(id) {
            return ordinal;
        }
        let ordinal = self.ids.len() as u32;
        self.ids.push(id.into());
        self.ordinals.insert(id.into(), ordinal);
        ordinal
    }
}

/// The edges of a graph, indexed for traversal.
#[derive(Debug)]
pub struct GraphIndex {
    /// Ordinals below this are the graph's nodes; the rest are endpoints of edges whose node is
    /// missing, which traversals skip.
    nodes: usize,
    ids: Vec<Box<str>>,
    ordinals: FnvHashMap<Box<str>, u32>,
    edges: Vec<Edge>,
    outgoing: Adjacency,
    incoming: Adjacency,
    /// Each ordinal's position when the ids are sorted, built on the first path search.
    rank: OnceLock<Vec<u32>>,
}

/// Builds a [`GraphIndex`]: the graph's nodes first, in its order, then its edges, in theirs.
#[derive(Debug, Default)]
pub struct GraphIndexBuilder {
    interner: Interner,
    /// The number of nodes, fixed by the first edge.
    nodes: Option<usize>,
    edges: Vec<Edge>,
}

impl GraphIndexBuilder {
    /// Add the node `id`.
    ///
    /// # Panics
    ///
    /// When an edge has been added already: ordinals below the node count must be the nodes.
    pub fn node(&mut self, id: &str) {
        assert!(
            self.nodes.is_none(),
            "every node is added before the first edge"
        );
        self.interner.intern(id);
    }

    /// Add an edge from `source` to `target`; `reference` when it joins, filters or groups.
    pub fn edge(&mut self, source: &str, target: &str, kind: EdgeKind, reference: bool) {
        self.nodes.get_or_insert(self.interner.ids.len());
        let source = self.interner.intern(source);
        let target = self.interner.intern(target);
        self.edges.push(Edge {
            source,
            target,
            kind,
            reference,
        });
    }

    pub fn build(self) -> GraphIndex {
        let count = self.interner.ids.len();
        GraphIndex {
            nodes: self.nodes.unwrap_or(count),
            outgoing: Adjacency::new(count, &self.edges, |e| e.source),
            incoming: Adjacency::new(count, &self.edges, |e| e.target),
            ids: self.interner.ids,
            ordinals: self.interner.ordinals,
            edges: self.edges,
            rank: OnceLock::new(),
        }
    }
}

impl GraphIndex {
    pub fn builder() -> GraphIndexBuilder {
        GraphIndexBuilder::default()
    }

    /// The number of nodes.
    pub fn node_count(&self) -> usize {
        self.nodes
    }

    /// The ordinal of the node `id`, or `None` when the graph has no such node.
    pub fn node(&self, id: &str) -> Option<u32> {
        self.ordinals.get(id).copied().filter(|&o| self.is_node(o))
    }

    fn is_node(&self, ordinal: u32) -> bool {
        (ordinal as usize) < self.nodes
    }

    /// The edges `flow` follows from `node`, each with the node it reaches, in the order the
    /// Python graph yields them: downstream, then upstream, then structure.
    pub fn neighbours(&self, node: u32, flow: Flow) -> impl Iterator<Item = (u32, u32)> + '_ {
        let down = matches!(flow, Flow::Downstream | Flow::Either);
        let up = matches!(flow, Flow::Upstream | Flow::Either);
        let structure = flow == Flow::Either;
        let out = self.outgoing.of(node);
        let into = self.incoming.of(node);
        let forward = move |wanted: fn(EdgeKind) -> bool, on: bool| {
            out.iter()
                .filter(move |&&e| on && wanted(self.edges[e as usize].kind))
                .map(move |&e| (e, self.edges[e as usize].target))
        };
        let backward = move |wanted: fn(EdgeKind) -> bool, on: bool| {
            into.iter()
                .filter(move |&&e| on && wanted(self.edges[e as usize].kind))
                .map(move |&e| (e, self.edges[e as usize].source))
        };
        forward(|k| k == EdgeKind::Feeds, down)
            .chain(backward(
                |k| matches!(k, EdgeKind::DerivedFrom | EdgeKind::Tests | EdgeKind::Runs),
                down,
            ))
            .chain(backward(
                |k| matches!(k, EdgeKind::Feeds | EdgeKind::Runs),
                up,
            ))
            .chain(forward(|k| k == EdgeKind::DerivedFrom, up))
            .chain(forward(
                |k| matches!(k, EdgeKind::Contains | EdgeKind::Precedes),
                structure,
            ))
            .chain(backward(
                |k| matches!(k, EdgeKind::Contains | EdgeKind::Precedes),
                structure,
            ))
    }

    /// The edge with ordinal `edge`.
    pub fn edge(&self, edge: u32) -> &Edge {
        &self.edges[edge as usize]
    }

    /// Every node reachable from `starts` along `flow` (`Downstream` or `Upstream`), breadth
    /// first, each once, in the order reached.
    ///
    /// The walk stops after `max_depth` steps when one is given. Nodes reached through `tests`
    /// and `runs` edges are reported but not walked through, and with `stop_at_reference` nor
    /// are nodes reached through a reference edge (a join, filter or group-by).
    pub fn walk(
        &self,
        starts: &[u32],
        flow: Flow,
        max_depth: Option<i64>,
        stop_at_reference: bool,
    ) -> Vec<Visit> {
        let mut seen = vec![false; self.ids.len()];
        let mut visits: Vec<Visit> = Vec::new();
        let mut queue: VecDeque<usize> = VecDeque::new();
        for &start in starts {
            if self.is_node(start) && !seen[start as usize] {
                seen[start as usize] = true;
                queue.push_back(visits.len());
                visits.push(Visit::start(start));
            }
        }
        while let Some(position) = queue.pop_front() {
            let current = visits[position];
            if max_depth.is_some_and(|limit| i64::from(current.depth) >= limit) {
                continue;
            }
            if stop_at_reference && current.via.is_some_and(|e| self.edge(e).reference) {
                continue;
            }
            for (edge, other) in self.neighbours(current.node, flow) {
                if seen[other as usize] || !self.is_node(other) {
                    continue;
                }
                seen[other as usize] = true;
                let kind = self.edge(edge).kind;
                if !matches!(kind, EdgeKind::Tests | EdgeKind::Runs) {
                    queue.push_back(visits.len());
                }
                visits.push(Visit {
                    node: other,
                    depth: current.depth + 1,
                    via: Some(edge),
                    previous: Some(current.node),
                });
            }
        }
        visits
    }

    fn rank(&self) -> &[u32] {
        self.rank.get_or_init(|| {
            let mut order: Vec<u32> = (0..self.ids.len() as u32).collect();
            // UTF-8 byte order is code point order, which is how Python compares strings.
            order.sort_unstable_by(|&a, &b| self.ids[a as usize].cmp(&self.ids[b as usize]));
            let mut rank = vec![0u32; order.len()];
            for (position, &ordinal) in order.iter().enumerate() {
                rank[ordinal as usize] = position as u32;
            }
            rank
        })
    }

    /// A shortest path from `start` to `end` along `flow`, start first; empty when there is none.
    ///
    /// Of several paths of the same length, the one found takes, at each node, the neighbour
    /// whose id sorts first, then the edge whose kind does: the same path whatever order the
    /// edges were added in.
    pub fn shortest(&self, start: u32, end: u32, flow: Flow) -> Vec<Visit> {
        if !self.is_node(start) || !self.is_node(end) {
            return Vec::new();
        }
        if start == end {
            return vec![Visit::start(start)];
        }
        let rank = self.rank();
        let mut reached: Vec<Option<Visit>> = vec![None; self.ids.len()];
        reached[start as usize] = Some(Visit::start(start));
        let mut queue = VecDeque::from([start]);
        let mut around: Vec<(u32, u32)> = Vec::new();
        while let Some(node) = queue.pop_front() {
            let depth = reached[node as usize].map_or(0, |v| v.depth);
            around.clear();
            around.extend(self.neighbours(node, flow));
            // Stable, so edges to one neighbour of one kind keep their order.
            around.sort_by_key(|&(edge, other)| (rank[other as usize], self.edge(edge).kind));
            for &(edge, other) in &around {
                if reached[other as usize].is_some() || !self.is_node(other) {
                    continue;
                }
                reached[other as usize] = Some(Visit {
                    node: other,
                    depth: depth + 1,
                    via: Some(edge),
                    previous: Some(node),
                });
                if other == end {
                    let mut chain = Vec::new();
                    let mut at = Some(end);
                    while let Some(current) = at.and_then(|n| reached[n as usize]) {
                        chain.push(current);
                        at = current.previous;
                    }
                    chain.reverse();
                    return chain;
                }
                queue.push_back(other);
            }
        }
        Vec::new()
    }

    /// The shortest path from `start` to `end`: downstream if there is one, else upstream, else
    /// in either direction through structure too.
    pub fn path(&self, start: u32, end: u32) -> Vec<Visit> {
        [Flow::Downstream, Flow::Upstream, Flow::Either]
            .into_iter()
            .map(|flow| self.shortest(start, end, flow))
            .find(|found| !found.is_empty())
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn index(nodes: &[&str], edges: &[(&str, &str, &str)]) -> GraphIndex {
        let mut builder = GraphIndex::builder();
        nodes.iter().for_each(|id| builder.node(id));
        for &(source, target, kind) in edges {
            builder.edge(source, target, EdgeKind::parse(kind), false);
        }
        builder.build()
    }

    fn ids(index: &GraphIndex, visits: &[Visit]) -> Vec<String> {
        visits
            .iter()
            .map(|v| index.ids[v.node as usize].to_string())
            .collect()
    }

    #[test]
    #[should_panic(expected = "every node is added before the first edge")]
    fn nodes_come_first() {
        let mut builder = GraphIndex::builder();
        builder.edge("a", "b", EdgeKind::Feeds, false);
        builder.node("c");
    }

    #[test]
    fn kinds_sort_by_name() {
        let mut names = [
            "tests",
            "runs",
            "relates_to",
            "precedes",
            "feeds",
            "derived_from",
            "defines",
            "contains",
        ];
        let mut kinds: Vec<EdgeKind> = names.iter().map(|n| EdgeKind::parse(n)).collect();
        names.sort_unstable();
        kinds.sort_unstable();
        assert_eq!(
            kinds,
            names.iter().map(|n| EdgeKind::parse(n)).collect::<Vec<_>>()
        );
    }

    #[test]
    fn downstream_follows_feeds_forwards_and_lineage_backwards() {
        let graph = index(
            &["raw", "raw#a", "stg", "stg#a", "test"],
            &[
                ("raw", "raw#a", "contains"),
                ("stg", "stg#a", "contains"),
                ("raw", "stg", "feeds"),
                ("stg#a", "raw#a", "derived_from"),
                ("test", "stg#a", "tests"),
            ],
        );
        let start = graph.node("raw#a").unwrap();
        let walk = graph.walk(&[start], Flow::Downstream, None, true);
        assert_eq!(ids(&graph, &walk), ["raw#a", "stg#a", "test"]);
        assert_eq!(walk[2].depth, 2);
        let up = graph.walk(&[graph.node("stg").unwrap()], Flow::Upstream, None, false);
        assert_eq!(ids(&graph, &up), ["stg", "raw"]);
    }

    #[test]
    fn tests_and_runs_are_reported_not_walked() {
        let graph = index(
            &["a", "t", "u"],
            &[("t", "a", "tests"), ("u", "t", "tests")],
        );
        let walk = graph.walk(&[graph.node("a").unwrap()], Flow::Downstream, None, false);
        assert_eq!(ids(&graph, &walk), ["a", "t"]);
    }

    #[test]
    fn reference_edges_stop_the_walk_when_asked() {
        let mut builder = GraphIndex::builder();
        ["a", "b", "c"].iter().for_each(|id| builder.node(id));
        builder.edge("a", "b", EdgeKind::Feeds, true);
        builder.edge("b", "c", EdgeKind::Feeds, false);
        let graph = builder.build();
        let start = [graph.node("a").unwrap()];
        assert_eq!(
            ids(&graph, &graph.walk(&start, Flow::Downstream, None, true)),
            ["a", "b"]
        );
        assert_eq!(
            ids(&graph, &graph.walk(&start, Flow::Downstream, None, false)),
            ["a", "b", "c"]
        );
    }

    #[test]
    fn depth_limits_and_missing_nodes() {
        let graph = index(
            &["a", "b", "c"],
            &[
                ("a", "b", "feeds"),
                ("b", "c", "feeds"),
                ("a", "gone", "feeds"),
            ],
        );
        let start = [graph.node("a").unwrap()];
        assert_eq!(
            ids(
                &graph,
                &graph.walk(&start, Flow::Downstream, Some(1), false)
            ),
            ["a", "b"]
        );
        assert_eq!(
            ids(
                &graph,
                &graph.walk(&start, Flow::Downstream, Some(0), false)
            ),
            ["a"]
        );
        assert_eq!(
            ids(
                &graph,
                &graph.walk(&start, Flow::Downstream, Some(-1), false)
            ),
            ["a"]
        );
        assert_eq!(graph.node("gone"), None);
        assert_eq!(graph.node_count(), 3);
    }

    #[test]
    fn path_prefers_data_flow_then_structure() {
        let graph = index(
            &["r", "r#x", "s", "s#y", "dag", "t1", "t2"],
            &[
                ("r", "r#x", "contains"),
                ("s", "s#y", "contains"),
                ("s#y", "r#x", "derived_from"),
                ("t1", "t2", "precedes"),
            ],
        );
        let node = |id| graph.node(id).unwrap();
        assert_eq!(
            ids(&graph, &graph.path(node("r#x"), node("s#y"))),
            ["r#x", "s#y"]
        );
        assert_eq!(
            ids(&graph, &graph.path(node("s#y"), node("r#x"))),
            ["s#y", "r#x"]
        );
        assert_eq!(
            ids(&graph, &graph.path(node("r"), node("s"))),
            ["r", "r#x", "s#y", "s"]
        );
        assert_eq!(
            ids(&graph, &graph.path(node("t2"), node("t1"))),
            ["t2", "t1"]
        );
        assert!(graph.path(node("dag"), node("r")).is_empty());
        assert_eq!(ids(&graph, &graph.path(node("r"), node("r"))), ["r"]);
    }

    #[test]
    fn path_ties_break_on_id_then_kind() {
        let graph = index(
            &["a", "m", "b", "z"],
            &[
                ("a", "z", "feeds"),
                ("a", "m", "feeds"),
                ("z", "b", "feeds"),
                ("m", "b", "feeds"),
            ],
        );
        let path = graph.path(graph.node("a").unwrap(), graph.node("b").unwrap());
        assert_eq!(ids(&graph, &path), ["a", "m", "b"]);
    }
}
