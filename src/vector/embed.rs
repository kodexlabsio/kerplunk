//! Each node's vector, composed from what it is called, what it computes and where it sits.
//!
//! A node has four blocks, the first three sparse rows of hashed features, scored exactly:
//!
//! * **Words**: the node's own name, synonyms, qualifier (a column's relation) and description.
//!   A feature many nodes share weighs less: `1 / (1 + floor(log2(nodes with it)))`. The weight
//!   depends on the count only through its power of two, so adding a few nodes rarely changes
//!   any weight, and an update re-embeds only what changed.
//! * **Lineage**: the whole-word features of the node's neighbours, averaged with weights by
//!   kind of edge (a business term that defines a column says the most about it, a column's
//!   upstream columns next) and propagated two steps, so a column with a bare name still
//!   carries what its sources and its terms call it.
//! * **Formula**: the canonical tokens of the column's expression (see [`super::formula`]),
//!   composed with its upstream columns' formulas at half weight per step, three steps deep, as
//!   the composed formula would be. A column that only copies another, and a column with no
//!   expression, computes nothing, so its own formula block is empty; what it copies still
//!   reaches the formulas downstream of it.
//! * **Structure**: a pseudo-random signature per node, propagated three steps up the lineage,
//!   so columns built from the same sources by the same paths point the same way. Questions in
//!   words leave it at zero; it serves [`super::VectorIndex::similar`], and only alongside a
//!   feature the two nodes share.
//!
//! A block's length is at most one and its weight scales it, so ranking by cosine is ranking by
//! the inner product with the question, block by block: a node that matches in its words and
//! its formula outranks one that matches in its words alone. Rows keep their heaviest
//! [`KEEP`] features, so a node with many neighbours does not get a long row of faint ones.
//!
//! The graph search navigates by a dense count sketch of each node's blocks (see [`sketch`]),
//! in which hashing collisions are harmless: they only change which candidates are found, and
//! every candidate is then scored from its sparse rows, where two features meet only when they
//! are the same.
//!
//! Every step is integer arithmetic or IEEE `f32` arithmetic in a fixed order (sums, products,
//! divisions and square roots, never a transcendental function), with neighbours in the order
//! of their keys and equal features summed in the order of their weights, so a graph has one
//! embedding on every machine, whatever order its nodes arrive in.

use std::collections::HashSet;
use std::hash::BuildHasherDefault;

use crate::fnv::{self, Fnv1a, FnvHashMap};
use crate::graph::EdgeKind;

use super::dot::dot;
use super::formula;
use super::text::{self, Feature, Space, mix};

/// The sizes of the sketch's blocks, before it is folded to [`SKETCH`] values.
pub const WORDS: usize = 128;
pub const LINEAGE: usize = 48;
pub const FORMULA: usize = 48;
pub const STRUCTURE: usize = 31;
/// The dense vector's length: the four blocks and the component that evens out lengths.
pub const DIM: usize = WORDS + LINEAGE + FORMULA + STRUCTURE + 1;

/// The length of the sketch the graph search navigates by (see [`sketch`]).
pub const SKETCH: usize = 64;

/// Where each block starts in the dense vector.
const AT_WORDS: usize = 0;
const AT_LINEAGE: usize = AT_WORDS + WORDS;
const AT_FORMULA: usize = AT_LINEAGE + LINEAGE;
const AT_STRUCTURE: usize = AT_FORMULA + FORMULA;
const AT_LENGTH: usize = AT_STRUCTURE + STRUCTURE;

/// Changes whenever the features, weights or dimensions change, so a saved index made by an
/// older embedding is rebuilt rather than read.
pub const VERSION: u32 = 2;

/// How much each block of a node weighs.
const NODE_WORDS: f32 = 1.0;
const NODE_LINEAGE: f32 = 0.75;
const NODE_FORMULA: f32 = 0.6;
const NODE_STRUCTURE: f32 = 0.5;

/// How much each block of a question weighs: (words, lineage, formula), for a question in
/// words and for one that reads as a formula.
const QUESTION: (f32, f32, f32) = (1.0, 0.6, 0.5);
const FORMULA_QUESTION: (f32, f32, f32) = (0.4, 0.2, 1.0);

/// How much each part of a node's text weighs.
const NAME: f32 = 1.0;
const SYNONYM: f32 = 0.9;
const QUALIFIER: f32 = 0.35;
const DESCRIPTION: f32 = 0.45;
/// A column without an expression, as its consumers' formulas read it.
const BARE_COLUMN: f32 = 0.5;

/// What carries over from one step of propagation to the next.
const CARRY: f32 = 0.5;

/// A block whose features weigh less than this together is not stretched to length one: a
/// node named only in common words keeps a short block, and so a low score.
const FLOOR: f32 = 1.0;

/// The most features a lineage or formula row keeps: its heaviest.
pub const KEEP: usize = 64;

/// A set of features.
type FeatureSet = HashSet<u64, BuildHasherDefault<Fnv1a>>;

/// One node, as the embedding reads it.
#[derive(Clone, Debug, Default)]
pub struct Node<'a> {
    pub id: &'a str,
    pub kind: &'a str,
    pub name: &'a str,
    /// For a column, its relation's label; for a relation, its warehouse name.
    pub qualifier: &'a str,
    pub description: &'a str,
    pub synonyms: Vec<&'a str>,
    /// For a column, its SQL expression.
    pub expression: Option<&'a str>,
}

/// One edge, between node ordinals, as `graph.json` directs it.
#[derive(Clone, Copy, Debug)]
pub struct Link {
    pub source: u32,
    pub target: u32,
    pub kind: EdgeKind,
}

