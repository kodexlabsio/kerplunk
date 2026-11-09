//! Property tests: each index against a brute-force reading of the rules it implements, on
//! generated graphs and names.

use std::collections::VecDeque;

use _core::graph::{EdgeKind, EdgeSpec, Flow, GraphIndex, Visit};
use _core::names::{Relation, RelationNames};
use _core::search::{Document, SearchIndex, Standing};
use proptest::prelude::*;

const KINDS: [&str; 6] = [
    "contains",
    "feeds",
    "derived_from",
    "runs",
    "precedes",
    "tests",
];

#[derive(Clone, Debug)]
struct Generated {
    nodes: Vec<String>,
    /// Source, target, kind and whether it is a reference edge. Endpoints past the node list
    /// are missing nodes.
    edges: Vec<(usize, usize, usize, bool)>,
}

fn graphs() -> impl Strategy<Value = Generated> {
    (1usize..24).prop_flat_map(|count| {
        let edges = prop::collection::vec(
            (0..count + 3, 0..count + 3, 0..KINDS.len(), any::<bool>()),
            0..60,
        );
        (Just(count), edges).prop_map(|(count, edges)| Generated {
            // Ids that sort differently from their order, so tie-breaks by id are exercised.
            nodes: (0..count).map(|i| format!("n{}", (i * 7) % 31)).collect(),
            edges: edges.into_iter().filter(|(s, t, _, _)| s != t).collect(),
        })
    })
}

fn id(graph: &Generated, ordinal: usize) -> String {
    graph
        .nodes
        .get(ordinal)
        .cloned()
        .unwrap_or_else(|| format!("missing{ordinal}"))
}

fn build(graph: &Generated) -> (GraphIndex, Vec<String>, Vec<String>) {
    let sources: Vec<String> = graph.edges.iter().map(|e| id(graph, e.0)).collect();
    let targets: Vec<String> = graph.edges.iter().map(|e| id(graph, e.1)).collect();
    let specs: Vec<EdgeSpec<'_>> = graph
        .edges
        .iter()
        .enumerate()
        .map(|(i, e)| EdgeSpec {
            source: &sources[i],
            target: &targets[i],
            kind: EdgeKind::parse(KINDS[e.2]),
            reference: e.3,
        })
        .collect();
    let index = GraphIndex::new(graph.nodes.iter().map(String::as_str), &specs);
    (index, sources, targets)
}

/// The neighbours of `node` by scanning every edge, in the order the Python graph yields them.
fn scan(
    graph: &Generated,
    sources: &[String],
    targets: &[String],
    node: &str,
    flow: Flow,
) -> Vec<(usize, String)> {
    let kind = |e: usize| KINDS[graph.edges[e].2];
    let all = 0..graph.edges.len();
    let out = |wanted: &[&str]| -> Vec<(usize, String)> {
        all.clone()
            .filter(|&e| sources[e] == node && wanted.contains(&kind(e)))
            .map(|e| (e, targets[e].clone()))
            .collect()
    };
    let into = |wanted: &[&str]| -> Vec<(usize, String)> {
        all.clone()
            .filter(|&e| targets[e] == node && wanted.contains(&kind(e)))
            .map(|e| (e, sources[e].clone()))
            .collect()
    };
    let mut found = Vec::new();
    if matches!(flow, Flow::Downstream | Flow::Either) {
        found.extend(out(&["feeds"]));
        found.extend(into(&["derived_from", "tests", "runs"]));
    }
    if matches!(flow, Flow::Upstream | Flow::Either) {
        found.extend(into(&["feeds", "runs"]));
        found.extend(out(&["derived_from"]));
    }
    if flow == Flow::Either {
        found.extend(out(&["contains", "precedes"]));
        found.extend(into(&["contains", "precedes"]));
    }
    found
}

/// A breadth-first walk over the scanned neighbours: (id, depth, edge, previous id).
type Step = (String, u32, Option<usize>, Option<String>);

