//! Search by meaning: find a column, model, table, business term or formula by what it is,
//! not only by what it is called.
//!
//! Each node gets a vector composed from its words, its neighbours' words, its formula and its
//! place in the lineage ([`embed`]), with nothing learnt and nothing downloaded: the meaning
//! comes from identifier splitting, a table of the abbreviations and synonyms of data work, and
//! the graph itself. A question is embedded the same way and answered in three steps:
//!
//! 1. **Candidates**: the nearest vectors in a hierarchical navigable small world graph
//!    ([`hnsw`]), and every node whose own words share a whole word or concept with the
//!    question (unless a thousand nodes do).
//! 2. **Scores**: each candidate's exact inner product with the question, block by block from
//!    sparse features, so a node scores only on features it shares with the question, and
//!    hashing collisions cannot promote an unrelated node; weighed by the node's kind, as the
//!    word search weighs it.
//! 3. **Fusion** with the word search, when asked for ([`fuse`]).
//!
//! The index saves to a few bytes per node ([`store`]) and reads back against the graph of the
//! moment: slots whose node changed are removed, new nodes inserted, and the rest kept.

pub mod dot;
pub mod embed;
pub mod formula;
pub mod fuse;
pub mod hnsw;
pub mod store;
pub mod text;

#[cfg(feature = "python")]
pub(crate) mod python;

use crate::fnv::{self, FnvHashMap};
use crate::keys;

use embed::{Basis, Embedding, Link, Node, NodeRows, Question, SKETCH};
use hnsw::{Hnsw, Params, SavedSlot, Scratch};

/// How many candidates the graph search returns, at least.
const EF_SEARCH: usize = 96;
/// A word or concept more nodes than this share is left to the graph search.
const POSTING_LIMIT: usize = 1000;
/// With a kind filter that leaves fewer nodes than this, every one of them is scored.
const EXHAUSTIVE: usize = 4096;
/// The least share of the best possible score a result needs. A result also needs a feature in
/// common with the question that is not a trigram: shared letters alone are not a meaning.
const MINIMUM: f32 = 0.05;
/// The least share of a question's content words a result must mean: a node that answers one
/// word of "employee turnover" but not the other does not answer the question.
const COVERAGE: f32 = 0.6;

/// How a node's kind weighs its score, as `search::Standing` does: 0 other, 1 business term,
/// 2 relation, 3 test.
pub fn prior(standing: u8) -> f32 {
    match standing {
        1 => 1.2,
        2 => 1.1,
        3 => 0.6,
        _ => 1.0,
    }
}

/// One result: a node ordinal, its score and what the match rests on most.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Hit {
    pub ordinal: u32,
    pub score: f32,
    pub basis: Basis,
}

/// What reading a saved index kept, added and removed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Update {
    /// Nodes whose saved slot still held their vector.
    pub kept: usize,
    /// Nodes inserted: new ones, and ones whose vector changed.
    pub added: usize,
    /// Saved slots removed: nodes that are gone, and ones whose vector changed.
    pub removed: usize,
    /// True when a saved index was given and could be read.
    pub reused: bool,
    /// True when every node's blocks were read from the saved index, which was made from the
    /// same graph, rather than computed.
    pub rows_read: bool,
}

/// A node's key: a hash of its id, never zero, which marks a free slot.
pub fn key(id: &str) -> u64 {
    text::mix(fnv::hash(fnv::OFFSET_BASIS, id.as_bytes())) | 1 << 63
}

fn vector_hash(vector: &[i8]) -> u64 {
    let bytes: Vec<u8> = vector.iter().map(|&x| x as u8).collect();
    text::mix(fnv::hash(fnv::OFFSET_BASIS, &bytes))
}

/// Saved rows, by key, in the order of `keys`; None unless each key is one node's and has rows.
fn in_node_order(rows: Vec<(u64, NodeRows)>, keys: &[u64]) -> Option<Vec<NodeRows>> {
    if rows.len() != keys.len() {
        return None;
    }
    let mut by_key: FnvHashMap<u64, NodeRows> = FnvHashMap::default();
    for (key, row) in rows {
        if by_key.insert(key, row).is_some() {
            return None;
        }
    }
    keys.iter().map(|key| by_key.remove(key)).collect()
}