/// How much a node learns from a neighbour across an edge: (what the source learns from the
/// target, what the target learns from the source).
fn lineage_weights(kind: EdgeKind) -> (f32, f32) {
    match kind {
        // A downstream column and its upstream column.
        EdgeKind::DerivedFrom => (1.0, 0.4),
        // A relation and its column.
        EdgeKind::Contains => (0.25, 0.5),
        // A business term and the column it defines.
        EdgeKind::Defines => (0.8, 2.0),
        EdgeKind::RelatesTo => (0.4, 0.4),
        // A test and what it tests.
        EdgeKind::Tests => (1.0, 0.0),
        // An upstream relation and its consumer.
        EdgeKind::Feeds => (0.2, 0.4),
        EdgeKind::Runs | EdgeKind::Precedes => (0.3, 0.3),
        EdgeKind::Other => (0.0, 0.0),
    }
}

/// For the structural signature and formula composition: the edges that point from a node to
/// what it is made from. Returns (node, its input) when `kind` is such an edge.
fn upstream(link: &Link) -> Option<(u32, u32)> {
    match link.kind {
        EdgeKind::DerivedFrom | EdgeKind::Tests | EdgeKind::Defines => {
            Some((link.source, link.target))
        }
        EdgeKind::Feeds => Some((link.target, link.source)),
        _ => None,
    }
}

/// Merge equal features, summing their weights, in feature order. Equal features are summed in
/// the order of their weights, so the sum does not depend on the order they came in.
fn merge(mut features: Vec<Feature>) -> Vec<Feature> {
    features.sort_unstable_by(|a, b| a.0.cmp(&b.0).then(a.1.total_cmp(&b.1)));
    let mut merged: Vec<Feature> = Vec::with_capacity(features.len());
    for (hash, weight) in features {
        match merged.last_mut() {
            Some(last) if last.0 == hash => last.1 += weight,
            _ => merged.push((hash, weight)),
        }
    }
    merged
}

/// A merged row cut to its `keep` heaviest features (ties to the smaller feature), in feature
/// order.
fn heaviest(mut row: Vec<Feature>, keep: usize) -> Vec<Feature> {
    if row.len() > keep {
        row.sort_unstable_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
        row.truncate(keep);
        row.sort_unstable_by_key(|f| f.0);
    }
    row
}

/// The length of a sparse row.
fn row_length(row: &[Feature]) -> f32 {
    row.iter().map(|f| f.1 * f.1).sum::<f32>().sqrt()
}

/// A row divided by its length, or by `floor` when it is shorter, then multiplied by `weight`.
fn scaled(mut row: Vec<Feature>, floor: f32, weight: f32) -> Vec<Feature> {
    let n = row_length(&row).max(floor);
    if n > 0.0 {
        row.iter_mut().for_each(|f| f.1 = f.1 / n * weight);
    }
    row
}

/// Add the features to a dense block, each at two positions with a sign each. `seed` makes
/// independent projections of the same features.
fn project(features: &[Feature], seed: u64, block: &mut [f32]) {
    let size = block.len() as u64;
    // 1/sqrt(2), so a feature's two halves together have its weight.
    const HALF: f32 = std::f32::consts::FRAC_1_SQRT_2;
    for &(hash, weight) in features {
        let one = mix(hash ^ seed);
        let two = mix(one);
        for h in [one, two] {
            let sign = if h >> 63 == 0 { 1.0 } else { -1.0 };
            block[((h >> 1) % size) as usize] += sign * weight * HALF;
        }
    }
}

fn length(block: &[f32]) -> f32 {
    block.iter().map(|x| x * x).sum::<f32>().sqrt()
}

/// Divide by the block's length, or by `floor` when the block is shorter.
fn shrink(block: &mut [f32], floor: f32) {
    let n = length(block).max(floor);
    if n > 0.0 {
        block.iter_mut().for_each(|x| *x /= n);
    }
}

/// The weight of a feature `count` nodes share.
fn rarity(count: u32) -> f32 {
    1.0 / (1.0 + count.max(1).ilog2() as f32)
}

const SEED_WORDS: u64 = 0x7701;
const SEED_LINEAGE: u64 = 0x7702;
const SEED_FORMULA: u64 = 0x7703;
const SEED_STRUCTURE: u64 = 0x7704;

/// A node's own words, as sparse features.
fn own_words(node: &Node<'_>) -> Vec<Feature> {
    let mut out = Vec::new();
    text::text_features(node.name, NAME, &mut out);
    for synonym in &node.synonyms {
        text::text_features(synonym, SYNONYM, &mut out);
    }
    text::text_features(node.qualifier, QUALIFIER, &mut out);
    text::text_features(node.description, DESCRIPTION, &mut out);
    merge(out)
}

/// What a node's own formula is: a computation, a copy of one column, or nothing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Computes {
    Formula,
    Copy,
    Nothing,
}

/// A node's own formula, as sparse features, and what it computes.
fn own_formula(node: &Node<'_>) -> (Vec<Feature>, Computes) {
    let mut out = Vec::new();
    let computes = match node.expression {
        Some(sql) if formula::is_column(sql) => Computes::Copy,
        Some(sql) => {
            formula::features(sql, 1.0, false, &mut out);
            Computes::Formula
        }
        None if node.kind == "column" => {
            // The column as the formulas downstream of it read it.
            formula::features(node.name, BARE_COLUMN, true, &mut out);
            Computes::Nothing
        }
        None => Computes::Nothing,
    };
    (merge(out), computes)
}

/// Divide each feature's weight by how common it is.
fn weigh_by_rarity(all: &mut [Vec<Feature>]) {
    let mut counts: FnvHashMap<u64, u32> = FnvHashMap::default();
    for features in all.iter() {
        for &(hash, _) in features {
            *counts.entry(hash).or_insert(0) += 1;
        }
    }
    for features in all.iter_mut() {
        for feature in features.iter_mut() {
            feature.1 *= rarity(counts[&feature.0]);
        }
    }
}

/// Weighted neighbour lists in compressed form: for each node, (neighbour, weight), in the
/// order of the neighbours' keys.
struct Neighbours {
    start: Vec<u32>,
    entries: Vec<(u32, f32)>,
}