fn brute_walk(
    graph: &Generated,
    starts: &[usize],
    flow: Flow,
    max_depth: Option<i64>,
    stop: bool,
) -> Vec<Step> {
    let (_, sources, targets) = build(graph);
    let mut steps: Vec<Step> = Vec::new();
    let mut queue = VecDeque::new();
    for &s in starts {
        let node = id(graph, s);
        if graph.nodes.contains(&node) && !steps.iter().any(|st| st.0 == node) {
            steps.push((node.clone(), 0, None, None));
            queue.push_back(steps.len() - 1);
        }
    }
    while let Some(at) = queue.pop_front() {
        let (node, depth, via, _) = steps[at].clone();
        if max_depth.is_some_and(|m| i64::from(depth) >= m)
            || (stop && via.is_some_and(|e| graph.edges[e].3))
        {
            continue;
        }
        for (edge, other) in scan(graph, &sources, &targets, &node, flow) {
            if steps.iter().any(|st| st.0 == other) || !graph.nodes.contains(&other) {
                continue;
            }
            steps.push((other, depth + 1, Some(edge), Some(node.clone())));
            if !matches!(KINDS[graph.edges[edge].2], "tests" | "runs") {
                queue.push_back(steps.len() - 1);
            }
        }
    }
    steps
}

fn brute_shortest(graph: &Generated, start: &str, end: &str, flow: Flow) -> Vec<Step> {
    let (_, sources, targets) = build(graph);
    if start == end {
        return vec![(start.to_string(), 0, None, None)];
    }
    let mut steps: Vec<Step> = vec![(start.to_string(), 0, None, None)];
    let mut queue = VecDeque::from([0usize]);
    while let Some(at) = queue.pop_front() {
        let (node, depth, _, _) = steps[at].clone();
        let mut around = scan(graph, &sources, &targets, &node, flow);
        around.sort_by(|a, b| {
            (&a.1, EdgeKind::parse(KINDS[graph.edges[a.0].2]))
                .cmp(&(&b.1, EdgeKind::parse(KINDS[graph.edges[b.0].2])))
        });
        for (edge, other) in around {
            if steps.iter().any(|st| st.0 == other) || !graph.nodes.contains(&other) {
                continue;
            }
            steps.push((other.clone(), depth + 1, Some(edge), Some(node.clone())));
            if other == end {
                let mut chain = vec![steps.last().cloned().unwrap()];
                while let Some(previous) = chain.last().and_then(|s| s.3.clone()) {
                    chain.push(steps.iter().find(|s| s.0 == previous).cloned().unwrap());
                }
                chain.reverse();
                return chain;
            }
            queue.push_back(steps.len() - 1);
        }
    }
    Vec::new()
}

fn named(graph: &Generated, visits: &[Visit]) -> Vec<Step> {
    let name = |o: u32| graph.nodes[o as usize].clone();
    visits
        .iter()
        .map(|v| {
            (
                name(v.node),
                v.depth,
                v.via.map(|e| e as usize),
                v.previous.map(name),
            )
        })
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(400))]

    #[test]
    fn walks_match_a_brute_force_walk(
        graph in graphs(),
        starts in prop::collection::vec(0usize..30, 1..4),
        downstream in any::<bool>(),
        max_depth in prop::option::of(-1i64..5),
        stop in any::<bool>(),
    ) {
        let (index, _, _) = build(&graph);
        let flow = if downstream { Flow::Downstream } else { Flow::Upstream };
        let ordinals: Vec<u32> = starts.iter().filter_map(|&s| index.node(&id(&graph, s))).collect();
        let walk = index.walk(&ordinals, flow, max_depth, stop);
        prop_assert_eq!(named(&graph, &walk), brute_walk(&graph, &starts, flow, max_depth, stop));
    }

    #[test]
    fn neighbours_match_a_scan_of_every_edge(graph in graphs(), node in 0usize..24, flow in 0usize..3) {
        let (index, sources, targets) = build(&graph);
        let flow = [Flow::Downstream, Flow::Upstream, Flow::Either][flow];
        let Some(name) = graph.nodes.get(node) else { return Ok(()); };
        let expected: Vec<usize> = scan(&graph, &sources, &targets, name, flow).into_iter().map(|(e, _)| e).collect();
        let found: Vec<usize> = index.neighbours(index.node(name).unwrap(), flow).map(|(e, _)| e as usize).collect();
        prop_assert_eq!(found, expected);
    }

    #[test]
    fn paths_match_a_brute_force_search(graph in graphs(), start in 0usize..24, end in 0usize..24) {
        let (index, _, _) = build(&graph);
        let (Some(a), Some(b)) = (graph.nodes.get(start), graph.nodes.get(end)) else { return Ok(()); };
        let expected = [Flow::Downstream, Flow::Upstream, Flow::Either]
            .into_iter()
            .map(|flow| brute_shortest(&graph, a, b, flow))
            .find(|p| !p.is_empty())
            .unwrap_or_default();
        let path = index.path(index.node(a).unwrap(), index.node(b).unwrap());
        prop_assert_eq!(named(&graph, &path), expected);
    }
}