pub struct VectorIndex {
    embedding: Embedding,
    hnsw: Hnsw,
    /// Each slot's node ordinal, `u32::MAX` for a free slot.
    node_of: Vec<u32>,
    kinds: Vec<u16>,
    kind_names: Vec<Box<str>>,
    priors: Vec<f32>,
    keys: Vec<u64>,
    /// Node ids, to break ties the same way whatever order the nodes came in.
    ids: Vec<Box<str>>,
    /// The digest of the graph the blocks come from ([`embed::fingerprint`]).
    fingerprint: u64,
    update: Update,
}

impl std::fmt::Debug for VectorIndex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("VectorIndex")
            .field("nodes", &self.priors.len())
            .field("update", &self.update)
            .finish()
    }
}

impl VectorIndex {
    /// The index of `nodes` and `links`, each node weighed by its `standings` entry (see
    /// [`prior`]). With `previous`, a saved index, its slots are reused where the node's vector
    /// is unchanged, and when it was made from this same graph its blocks are read rather than
    /// computed. `threads` bounds the threads a build uses; the result is the same for any.
    pub fn new(
        nodes: &[Node<'_>],
        links: &[Link],
        standings: &[u8],
        previous: Option<&[u8]>,
        threads: usize,
    ) -> Self {
        assert_eq!(nodes.len(), standings.len());
        let keys: Vec<u64> = nodes.iter().map(|n| key(n.id)).collect();
        let fingerprint = embed::fingerprint(nodes, &keys, links);
        let mut saved = previous.and_then(store::read).filter(|saved| {
            saved.version == embed::VERSION
                && saved.dim == SKETCH
                && saved.params == Params::default()
        });
        let rows = saved
            .as_mut()
            .filter(|saved| saved.fingerprint == fingerprint)
            .and_then(|saved| {
                let rows = in_node_order(std::mem::take(&mut saved.rows), &keys)?;
                Some((rows, std::mem::take(&mut saved.features)))
            });
        let rows_read = rows.is_some();
        let embedding = match rows {
            Some((rows, features)) => Embedding::from_rows(rows, features),
            None => embed::embed(nodes, &keys, links, threads),
        };
        let mut kind_names: Vec<Box<str>> = Vec::new();
        let mut kind_numbers: FnvHashMap<&str, u16> = FnvHashMap::default();
        let kinds = nodes
            .iter()
            .map(|node| {
                *kind_numbers.entry(node.kind).or_insert_with(|| {
                    kind_names.push(node.kind.into());
                    (kind_names.len() - 1) as u16
                })
            })
            .collect();
        let mut index = Self {
            hnsw: Hnsw::new(SKETCH, Params::default()),
            node_of: Vec::new(),
            kinds,
            kind_names,
            priors: standings.iter().map(|&s| prior(s)).collect(),
            keys,
            ids: nodes.iter().map(|n| n.id.into()).collect(),
            fingerprint,
            update: Update::default(),
            embedding,
        };
        index.place(saved, threads);
        index.update.rows_read = rows_read;
        index
    }

    /// The sketch the graph indexes node `node` by.
    fn vector(&self, node: usize) -> &[i8] {
        &self.embedding.sketches[node * SKETCH..(node + 1) * SKETCH]
    }

    /// Fill the graph: from the saved index's slots where they still hold their node's vector,
    /// else from nothing.
    fn place(&mut self, saved: Option<store::Saved>, threads: usize) {
        let mut ordinal_of: FnvHashMap<u64, u32> = FnvHashMap::default();
        // A second node with the same key (one in 2^63) is searched by its words only.
        let mut indexed = vec![false; self.keys.len()];
        for (node, &key) in self.keys.iter().enumerate() {
            if let std::collections::hash_map::Entry::Vacant(slot) = ordinal_of.entry(key) {
                slot.insert(node as u32);
                indexed[node] = true;
            }
        }
        let mut placed = vec![false; self.keys.len()];
        if let Some(saved) = saved {
            let mut stale = Vec::new();
            let vectors: Vec<Option<&[i8]>> = saved
                .slots
                .iter()
                .zip(&saved.hashes)
                .enumerate()
                .map(|(slot, (saved_slot, &hash))| {
                    let node = ordinal_of
                        .get(&saved_slot.key)
                        .copied()
                        .filter(|_| saved_slot.live);
                    match node {
                        Some(node) if vector_hash(self.vector(node as usize)) == hash => {
                            placed[node as usize] = true;
                            Some(self.vector(node as usize))
                        }
                        _ => {
                            if saved_slot.live {
                                stale.push(slot as u32);
                            }
                            None
                        }
                    }
                })
                .collect();
            let slots: Vec<SavedSlot> = saved.slots;
            let mut hnsw = Hnsw::restore(SKETCH, Params::default(), slots, &vectors, saved.entry);
            hnsw.remove(&stale);
            self.update = Update {
                kept: placed.iter().filter(|&&p| p).count(),
                added: 0,
                removed: stale.len(),
                reused: true,
                rows_read: false,
            };
            self.hnsw = hnsw;
        }
        let new: Vec<usize> = (0..self.keys.len())
            .filter(|&n| indexed[n] && !placed[n])
            .collect();
        let sketches = &self.embedding.sketches;
        let items: Vec<(u64, &[i8])> = new
            .iter()
            .map(|&n| (self.keys[n], &sketches[n * SKETCH..(n + 1) * SKETCH]))
            .collect();
        self.hnsw.insert_all(&items, threads);
        self.update.added = new.len();
        self.node_of = (0..self.hnsw.slots() as u32)
            .map(|slot| {
                if self.hnsw.is_live(slot) {
                    ordinal_of[&self.hnsw.key(slot)]
                } else {
                    u32::MAX
                }
            })
            .collect();
    }

    /// Orders nodes as the word search breaks ties: shorter ids first, then by id.
    pub fn order(&self, a: u32, b: u32) -> std::cmp::Ordering {
        let (a, b) = (&self.ids[a as usize], &self.ids[b as usize]);
        keys::length(a).cmp(&keys::length(b)).then_with(|| a.cmp(b))
    }

    /// What reading the saved index kept, added and removed.
    pub fn update(&self) -> Update {
        self.update
    }

    /// How many nodes the index holds.
    pub fn len(&self) -> usize {
        self.priors.len()
    }

    pub fn is_empty(&self) -> bool {
        self.priors.is_empty()
    }

    /// The index as the bytes of a saved index.
    pub fn to_bytes(&self) -> Vec<u8> {
        let slots = self.hnsw.saved();
        let hashes = (0..self.hnsw.slots() as u32)
            .map(|slot| {
                if self.hnsw.is_live(slot) {
                    vector_hash(self.hnsw.vector(slot))
                } else {
                    0
                }
            })
            .collect();
        let mut order: Vec<usize> = (0..self.len()).collect();
        order.sort_unstable_by(|&a, &b| {
            (self.keys[a], &self.ids[a]).cmp(&(self.keys[b], &self.ids[b]))
        });
        store::write(&store::Saved {
            version: embed::VERSION,
            dim: SKETCH,
            params: self.hnsw.params(),
            entry: self.hnsw.entry(),
            slots,
            hashes,
            fingerprint: self.fingerprint,
            features: Vec::new(),
            rows: order
                .into_iter()
                .map(|node| (self.keys[node], self.embedding.rows(node)))
                .collect(),
        })
    }

    /// The kind numbers `names` refer to; None when there is no filter.
    fn kind_filter(&self, names: &[&str]) -> Option<Vec<bool>> {
        if names.is_empty() {
            return None;
        }
        Some(
            self.kind_names
                .iter()
                .map(|kind| names.contains(&&**kind))
                .collect(),
        )
    }

    /// Score candidates exactly and keep the best `limit` above the minimum.
    fn rank(
        &self,
        question: &Question,
        candidates: impl Iterator<Item = u32>,
        allowed: Option<&[bool]>,
        skip: Option<u32>,
        limit: usize,
    ) -> Vec<Hit> {
        let most = embed::most(question);
        if most <= 0.0 {
            return Vec::new();
        }
        let mut seen = vec![false; self.len()];
        let mut hits: Vec<Hit> = Vec::new();
        for node in candidates {
            let n = node as usize;
            if std::mem::replace(&mut seen[n], true)
                || Some(node) == skip
                || allowed.is_some_and(|a| !a[self.kinds[n] as usize])
            {
                continue;
            }
            let (blocks, shared) = embed::parts(question, &self.embedding, n);
            let covered = embed::coverage(question, &self.embedding, n);
            let score = blocks.iter().sum::<f32>() / most * self.priors[n] * covered;
            if shared && covered >= COVERAGE && score >= MINIMUM {
                hits.push(Hit {
                    ordinal: node,
                    score,
                    basis: Basis::of(blocks),
                });
            }
        }
        hits.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then_with(|| self.order(a.ordinal, b.ordinal))
        });
        hits.truncate(limit);
        hits
    }

    /// The candidates for a question: the graph search's, then every node with a whole feature
    /// of the question in its words or formula, or, with a filter that leaves few nodes, all of
    /// those.
    fn candidates(&self, question: &Question, allowed: Option<&[bool]>, ef: usize) -> Vec<u32> {
        if let Some(allowed) = allowed {
            let count = self.kinds.iter().filter(|&&k| allowed[k as usize]).count();
            if count <= EXHAUSTIVE {
                return (0..self.len() as u32)
                    .filter(|&n| allowed[self.kinds[n as usize] as usize])
                    .collect();
            }
        }
        let mut scratch = Scratch::default();
        let mut found: Vec<u32> = self
            .hnsw
            .search(&question.sketch, ef, ef, &mut scratch)
            .into_iter()
            .map(|near| self.node_of[near.slot as usize])
            .collect();
        for key in &question.keys {
            if let Some(nodes) = self.embedding.postings.get(key)
                && nodes.len() <= POSTING_LIMIT
            {
                found.extend(nodes);
            }
        }
        found
    }

    /// The nodes that best answer `text`, best first, at most `limit`; with `kinds`, only
    /// nodes of those kinds.
    pub fn search(&self, text: &str, limit: usize, kinds: &[&str]) -> Vec<Hit> {
        let question = embed::question(&self.embedding, text);
        let allowed = self.kind_filter(kinds);
        let ef = EF_SEARCH.max(2 * limit);
        let candidates = self.candidates(&question, allowed.as_deref(), ef);
        self.rank(
            &question,
            candidates.into_iter(),
            allowed.as_deref(),
            None,
            limit,
        )
    }

    /// The nodes most like node `ordinal`, best first, at most `limit`, excluding itself.
    pub fn similar(&self, ordinal: u32, limit: usize, kinds: &[&str]) -> Vec<Hit> {
        if ordinal as usize >= self.len() {
            return Vec::new();
        }
        let question = embed::like(&self.embedding, ordinal as usize);
        let allowed = self.kind_filter(kinds);
        let ef = EF_SEARCH.max(2 * limit + 1);
        let candidates = self.candidates(&question, allowed.as_deref(), ef);
        self.rank(
            &question,
            candidates.into_iter(),
            allowed.as_deref(),
            Some(ordinal),
            limit,
        )
    }

    /// Every node scored against `text` with no index, for tests that check the index finds
    /// what a full scan does.
    pub fn scan(&self, text: &str, limit: usize) -> Vec<Hit> {
        let question = embed::question(&self.embedding, text);
        self.rank(&question, 0..self.len() as u32, None, None, limit)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::EdgeKind;

    pub(super) struct Project {
        ids: Vec<String>,
        names: Vec<String>,
        qualifiers: Vec<String>,
        expressions: Vec<Option<String>>,
        pub(super) links: Vec<Link>,
    }

    impl Project {
        pub(super) fn nodes(&self) -> Vec<Node<'_>> {
            (0..self.ids.len())
                .map(|i| Node {
                    id: &self.ids[i],
                    kind: if self.qualifiers[i].is_empty() {
                        "model"
                    } else {
                        "column"
                    },
                    name: &self.names[i],
                    qualifier: &self.qualifiers[i],
                    description: "",
                    synonyms: Vec::new(),
                    expression: self.expressions[i].as_deref(),
                })
                .collect()
        }
    }

    /// A layered project of `models` models of `columns` columns, like the generated project
    /// of the Python scale tests, with a few columns whose names say what they are.
    fn project(models: usize, columns: usize) -> Project {
        let mut p = Project {
            ids: Vec::new(),
            names: Vec::new(),
            qualifiers: Vec::new(),
            expressions: Vec::new(),
            links: Vec::new(),
        };
        let words = [
            "revenue",
            "refund_amt_gbp",
            "churned_at",
            "qty",
            "unit_price",
            "customer_id",
        ];
        for m in 0..models {
            let model = format!("m{m:03}");
            p.ids.push(format!("model.big.{model}"));
            p.names.push(model.clone());
            p.qualifiers.push(String::new());
            p.expressions.push(None);
            let relation = p.ids.len() - 1;
            for c in 0..columns {
                let name = if c < words.len() && m % 7 == 0 {
                    words[c].to_owned()
                } else {
                    format!("c{c:02}")
                };
                p.ids.push(format!("model.big.{model}#{name}"));
                p.names.push(name.clone());
                p.qualifiers.push(model.clone());
                let column = p.ids.len() - 1;
                p.links.push(Link {
                    source: relation as u32,
                    target: column as u32,
                    kind: EdgeKind::Contains,
                });
                if m >= 10 {
                    let up = m / 2 * (columns + 1) + 1 + c;
                    p.expressions.push(Some(format!("a.{} * 2", p.names[up])));
                    p.links.push(Link {
                        source: column as u32,
                        target: up as u32,
                        kind: EdgeKind::DerivedFrom,
                    });
                } else {
                    p.expressions.push(None);
                }
            }
        }
        p
    }

    fn standings(n: usize) -> Vec<u8> {
        vec![0; n]
    }

    #[test]
    fn search_finds_what_a_full_scan_finds() {
        let p = project(300, 12);
        let nodes = p.nodes();
        let index = VectorIndex::new(&nodes, &p.links, &standings(nodes.len()), None, 4);
        for question in [
            "revenue",
            "refund amount in pounds",
            "cancellations",
            "price",
            "quantity",
        ] {
            let scan = index.scan(question, 10);
            let found = index.search(question, 10, &[]);
            assert!(!scan.is_empty(), "{question}");
            let same = scan
                .iter()
                .filter(|h| found.iter().any(|f| f.ordinal == h.ordinal))
                .count();
            assert!(
                same * 10 >= scan.len() * 9,
                "{question}: {same} of {}",
                scan.len()
            );
            assert_eq!(found[0], scan[0], "{question}");
        }
        let churn = index.search("cancellations", 3, &[]);
        assert!(nodes[churn[0].ordinal as usize].name.contains("churn"));
        let pounds = index.search("refund amount in pounds", 3, &[]);
        assert!(
            nodes[pounds[0].ordinal as usize]
                .name
                .starts_with("refund_amt_gbp")
        );
        assert!(index.search("zzqx", 5, &[]).is_empty());
        let models = index.search("m007", 5, &["model"]);
        assert_eq!(nodes[models[0].ordinal as usize].id, "model.big.m007");
        assert!(
            models
                .iter()
                .all(|h| nodes[h.ordinal as usize].kind == "model")
        );
    }

    #[test]
    fn saved_indexes_are_reused_and_updated() {
        let p = project(120, 8);
        let nodes = p.nodes();
        let s = standings(nodes.len());
        let full = VectorIndex::new(&nodes, &p.links, &s, None, 2);
        assert_eq!(
            full.update(),
            Update {
                kept: 0,
                added: nodes.len(),
                removed: 0,
                reused: false,
                rows_read: false,
            }
        );
        let bytes = full.to_bytes();
        // Unchanged: everything is kept, the blocks are read rather than computed, and the
        // bytes are the same.
        let again = VectorIndex::new(&nodes, &p.links, &s, Some(&bytes), 2);
        assert_eq!(
            again.update(),
            Update {
                kept: nodes.len(),
                added: 0,
                removed: 0,
                reused: true,
                rows_read: true,
            }
        );
        assert_eq!(again.to_bytes(), bytes);
        assert_eq!(
            again.search("revenue", 5, &[]),
            full.search("revenue", 5, &[])
        );
        // Rename one source column: it and what reads it change, nothing else.
        let mut renamed = project(120, 8);
        renamed.names[1] = "gross_sales".into();
        let renamed_nodes = renamed.nodes();
        let updated = VectorIndex::new(&renamed_nodes, &renamed.links, &s, Some(&bytes), 2);
        let update = updated.update();
        assert!(update.reused && !update.rows_read && update.added == update.removed);
        assert!(
            update.added >= 1 && update.added < nodes.len() / 10,
            "{update:?}"
        );
        let rebuilt = VectorIndex::new(&renamed_nodes, &renamed.links, &s, None, 2);
        assert_eq!(
            updated.search("gross sales", 5, &[]),
            rebuilt.search("gross sales", 5, &[])
        );
        // The same graph with its nodes in another order still reads the saved blocks.
        let order: Vec<usize> = (0..nodes.len()).rev().collect();
        let position = |n: u32| (nodes.len() - 1 - n as usize) as u32;
        let shuffled: Vec<Node<'_>> = order.iter().map(|&n| nodes[n].clone()).collect();
        let links: Vec<Link> = p
            .links
            .iter()
            .map(|l| Link {
                source: position(l.source),
                target: position(l.target),
                kind: l.kind,
            })
            .collect();
        let reordered = VectorIndex::new(&shuffled, &links, &s, Some(&bytes), 2);
        assert!(reordered.update().rows_read);
        assert_eq!(reordered.to_bytes(), bytes);
        let found = |index: &VectorIndex, nodes: &[Node<'_>]| -> Vec<String> {
            index
                .search("revenue", 5, &[])
                .iter()
                .map(|h| nodes[h.ordinal as usize].id.to_owned())
                .collect()
        };
        assert_eq!(found(&reordered, &shuffled), found(&full, &nodes));
        // A damaged or foreign file is ignored.
        let ignored = VectorIndex::new(&nodes, &p.links, &s, Some(b"not an index"), 1);
        assert!(!ignored.update().reused);
        assert_eq!(ignored.to_bytes(), bytes);
    }

    #[test]
    fn every_machine_builds_the_same_index() {
        // The hash of a saved index, the same on every architecture, either endianness and any
        // number of threads: integer sums, and floating point in a fixed order.
        let p = project(80, 6);
        let nodes = p.nodes();
        let s = standings(nodes.len());
        for threads in [1, 3] {
            let bytes = VectorIndex::new(&nodes, &p.links, &s, None, threads).to_bytes();
            assert_eq!(
                fnv::hash(fnv::OFFSET_BASIS, &bytes),
                0x0452_a920_f857_e478,
                "{threads}"
            );
        }
    }

    #[test]
    fn similar_columns_share_lineage_and_formulas() {
        let p = project(60, 6);
        let nodes = p.nodes();
        let index = VectorIndex::new(&nodes, &p.links, &standings(nodes.len()), None, 1);
        let start = p
            .ids
            .iter()
            .position(|i| i == "model.big.m014#qty")
            .unwrap();
        let like = index.similar(start as u32, 5, &["column"]);
        assert!(like.iter().all(|h| h.ordinal as usize != start));
        assert!(
            like.iter().any(|h| nodes[h.ordinal as usize].name == "qty"),
            "{like:?}"
        );
        assert!(index.similar(u32::MAX, 5, &[]).is_empty());
    }
}