impl Neighbours {
    fn new(keys: &[u64], pairs: impl Iterator<Item = (u32, u32, f32)>) -> Self {
        let count = keys.len();
        let mut pairs: Vec<(u32, u32, f32)> = pairs
            .filter(|&(a, b, w)| w > 0.0 && a != b && (a as usize) < count && (b as usize) < count)
            .collect();
        pairs.sort_by(|x, y| {
            (x.0, keys[x.1 as usize], x.1)
                .cmp(&(y.0, keys[y.1 as usize], y.1))
                .then(x.2.total_cmp(&y.2))
        });
        let mut start = vec![0u32; count + 1];
        for &(a, _, _) in &pairs {
            start[a as usize + 1] += 1;
        }
        for i in 0..count {
            start[i + 1] += start[i];
        }
        Self {
            start,
            entries: pairs.into_iter().map(|(_, b, w)| (b, w)).collect(),
        }
    }

    fn of(&self, node: usize) -> &[(u32, f32)] {
        &self.entries[self.start[node] as usize..self.start[node + 1] as usize]
    }

    /// The sum of a node's weights, at least one, which its neighbours' rows are divided by.
    fn total(&self, node: usize) -> f32 {
        self.of(node).iter().map(|e| e.1).sum::<f32>().max(1.0)
    }
}

/// `f` of every node ordinal below `count`, in order, on up to `threads` threads.
fn each<U: Send>(count: usize, threads: usize, f: impl Fn(usize) -> U + Sync) -> Vec<U> {
    let ordinals: Vec<usize> = (0..count).collect();
    map(&ordinals, threads, |&n| f(n))
}

/// `own` (when given) plus `carry` times the weighted mean of the neighbours' rows, cut to its
/// [`KEEP`] heaviest features.
fn gather(
    own: Option<&[Feature]>,
    rows: &[Vec<Feature>],
    neighbours: &Neighbours,
    node: usize,
    carry: f32,
) -> Vec<Feature> {
    let list = neighbours.of(node);
    let mut row: Vec<Feature> = own.map(<[Feature]>::to_vec).unwrap_or_default();
    if !list.is_empty() {
        let total = neighbours.total(node);
        for &(other, weight) in list {
            let scale = carry * weight / total;
            row.extend(rows[other as usize].iter().map(|&(h, w)| (h, w * scale)));
        }
    }
    heaviest(merge(row), KEEP)
}

/// The mean of the neighbours' dense blocks, without the node's own, with the same weights.
fn gather_dense(blocks: &[f32], size: usize, neighbours: &Neighbours) -> Vec<f32> {
    let count = blocks.len() / size;
    let mut out = vec![0.0f32; blocks.len()];
    for node in 0..count {
        let list = neighbours.of(node);
        if list.is_empty() {
            continue;
        }
        let total = neighbours.total(node);
        let row = &mut out[node * size..(node + 1) * size];
        for &(other, weight) in list {
            let scale = weight / total;
            let source = &blocks[other as usize * size..(other as usize + 1) * size];
            for (x, y) in row.iter_mut().zip(source) {
                *x += scale * y;
            }
        }
    }
    out
}

/// The output position and sign of each dense component in the sketch.
fn sketch_map() -> &'static [(u8, bool); DIM] {
    static MAP: std::sync::OnceLock<[(u8, bool); DIM]> = std::sync::OnceLock::new();
    MAP.get_or_init(|| {
        let mut map = [(0u8, false); DIM];
        for (i, entry) in map.iter_mut().enumerate() {
            let h = mix(0x5ce7_c400 ^ i as u64);
            *entry = (((h >> 1) % SKETCH as u64) as u8, h & 1 == 1);
        }
        map
    })
}

/// A count sketch of a dense vector (Charikar, Chen and Farach-Colton, 2002): each component
/// added, with a fixed random sign, to one of [`SKETCH`] sums, then quantised. Inner products
/// of sketches estimate those of the vectors, which is what the graph search needs to
/// navigate; every candidate it finds is then scored exactly.
pub fn sketch(vector: &[f32]) -> Vec<i8> {
    let mut out = [0.0f32; SKETCH];
    for (&x, &(at, negative)) in vector.iter().zip(sketch_map().iter()) {
        out[at as usize] += if negative { -x } else { x };
    }
    quantise(&out).0
}

/// Quantise to `i8`, the largest component to ±127, with the factor the values were
/// multiplied by (1 for a zero vector).
pub fn quantise(vector: &[f32]) -> (Vec<i8>, f32) {
    let largest = vector.iter().fold(0.0f32, |m, x| m.max(x.abs()));
    if largest == 0.0 {
        return (vec![0; vector.len()], 1.0);
    }
    let scale = 127.0 / largest;
    let quantised = vector
        .iter()
        .map(|x| (x * scale).round().clamp(-127.0, 127.0) as i8)
        .collect();
    (quantised, scale)
}

/// Sparse rows, compactly: each node's (feature, weight) pairs, by feature.
#[derive(Clone, Debug, Default)]
pub struct Sparse {
    start: Vec<u32>,
    features: Vec<u64>,
    weights: Vec<f32>,
}

impl Sparse {
    fn of(rows: impl IntoIterator<Item = Vec<Feature>>) -> Self {
        let mut sparse = Self {
            start: vec![0],
            ..Self::default()
        };
        for row in rows {
            for (feature, weight) in row {
                sparse.features.push(feature);
                sparse.weights.push(weight);
            }
            sparse
                .start
                .push(crate::graph::ordinal(sparse.features.len()));
        }
        sparse
    }

    pub fn row(&self, node: usize) -> (&[u64], &[f32]) {
        let (a, b) = (self.start[node] as usize, self.start[node + 1] as usize);
        (&self.features[a..b], &self.weights[a..b])
    }

    fn owned_row(&self, node: usize) -> Vec<Feature> {
        let (features, weights) = self.row(node);
        features
            .iter()
            .copied()
            .zip(weights.iter().copied())
            .collect()
    }