fn relation_names() -> impl Strategy<Value = Vec<(String, String, Option<String>, bool)>> {
    let part = prop::sample::select(vec!["a", "b", "orders", "main", "x.y", ""]);
    let name = prop::collection::vec(part, 1..4).prop_map(|parts| parts.join("."));
    prop::collection::vec(
        (
            name.clone(),
            name.clone(),
            prop::option::of(name),
            any::<bool>(),
        ),
        0..12,
    )
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(400))]

    #[test]
    fn relation_names_match_a_scan_of_every_relation(relations in relation_names(), needle in "[abxy.]{0,6}|orders|main.orders") {
        let index = RelationNames::new(relations.iter().map(|(name, id, relation, outside)| Relation {
            name, id, relation: relation.as_deref(), outside_warehouse: *outside,
        }));
        let matches: Vec<u32> = relations.iter().enumerate().filter(|(_, (name, id, relation, outside))| {
            let mut keys = vec![name.clone(), id.clone()];
            if *outside {
                return keys.contains(&needle);
            }
            if let Some(key) = relation.as_ref().filter(|k| !k.is_empty()) {
                keys.push(key.clone());
            }
            keys.iter().any(|k| *k == needle || k.ends_with(&format!(".{needle}")))
        }).map(|(i, _)| i as u32).collect();
        let exact: Vec<u32> = matches.iter().copied().filter(|&i| {
            let (name, _, relation, _) = &relations[i as usize];
            *name == needle || relation.as_deref() == Some(needle.as_str())
        }).collect();
        let expected = if exact.len() == 1 || (!exact.is_empty() && matches.len() > exact.len()) { exact } else { matches };
        prop_assert_eq!(index.find(&needle), expected);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(200))]

    #[test]
    fn search_results_are_ranked_limited_and_filtered(
        labels in prop::collection::vec("[a-c]{1,3}(\\.[a-c]{1,3})?", 1..30),
        query in "[a-c ]{1,8}",
        limit in prop::option::of(0usize..10),
        only_columns in any::<bool>(),
    ) {
        let ids: Vec<String> = (0..labels.len()).map(|i| format!("id{i}")).collect();
        let kinds = ["column", "model", "term", "test"];
        let documents = labels.iter().enumerate().map(|(i, label)| Document {
            id: &ids[i],
            kind: kinds[i % 4],
            standing: [Standing::Other, Standing::Relation, Standing::Term, Standing::Test][i % 4],
            name: label.rsplit('.').next().unwrap(),
            label,
            names: label,
            description: "",
            synonyms: Vec::new(),
        });
        let index = SearchIndex::new(documents);
        let wanted: &[&str] = if only_columns { &["column"] } else { &[] };
        let all = index.search(&query, query.trim(), None, wanted);
        let some = index.search(&query, query.trim(), limit, wanted);
        prop_assert_eq!(&some[..], &all[..limit.unwrap_or(all.len()).min(all.len())]);
        for pair in all.windows(2) {
            let (a, b) = (&pair[0], &pair[1]);
            prop_assert!(a.1 > b.1 || (a.1 == b.1 && (ids[a.0 as usize].len(), &ids[a.0 as usize]) < (ids[b.0 as usize].len(), &ids[b.0 as usize])));
        }
        for (ordinal, score) in &all {
            prop_assert!(*score > 0.0);
            prop_assert!(!only_columns || ordinal % 4 == 0);
            // Every result shares a word with the query, or holds one in its label.
            let label = &labels[*ordinal as usize];
            prop_assert!(query.split(' ').filter(|w| !w.is_empty()).any(|w| label.contains(w)));
        }
    }
}