    pub fn len(&self) -> usize {
        self.start.len().saturating_sub(1)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The inner product of two sparse rows, each sorted by feature, and whether they share a
/// feature that is not a trigram.
pub fn sparse_dot(a: (&[u64], &[f32]), b: (&[u64], &[f32])) -> (f32, bool) {
    let (mut i, mut j, mut sum, mut whole) = (0, 0, 0.0f32, false);
    while i < a.0.len() && j < b.0.len() {
        match a.0[i].cmp(&b.0[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                sum += a.1[i] * b.1[j];
                whole |= !text::is_trigram(a.0[i]);
                i += 1;
                j += 1;
            }
        }
    }
    (sum, whole)
}

/// The blocks of a graph's nodes.
#[derive(Clone, Debug, Default)]
pub struct Embedding {
    /// Each node's own words, weighed by rarity.
    pub words: Sparse,
    /// Each node's neighbours' whole-word features, two steps out.
    pub lineage: Sparse,
    /// Each column's formula, composed with its inputs'; empty for one that computes nothing.
    pub formulas: Sparse,
    /// `STRUCTURE` values per node, quantised, and what each node's were multiplied by.
    pub structure: Vec<i8>,
    pub scales: Vec<f32>,
    /// `SKETCH` values per node: the sketches the graph search navigates by.
    pub sketches: Vec<i8>,
    /// The nodes that have each whole feature in their words or formula, for finding
    /// candidates.
    pub postings: FnvHashMap<u64, Vec<u32>>,
    /// Every feature of any node's rows: a question's other features match nothing.
    known: FeatureSet,
}

/// A question, embedded.
#[derive(Clone, Debug)]
pub struct Question {
    /// Its blocks, each a sparse row with one node's place, and its structure (zero for a
    /// question in words).
    pub words: Sparse,
    pub lineage: Sparse,
    pub formula: Sparse,
    pub structure: Vec<i8>,
    pub scale: f32,
    pub sketch: Vec<i8>,
    /// Its whole features, to find candidates by.
    pub keys: Vec<u64>,
    /// For each content word of a question in words, the features that mean it: the word,
    /// its concept and the concept it is partly, in a node's words and lineage or as a column
    /// its formula reads. Empty for a question that reads as a formula, or is a node.
    pub content: Vec<Vec<u64>>,
}

/// `f` of every item, in order, on up to `threads` threads.
fn map<T: Sync, U: Send>(items: &[T], threads: usize, f: impl Fn(&T) -> U + Sync) -> Vec<U> {
    let chunk = items.len().div_ceil(threads.max(1)).max(256);
    if chunk >= items.len() {
        return items.iter().map(f).collect();
    }
    let f = &f;
    std::thread::scope(|scope| {
        let parts: Vec<_> = items
            .chunks(chunk)
            .map(|part| scope.spawn(move || part.iter().map(f).collect::<Vec<U>>()))
            .collect();
        parts
            .into_iter()
            .flat_map(|part| part.join().expect("an embedding thread panicked"))
            .collect()
    })
}

/// A dense block holding a sparse row's projection, at the row's length.
fn fill(block: &mut [f32], row: &[Feature], seed: u64) {
    project(row, seed, block);
    let wanted = row_length(row);
    shrink(block, 0.0);
    block.iter_mut().for_each(|x| *x *= wanted);
}

/// Every node's blocks, in node order, using up to `threads` threads; `keys` are the nodes'
/// keys, which order each node's neighbours. The result is the same for any number of threads
/// and any order of the nodes.
pub fn embed(nodes: &[Node<'_>], keys: &[u64], links: &[Link], threads: usize) -> Embedding {
    assert_eq!(nodes.len(), keys.len());
    let count = nodes.len();
    let mut words: Vec<Vec<Feature>> = map(nodes, threads, own_words);
    weigh_by_rarity(&mut words);
    let (mut own_formulas, computes): (Vec<Vec<Feature>>, Vec<Computes>) =
        map(nodes, threads, own_formula).into_iter().unzip();
    weigh_by_rarity(&mut own_formulas);

    // Lineage: the neighbours' whole words, two steps out.
    let context = Neighbours::new(
        keys,
        links.iter().flat_map(|link| {
            let (to_source, to_target) = lineage_weights(link.kind);
            [
                (link.source, link.target, to_source),
                (link.target, link.source, to_target),
            ]
        }),
    );
    let own_lineage: Vec<Vec<Feature>> = map(&words, threads, |row| {
        let whole = row
            .iter()
            .filter(|f| !text::is_trigram(f.0))
            .copied()
            .collect();
        scaled(whole, FLOOR, 1.0)
    });
    let first = each(count, threads, |n| {
        gather(None, &own_lineage, &context, n, 1.0)
    });
    let carried = each(count, threads, |n| {
        let mut row = own_lineage[n].clone();
        row.extend(first[n].iter().map(|&(h, w)| (h, CARRY * w)));
        heaviest(merge(row), KEEP)
    });
    drop((first, own_lineage));
    let lineage = each(count, threads, |n| {
        scaled(
            gather(None, &carried, &context, n, 1.0),
            FLOOR,
            NODE_LINEAGE,
        )
    });
    drop(carried);

    // Formulas composed with their inputs' formulas, three steps up; a copy passes on what it
    // copies at full weight, as its consumers read the column it copies.
    let inputs = Neighbours::new(
        keys,
        links
            .iter()
            .filter_map(upstream)
            .map(|(node, input)| (node, input, 1.0)),
    );
    let mut composed = own_formulas.clone();
    for _ in 0..3 {
        composed = each(count, threads, |n| {
            let carry = if computes[n] == Computes::Copy {
                1.0
            } else {
                CARRY
            };
            gather(Some(&own_formulas[n]), &composed, &inputs, n, carry)
        });
    }
    drop(own_formulas);
    let formulas: Vec<Vec<Feature>> = composed
        .into_iter()
        .zip(&computes)
        .map(|(row, &computes)| match computes {
            Computes::Formula => scaled(row, FLOOR, NODE_FORMULA),
            Computes::Copy | Computes::Nothing => Vec::new(),
        })
        .collect();

    // Structural signatures, propagated upstream.
    let mut signatures = vec![0.0f32; count * STRUCTURE];
    let unit = 1.0 / (STRUCTURE as f32).sqrt();
    for (node, row) in signatures
        .as_chunks_mut::<STRUCTURE>()
        .0
        .iter_mut()
        .enumerate()
    {
        let mut h = keys[node] ^ SEED_STRUCTURE;
        for x in row.iter_mut() {
            h = mix(h);
            *x = if h >> 63 == 0 { unit } else { -unit };
        }
    }
    let mut structure: Vec<f32> = signatures.iter().map(|x| 0.3 * x).collect();
    let mut reached = signatures;
    let mut carry = 1.0f32;
    for _ in 0..3 {
        reached = gather_dense(&reached, STRUCTURE, &inputs);
        for (x, y) in structure.iter_mut().zip(&reached) {
            *x += carry * y;
        }
        carry *= CARRY;
    }
    drop(reached);

    let words: Vec<Vec<Feature>> = words
        .into_iter()
        .map(|row| scaled(row, FLOOR, NODE_WORDS))
        .collect();
    let full = full_length();
    let mut quantised_structure = Vec::with_capacity(count * STRUCTURE);
    let mut scales = Vec::with_capacity(count);
    let mut sketches = Vec::with_capacity(count * SKETCH);
    let mut vector = vec![0.0f32; DIM];
    for node in 0..count {
        let block = &mut structure[node * STRUCTURE..(node + 1) * STRUCTURE];
        shrink(block, FLOOR);
        block.iter_mut().for_each(|x| *x *= NODE_STRUCTURE);
        let (values, scale) = quantise(block);
        quantised_structure.extend(values);
        scales.push(scale);
        vector.fill(0.0);
        fill(&mut vector[AT_WORDS..AT_LINEAGE], &words[node], SEED_WORDS);
        fill(
            &mut vector[AT_LINEAGE..AT_FORMULA],
            &lineage[node],
            SEED_LINEAGE,
        );
        fill(
            &mut vector[AT_FORMULA..AT_STRUCTURE],
            &formulas[node],
            SEED_FORMULA,
        );
        vector[AT_STRUCTURE..AT_LENGTH].copy_from_slice(block);
        let used = length(&vector[..AT_LENGTH]);
        vector[AT_LENGTH] = (full * full - used * used).max(0.0).sqrt();
        sketches.extend(sketch(&vector));
    }
    Embedding::assemble(
        words,
        lineage,
        formulas,
        quantised_structure,
        scales,
        sketches,
        None,
    )
}

/// One node's blocks, as a saved index stores them.
#[derive(Clone, Debug, PartialEq)]
pub struct NodeRows {
    pub words: Vec<Feature>,
    pub lineage: Vec<Feature>,
    pub formula: Vec<Feature>,
    /// `STRUCTURE` values, quantised, and what they were multiplied by.
    pub structure: Vec<i8>,
    pub scale: f32,
    /// `SKETCH` values.
    pub sketch: Vec<i8>,
}

/// A digest of everything the embedding reads (each node's text and expression, and every
/// edge, by the nodes' keys) and of the embedding's version: equal digests give equal
/// embeddings, so a saved index whose digest is this one's holds this embedding's rows.
pub fn fingerprint(nodes: &[Node<'_>], keys: &[u64], links: &[Link]) -> u64 {
    // 0xff appears in no UTF-8, so it ends each string unambiguously.
    let field = |state: u64, text: &str| fnv::hash(fnv::hash(state, text.as_bytes()), &[0xff]);
    let mut digests: Vec<(u64, u64)> = nodes
        .iter()
        .zip(keys)
        .map(|(node, &key)| {
            let mut state = fnv::OFFSET_BASIS;
            for text in [
                node.id,
                node.kind,
                node.name,
                node.qualifier,
                node.description,
            ] {
                state = field(state, text);
            }
            state = fnv::hash(state, &(node.synonyms.len() as u64).to_le_bytes());
            for synonym in &node.synonyms {
                state = field(state, synonym);
            }
            state = match node.expression {
                Some(sql) => field(fnv::hash(state, &[1]), sql),
                None => fnv::hash(state, &[0]),
            };
            (key, state)
        })
        .collect();
    digests.sort_unstable();
    let mut edges: Vec<(u64, u64, u8)> = links
        .iter()
        .map(|l| {
            (
                keys[l.source as usize],
                keys[l.target as usize],
                l.kind as u8,
            )
        })
        .collect();
    edges.sort_unstable();
    let mut state = fnv::hash(fnv::OFFSET_BASIS, &VERSION.to_le_bytes());
    for (key, digest) in digests {
        state = fnv::hash(state, &key.to_le_bytes());
        state = fnv::hash(state, &digest.to_le_bytes());
    }
    for (source, target, kind) in edges {
        state = fnv::hash(state, &source.to_le_bytes());
        state = fnv::hash(state, &target.to_le_bytes());
        state = fnv::hash(state, &[kind]);
    }
    state
}

impl Embedding {
    /// The embedding of these blocks, with the postings and the set of features found from
    /// them.
    fn assemble(
        words: Vec<Vec<Feature>>,
        lineage: Vec<Vec<Feature>>,
        formulas: Vec<Vec<Feature>>,
        structure: Vec<i8>,
        scales: Vec<f32>,
        sketches: Vec<i8>,
        known: Option<Vec<u64>>,
    ) -> Self {
        // Each node's whole features of its words and formula, as (feature, node) pairs sorted
        // by feature, then grouped: each posting list comes out in node order.
        let mut pairs: Vec<(u64, u32)> = Vec::new();
        let mut whole: Vec<u64> = Vec::new();
        for node in 0..words.len() {
            whole.clear();
            // Not the lineage rows: those are long, and the graph search finds what they match.
            for row in [&words[node], &formulas[node]] {
                whole.extend(row.iter().map(|f| f.0).filter(|&h| !text::is_trigram(h)));
            }
            whole.sort_unstable();
            whole.dedup();
            let ordinal = crate::graph::ordinal(node);
            pairs.extend(whole.iter().map(|&h| (h, ordinal)));
        }
        pairs.sort_unstable();
        let mut postings: FnvHashMap<u64, Vec<u32>> = FnvHashMap::default();
        for group in pairs.chunk_by(|a, b| a.0 == b.0) {
            postings.insert(group[0].0, group.iter().map(|p| p.1).collect());
        }
        let known: FeatureSet = match known {
            Some(features) => features.into_iter().collect(),
            None => [&words, &lineage, &formulas]
                .into_iter()
                .flatten()
                .flat_map(|row| row.iter().map(|f| f.0))
                .collect(),
        };
        Self {
            words: Sparse::of(words),
            lineage: Sparse::of(lineage),
            formulas: Sparse::of(formulas),
            structure,
            scales,
            sketches,
            postings,
            known,
        }
    }

    /// The embedding saved as these rows, one per node in node order, with `features`, every
    /// feature of the rows.
    pub fn from_rows(rows: Vec<NodeRows>, features: Vec<u64>) -> Self {
        let count = rows.len();
        let (mut words, mut lineage, mut formulas) = (
            Vec::with_capacity(count),
            Vec::with_capacity(count),
            Vec::with_capacity(count),
        );
        let mut structure = Vec::with_capacity(count * STRUCTURE);
        let mut scales = Vec::with_capacity(count);
        let mut sketches = Vec::with_capacity(count * SKETCH);
        for row in rows {
            words.push(row.words);
            lineage.push(row.lineage);
            formulas.push(row.formula);
            structure.extend(row.structure);
            scales.push(row.scale);
            sketches.extend(row.sketch);
        }
        Self::assemble(
            words,
            lineage,
            formulas,
            structure,
            scales,
            sketches,
            Some(features),
        )
    }

    /// Node `node`'s blocks, to save.
    pub fn rows(&self, node: usize) -> NodeRows {
        NodeRows {
            words: self.words.owned_row(node),
            lineage: self.lineage.owned_row(node),
            formula: self.formulas.owned_row(node),
            structure: self.structure_of(node).to_vec(),
            scale: self.scales[node],
            sketch: self.sketches[node * SKETCH..(node + 1) * SKETCH].to_vec(),
        }
    }

    /// How many nodes the embedding holds.
    pub fn len(&self) -> usize {
        self.words.len()
    }

    pub fn is_empty(&self) -> bool {
        self.words.is_empty()
    }

    fn structure_of(&self, node: usize) -> &[i8] {
        &self.structure[node * STRUCTURE..(node + 1) * STRUCTURE]
    }
}

/// A question's features that some node has, merged.
fn known(embedding: &Embedding, features: Vec<Feature>) -> Vec<Feature> {
    merge(features)
        .into_iter()
        .filter(|f| embedding.known.contains(&f.0))
        .collect()
}

/// A question's blocks, its sketch and its keys, from its three rows and its structure.
fn ask(rows: [Vec<Feature>; 3], structure: Vec<i8>, scale: f32) -> Question {
    let mut vector = vec![0.0f32; DIM];
    let seeds = [
        (SEED_WORDS, AT_WORDS..AT_LINEAGE),
        (SEED_LINEAGE, AT_LINEAGE..AT_FORMULA),
        (SEED_FORMULA, AT_FORMULA..AT_STRUCTURE),
    ];
    for (row, (seed, range)) in rows.iter().zip(seeds) {
        fill(&mut vector[range], row, seed);
    }
    for (x, &q) in vector[AT_STRUCTURE..AT_LENGTH].iter_mut().zip(&structure) {
        *x = f32::from(q) / scale;
    }
    let mut keys: Vec<u64> = rows
        .iter()
        .flat_map(|row| row.iter().map(|f| f.0))
        .filter(|&h| !text::is_trigram(h))
        .collect();
    keys.sort_unstable();
    keys.dedup();
    let [words, lineage, formula] = rows;
    Question {
        words: Sparse::of([words]),
        lineage: Sparse::of([lineage]),
        formula: Sparse::of([formula]),
        structure,
        scale,
        sketch: sketch(&vector),
        keys,
        content: Vec::new(),
    }
}

/// A question's blocks: its words in the words and lineage blocks, and read as a formula in the
/// formula block, which weighs most when the question reads as one (`amount / 100`). Features
/// no node has are left out, so a word the project never uses neither matches nor dilutes the
/// words it does use.
pub fn question(embedding: &Embedding, text: &str) -> Question {
    let mut words = Vec::new();
    text::text_features(text, 1.0, &mut words);
    let words = known(embedding, words);
    let reads_as_formula = formula::is_formula(text);
    let (in_words, in_lineage, in_formula) = if reads_as_formula {
        FORMULA_QUESTION
    } else {
        QUESTION
    };
    let whole: Vec<Feature> = words
        .iter()
        .filter(|f| !text::is_trigram(f.0))
        .copied()
        .collect();
    let mut formula = Vec::new();
    formula::features(text, 1.0, true, &mut formula);
    let formula = known(embedding, formula);
    // A question's blocks are always length one: its words are all it has.
    let mut question = ask(
        [
            scaled(words, 0.0, in_words),
            scaled(whole, 0.0, in_lineage),
            scaled(formula, 0.0, in_formula),
        ],
        vec![0; STRUCTURE],
        1.0,
    );
    if !reads_as_formula {
        question.content = content_words(embedding, text);
    }
    question
}

/// The features that mean each content word of `text` (see [`Question::content`]): each word
/// that names something, in the project or in the table of data terms. Other words (`many`,
/// `after`) and numbers say how the question is put, not what it asks for.
fn content_words(embedding: &Embedding, text: &str) -> Vec<Vec<u64>> {
    let mut seen: Vec<String> = Vec::new();
    let mut out = Vec::new();
    for word in text::terms(text) {
        if word.chars().all(char::is_numeric) || seen.contains(&word) {
            continue;
        }
        let mut features = vec![
            text::feature(Space::Word, &[&word]),
            text::feature(Space::Column, &[&word]),
        ];
        if let Some(concept) = text::concept(&word) {
            for meaning in std::iter::once(concept).chain(text::related(concept)) {
                features.push(text::feature(Space::Concept, &[meaning]));
                features.push(text::feature(Space::ColumnConcept, &[meaning]));
            }
        }
        let named =
            text::concept(&word).is_some() || features.iter().any(|f| embedding.known.contains(f));
        if named {
            features.sort_unstable();
            out.push(features);
        }
        seen.push(word);
    }
    out
}

/// The share of the question's content words that node `node` means somewhere in its rows; 1
/// for a question with none.
pub fn coverage(question: &Question, embedding: &Embedding, node: usize) -> f32 {
    if question.content.is_empty() {
        return 1.0;
    }
    let rows = [
        embedding.words.row(node).0,
        embedding.lineage.row(node).0,
        embedding.formulas.row(node).0,
    ];
    let met = question
        .content
        .iter()
        .filter(|features| {
            features
                .iter()
                .any(|f| rows.iter().any(|row| row.binary_search(f).is_ok()))
        })
        .count();
    met as f32 / question.content.len() as f32
}

/// A node as a question about the nodes like it: its own blocks.
pub fn like(embedding: &Embedding, node: usize) -> Question {
    ask(
        [
            embedding.words.owned_row(node),
            embedding.lineage.owned_row(node),
            embedding.formulas.owned_row(node),
        ],
        embedding.structure_of(node).to_vec(),
        embedding.scales[node],
    )
}

/// A node's score for a question, block by block (words, lineage, formula, structure), and
/// whether the two share a feature that is not a trigram.
pub fn parts(question: &Question, embedding: &Embedding, node: usize) -> ([f32; 4], bool) {
    let (words, by_word) = sparse_dot(question.words.row(0), embedding.words.row(node));
    let (lineage, by_lineage) = sparse_dot(question.lineage.row(0), embedding.lineage.row(node));
    let (formula, by_formula) = sparse_dot(question.formula.row(0), embedding.formulas.row(node));
    let structure = dot(&question.structure, embedding.structure_of(node)) as f32
        / (question.scale * embedding.scales[node]);
    (
        [words, lineage, formula, structure],
        by_word || by_lineage || by_formula,
    )
}

/// The largest score a question can have: the product of its length and the longest a node's
/// can be.
pub fn most(question: &Question) -> f32 {
    let square = |row: (&[u64], &[f32])| row.1.iter().map(|w| w * w).sum::<f32>();
    let structure =
        dot(&question.structure, &question.structure) as f32 / (question.scale * question.scale);
    let length = (square(question.words.row(0))
        + square(question.lineage.row(0))
        + square(question.formula.row(0))
        + structure)
        .sqrt();
    length * full_length()
}

/// The longest a node's blocks can be together.
fn full_length() -> f32 {
    (NODE_WORDS * NODE_WORDS
        + NODE_LINEAGE * NODE_LINEAGE
        + NODE_FORMULA * NODE_FORMULA
        + NODE_STRUCTURE * NODE_STRUCTURE)
        .sqrt()
}

/// What a match rests on most: the node's words, its lineage, its formula or its structure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Basis {
    Words,
    Lineage,
    Formula,
    Structure,
}

impl Basis {
    pub fn name(self) -> &'static str {
        match self {
            Self::Words => "words",
            Self::Lineage => "lineage",
            Self::Formula => "formula",
            Self::Structure => "structure",
        }
    }

    /// The block that contributes most to a score, given block by block; the first of equals.
    pub fn of(parts: [f32; 4]) -> Self {
        [Self::Words, Self::Lineage, Self::Formula, Self::Structure]
            .into_iter()
            .zip(parts)
            .fold((Self::Words, f32::MIN), |best, (basis, value)| {
                if value > best.1 { (basis, value) } else { best }
            })
            .0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn column<'a>(
        id: &'a str,
        name: &'a str,
        description: &'a str,
        sql: Option<&'a str>,
    ) -> Node<'a> {
        Node {
            id,
            kind: "column",
            name,
            qualifier: id.split('#').next().unwrap_or(""),
            description,
            synonyms: Vec::new(),
            expression: sql,
        }
    }

    fn keys(nodes: &[Node<'_>]) -> Vec<u64> {
        nodes.iter().map(|n| super::super::key(n.id)).collect()
    }

    fn score(question: &Question, embedding: &Embedding, node: usize) -> f32 {
        parts(question, embedding, node).0.iter().sum()
    }

    fn explain(question: &Question, embedding: &Embedding, node: usize) -> Basis {
        Basis::of(parts(question, embedding, node).0)
    }

    #[test]
    fn every_block_is_at_most_its_weight() {
        let nodes = [
            column("a#x", "x", "", None),
            column(
                "b#net_sales_amt",
                "net_sales_amt",
                "Sales after refunds",
                Some("a.x - a.y"),
            ),
        ];
        let links = [Link {
            source: 1,
            target: 0,
            kind: EdgeKind::DerivedFrom,
        }];
        let embedding = embed(&nodes, &keys(&nodes), &links, 1);
        assert_eq!(embedding.len(), 2);
        for node in 0..2 {
            for (sparse, weight) in [
                (&embedding.words, NODE_WORDS),
                (&embedding.lineage, NODE_LINEAGE),
                (&embedding.formulas, NODE_FORMULA),
            ] {
                let (_, weights) = sparse.row(node);
                let length = weights.iter().map(|w| w * w).sum::<f32>().sqrt();
                assert!(length <= weight * 1.0001, "{node}: {length}");
            }
        }
        // A bare column computes nothing; the column that subtracts does.
        assert!(embedding.formulas.row(0).0.is_empty());
        assert!(!embedding.formulas.row(1).0.is_empty());
    }

    #[test]
    fn meaning_reaches_through_synonyms_lineage_and_formulas() {
        let nodes = [
            column("raw.orders#revenue", "revenue", "", None),
            column("stg.orders#rev", "rev", "", Some("raw.orders.revenue")),
            column(
                "fct.orders#net_amt",
                "net_amt",
                "",
                Some("stg.orders.rev - stg.orders.refund"),
            ),
            column("fct.orders#customer_id", "customer_id", "", None),
            column(
                "x#line_total",
                "line_total",
                "",
                Some("x.unit_price * x.qty"),
            ),
        ];
        let links = [
            Link {
                source: 1,
                target: 0,
                kind: EdgeKind::DerivedFrom,
            },
            Link {
                source: 2,
                target: 1,
                kind: EdgeKind::DerivedFrom,
            },
        ];
        let embedding = embed(&nodes, &keys(&nodes), &links, 2);
        let scores = |q: &str| -> Vec<f32> {
            let q = question(&embedding, q);
            (0..nodes.len())
                .map(|n| score(&q, &embedding, n) / most(&q))
                .collect()
        };
        let sales = scores("sales");
        // `rev` abbreviates revenue, of which sales is a synonym; `net_amt` reads it.
        assert!(sales[1] > 0.2 && sales[2] > 0.05, "{sales:?}");
        assert_eq!(sales[3], 0.0, "{sales:?}");
        let product = scores("price * quantity");
        assert!(product[4] > 2.0 * product[2], "{product:?}");
        let ask = |q: &str| question(&embedding, q);
        assert_eq!(
            explain(&ask("price * quantity"), &embedding, 4),
            Basis::Formula
        );
        assert_eq!(explain(&ask("revenue"), &embedding, 0), Basis::Words);
        assert_eq!(explain(&ask("revenue"), &embedding, 2), Basis::Lineage);
        // `rev` only copies `revenue`, so it never matches by formula.
        assert_eq!(parts(&ask("revenue"), &embedding, 1).0[2], 0.0);
        assert!(scores("revenue")[0] > 0.3);
        // A column is most like itself, then like what shares its lineage.
        let like_rev = like(&embedding, 1);
        let similar: Vec<f32> = (0..nodes.len())
            .map(|n| score(&like_rev, &embedding, n))
            .collect();
        assert!(
            similar[1] > similar[2] && similar[2] > similar[3],
            "{similar:?}"
        );
    }

    #[test]
    fn words_no_node_has_are_left_out_of_a_question() {
        let nodes = [column("t#amount", "amount", "", None)];
        let embedding = embed(&nodes, &keys(&nodes), &[], 1);
        let plain = question(&embedding, "amount");
        let padded = question(&embedding, "amount zzqx plugh");
        assert_eq!(plain.words.row(0), padded.words.row(0));
        assert!(question(&embedding, "zzqx").keys.is_empty());
        assert_eq!(most(&question(&embedding, "zzqx")), 0.0);
    }

    #[test]
    fn embedding_is_deterministic_and_does_not_depend_on_node_order() {
        let nodes = [
            column("a#x", "amount", "money", Some("a.b * 2")),
            column("a#b", "base", "", None),
            column("a#c", "cost", "", Some("a.x + a.b")),
        ];
        let links = [
            Link {
                source: 0,
                target: 1,
                kind: EdgeKind::DerivedFrom,
            },
            Link {
                source: 2,
                target: 0,
                kind: EdgeKind::DerivedFrom,
            },
            Link {
                source: 2,
                target: 1,
                kind: EdgeKind::DerivedFrom,
            },
        ];
        let one = embed(&nodes, &keys(&nodes), &links, 1);
        let again = embed(&nodes, &keys(&nodes), &links, 3);
        assert_eq!(one.sketches, again.sketches);
        let reversed: Vec<Node<'_>> = nodes.iter().rev().cloned().collect();
        let flip = |n: u32| 2 - n;
        let reversed_links: Vec<Link> = links
            .iter()
            .rev()
            .map(|l| Link {
                source: flip(l.source),
                target: flip(l.target),
                kind: l.kind,
            })
            .collect();
        let other = embed(&reversed, &keys(&reversed), &reversed_links, 2);
        for node in 0..3 {
            let mirror = 2 - node;
            assert_eq!(one.lineage.row(node), other.lineage.row(mirror));
            assert_eq!(one.formulas.row(node), other.formulas.row(mirror));
            assert_eq!(
                one.sketches[node * SKETCH..(node + 1) * SKETCH],
                other.sketches[mirror * SKETCH..(mirror + 1) * SKETCH]
            );
        }
        assert_eq!(quantise(&[0.0, 0.0]), (vec![0, 0], 1.0));
        assert_eq!(quantise(&[0.5, -1.0]).0, [64, -127]);
        assert_eq!(rarity(1), 1.0);
        assert_eq!(rarity(3), 0.5);
        assert_eq!(rarity(4), 1.0 / 3.0);
    }

    #[test]
    fn sparse_rows_meet_only_on_shared_features() {
        let rows = Sparse::of([
            vec![(4 << 32, 1.0), (9 << 32, 2.0)],
            vec![(1 << 32, 1.0), (9 << 32, 1.5)],
            vec![(3, 1.0)],
        ]);
        assert_eq!(rows.len(), 3);
        assert_eq!(sparse_dot(rows.row(0), rows.row(1)), (3.0, true));
        // A trigram alone is a feature, but not a whole one.
        assert_eq!(sparse_dot(rows.row(2), rows.row(2)), (1.0, false));
        assert_eq!(sparse_dot(rows.row(0), rows.row(2)), (0.0, false));
        assert!(Sparse::default().is_empty());
        assert_eq!(
            heaviest(vec![(1, 0.5), (2, 0.9), (3, 0.5), (4, 0.1)], 2),
            [(1, 0.5), (2, 0.9)]
        );
        assert_eq!(
            merge(vec![(2, 0.25), (1, 1.0), (2, 0.5)]),
            [(1, 1.0), (2, 0.75)]
        );
    }

    #[test]
    fn the_basis_is_the_largest_block() {
        assert_eq!(Basis::of([0.1, 0.3, 0.2, 0.0]), Basis::Lineage);
        assert_eq!(Basis::of([0.0, 0.0, 0.0, 0.0]), Basis::Words);
        assert_eq!(Basis::Structure.name(), "structure");
    }
}
