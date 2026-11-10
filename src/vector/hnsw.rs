//! A hierarchical navigable small world graph (Malkov and Yashunin, 2016) over quantised
//! vectors, built the same way on every machine.
//!
//! Each vector lives in a slot. A slot's level comes from its key alone (a hash of the node id),
//! so a node keeps its level however the index was built, and vectors are inserted in key order,
//! so a build does not depend on the order nodes arrive in. Similarities are exact integer inner
//! products scaled by two `f32` multiplications, and every tie is broken by slot number, so a
//! build is the same bytes on every machine and thread count.
//!
//! Large builds insert in batches. The search half of each insertion only reads the graph, so a
//! batch's searches run in parallel against the graph as it stood when the batch began; the
//! linking half then runs in key order, and each item also considers the items before it in
//! its own batch. Batches are small next to the graph, and their size depends only on how many
//! vectors are already in it, so the result does not depend on how many threads ran.
//!
//! Removal takes the removed slots out of every neighbour list at once and reconnects each
//! affected node to the best of its remaining neighbours and of the removed nodes' neighbours,
//! chosen by the same rule as an insertion.

use std::cmp::Ordering;
use std::collections::{BTreeSet, BinaryHeap};

use super::dot::dot;
use super::text::mix;

/// The highest level a node can have; reached by one key in 16^15.
const MAX_LEVEL: u8 = 15;

/// How the graph is built.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Params {
    /// Neighbours per node above level 0; level 0 keeps twice as many.
    pub m: usize,
    /// How many candidates an insertion searches for.
    pub ef_construction: usize,
}

impl Default for Params {
    fn default() -> Self {
        Self {
            m: 16,
            ef_construction: 64,
        }
    }
}

/// A slot and its similarity to the vector searched for. `Ord` puts the more similar first,
/// and among equals the lower slot.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Near {
    pub similarity: f32,
    pub slot: u32,
}

impl Eq for Near {}

impl Ord for Near {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .similarity
            .total_cmp(&self.similarity)
            .then(self.slot.cmp(&other.slot))
    }
}

impl PartialOrd for Near {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Wraps a `Near` so a `BinaryHeap` pops the most similar first.
#[derive(Clone, Copy, PartialEq, Eq)]
struct Best(Near);

impl Ord for Best {
    fn cmp(&self, other: &Self) -> Ordering {
        other.0.cmp(&self.0)
    }
}

impl PartialOrd for Best {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// The level of the node with `key`: 0 with probability 15/16, 1 with 15/256, and so on, for M
/// of 16.
pub fn level_of(key: u64) -> u8 {
    ((mix(key ^ 0x9e37_79b9_7f4a_7c15).leading_zeros() / 4) as u8).min(MAX_LEVEL)
}

/// The scratch space one search needs.
#[derive(Default)]
pub struct Scratch {
    visited: Vec<u32>,
    stamp: u32,
}

impl Scratch {
    fn begin(&mut self, slots: usize) {
        if self.visited.len() < slots {
            self.visited.resize(slots, 0);
        }
        self.stamp = self.stamp.wrapping_add(1);
        if self.stamp == 0 {
            self.visited.fill(0);
            self.stamp = 1;
        }
    }

    /// Marks `slot` visited; false when it already was.
    fn visit(&mut self, slot: u32) -> bool {
        let seen = &mut self.visited[slot as usize];
        if *seen == self.stamp {
            false
        } else {
            *seen = self.stamp;
            true
        }
    }
}

/// One slot as a saved index records it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SavedSlot {
    pub key: u64,
    pub live: bool,
    /// Neighbours at each level from 0 up to the slot's level.
    pub links: Vec<Vec<u32>>,
}

#[derive(Clone)]
pub struct Hnsw {
    dim: usize,
    params: Params,
    vectors: Vec<i8>,
    /// The reciprocal of each vector's length; 0 for an empty slot or a zero vector.
    inverse: Vec<f32>,
    keys: Vec<u64>,
    levels: Vec<u8>,
    live: Vec<bool>,
    /// Level-0 neighbours, room for `room(0)` per slot, with their counts.
    base: Vec<Near>,
    base_len: Vec<u8>,
    /// Neighbours at levels 1 and up, for the few slots that have them.
    upper: Vec<Vec<Vec<Near>>>,
    entry: Option<u32>,
    free: BTreeSet<u32>,
    count: usize,
}

impl std::fmt::Debug for Hnsw {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Hnsw")
            .field("dim", &self.dim)
            .field("count", &self.count)
            .field("slots", &self.keys.len())
            .finish()
    }
}

/// The reciprocal of a vector's length, computed exactly from its integer square sum.
pub fn inverse_norm(vector: &[i8]) -> f32 {
    let sum: i64 = vector.iter().map(|&x| i64::from(x) * i64::from(x)).sum();
    if sum == 0 {
        0.0
    } else {
        1.0 / (sum as f64).sqrt() as f32
    }
}

impl Hnsw {
    pub fn new(dim: usize, params: Params) -> Self {
        assert!(dim > 0 && dim <= super::dot::MAX_LENGTH);
        assert!(params.m >= 2 && params.m <= 127);
        Self {
            dim,
            params,
            vectors: Vec::new(),
            inverse: Vec::new(),
            keys: Vec::new(),
            levels: Vec::new(),
            live: Vec::new(),
            base: Vec::new(),
            base_len: Vec::new(),
            upper: Vec::new(),
            entry: None,
            free: BTreeSet::new(),
            count: 0,
        }
    }

    pub fn dim(&self) -> usize {
        self.dim
    }

    pub fn params(&self) -> Params {
        self.params
    }

    /// How many vectors the index holds.
    pub fn len(&self) -> usize {
        self.count
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }

    /// How many slots there are, live or free.
    pub fn slots(&self) -> usize {
        self.keys.len()
    }

    pub fn key(&self, slot: u32) -> u64 {
        self.keys[slot as usize]
    }

    pub fn is_live(&self, slot: u32) -> bool {
        self.live[slot as usize]
    }

    pub fn vector(&self, slot: u32) -> &[i8] {
        let at = slot as usize * self.dim;
        &self.vectors[at..at + self.dim]
    }

    pub fn entry(&self) -> Option<u32> {
        self.entry
    }

    fn m0(&self) -> usize {
        2 * self.params.m
    }

    /// How many neighbours a node chooses at `level`.
    fn cap(&self, level: usize) -> usize {
        if level == 0 { self.m0() } else { self.params.m }
    }

    /// How many neighbours a node may hold at `level` before they are chosen again. The slack
    /// means a full list is pruned once in every `cap / 2` new links, not on every one, which
    /// is most of what an insertion costs.
    fn room(&self, level: usize) -> usize {
        self.cap(level) + self.cap(level) / 2
    }

    /// A node's neighbours at `level`, by slot, each with its similarity to the node.
    fn near(&self, slot: u32, level: usize) -> &[Near] {
        let s = slot as usize;
        if level == 0 {
            let at = s * self.room(0);
            &self.base[at..at + self.base_len[s] as usize]
        } else {
            &self.upper[s][level - 1]
        }
    }

    /// A node's neighbours at `level`, in slot order.
    pub fn links(&self, slot: u32, level: usize) -> impl Iterator<Item = u32> + '_ {
        self.near(slot, level).iter().map(|n| n.slot)
    }

    /// Set a node's neighbours, kept in slot order so the lists do not depend on the order
    /// they were chosen in.
    fn set_links(&mut self, slot: u32, level: usize, links: &[Near]) {
        let mut sorted = links.to_vec();
        sorted.sort_unstable_by_key(|n| n.slot);
        let s = slot as usize;
        if level == 0 {
            let room = self.room(0);
            debug_assert!(sorted.len() <= room);
            self.base[s * room..s * room + sorted.len()].copy_from_slice(&sorted);
            self.base_len[s] = sorted.len() as u8;
        } else {
            self.upper[s][level - 1] = sorted;
        }
    }

    /// The similarity of a query (with its reciprocal length) to a slot's vector.
    #[inline]
    fn similarity(&self, query: &[i8], inverse: f32, slot: u32) -> f32 {
        dot(query, self.vector(slot)) as f32 * inverse * self.inverse[slot as usize]
    }

    fn between(&self, a: u32, b: u32) -> f32 {
        self.similarity(self.vector(a), self.inverse[a as usize], b)
    }

    /// Take a slot for `key`: the lowest free one, or a new one.
    fn allocate(&mut self, key: u64, vector: &[i8]) -> u32 {
        assert_eq!(vector.len(), self.dim);
        let level = level_of(key);
        let slot = match self.free.pop_first() {
            Some(slot) => slot,
            None => {
                let slot = self.keys.len() as u32;
                self.vectors.resize(self.vectors.len() + self.dim, 0);
                self.inverse.push(0.0);
                self.keys.push(0);
                self.levels.push(0);
                self.live.push(false);
                let room = self.room(0);
                self.base.resize(
                    self.base.len() + room,
                    Near {
                        similarity: 0.0,
                        slot: 0,
                    },
                );
                self.base_len.push(0);
                self.upper.push(Vec::new());
                slot
            }
        };
        let s = slot as usize;
        self.vectors[s * self.dim..(s + 1) * self.dim].copy_from_slice(vector);
        self.inverse[s] = inverse_norm(vector);
        self.keys[s] = key;
        self.levels[s] = level;
        self.live[s] = true;
        self.base_len[s] = 0;
        self.upper[s] = vec![Vec::new(); level as usize];
        self.count += 1;
        slot
    }

    /// The `ef` slots most similar to `query` at `level`, best first, from `entries`.
    fn search_layer(
        &self,
        query: &[i8],
        inverse: f32,
        entries: &[Near],
        ef: usize,
        level: usize,
        scratch: &mut Scratch,
    ) -> Vec<Near> {
        scratch.begin(self.keys.len());
        let mut candidates: BinaryHeap<Best> = BinaryHeap::with_capacity(ef * 2);
        // The results kept so far; the heap's top is the worst of them.
        let mut results: BinaryHeap<Near> = BinaryHeap::with_capacity(ef + 1);
        for &near in entries {
            if scratch.visit(near.slot) {
                candidates.push(Best(near));
                results.push(near);
                if results.len() > ef {
                    results.pop();
                }
            }
        }
        while let Some(Best(current)) = candidates.pop() {
            if let Some(worst) = results.peek()
                && results.len() >= ef
                && current > *worst
            {
                break;
            }
            for neighbour in self.links(current.slot, level) {
                if !scratch.visit(neighbour) {
                    continue;
                }
                let near = Near {
                    similarity: self.similarity(query, inverse, neighbour),
                    slot: neighbour,
                };
                if results.len() < ef || results.peek().is_some_and(|worst| near < *worst) {
                    candidates.push(Best(near));
                    results.push(near);
                    if results.len() > ef {
                        results.pop();
                    }
                }
            }
        }
        results.into_sorted_vec()
    }

    /// From `candidates`, best first, the neighbours to keep: each closer to the node than to
    /// any neighbour kept before it, which spreads the links in every direction; then, at level
    /// 0, the best of the rest until the list is full.
    fn select(&self, candidates: &[Near], cap: usize, fill: bool) -> Vec<Near> {
        let mut kept: Vec<Near> = Vec::with_capacity(cap);
        let mut passed: Vec<Near> = Vec::new();
        for &candidate in candidates {
            if kept.len() >= cap {
                break;
            }
            let diverse = kept
                .iter()
                .all(|k| self.between(candidate.slot, k.slot) < candidate.similarity);
            if diverse {
                kept.push(candidate);
            } else if fill {
                passed.push(candidate);
            }
        }
        if fill {
            for candidate in passed {
                if kept.len() >= cap {
                    break;
                }
                kept.push(candidate);
            }
        }
        kept
    }

    /// The candidate neighbours of a new vector at each level up to `level`, from a search of
    /// the graph as it stands. Reads only.
    fn plan(&self, vector: &[i8], level: u8, scratch: &mut Scratch) -> Vec<Vec<Near>> {
        let Some(entry) = self.entry else {
            return vec![Vec::new(); level as usize + 1];
        };
        let inverse = inverse_norm(vector);
        let top = self.levels[entry as usize];
        let mut current = vec![Near {
            similarity: self.similarity(vector, inverse, entry),
            slot: entry,
        }];
        let mut planned = vec![Vec::new(); level as usize + 1];
        for l in (0..=top as usize).rev() {
            let ef = if l > level as usize {
                1
            } else {
                self.params.ef_construction
            };
            let found = self.search_layer(vector, inverse, &current, ef, l, scratch);
            if l <= level as usize {
                planned[l] = found.clone();
            }
            current = found;
        }
        planned
    }

    /// Link a newly allocated slot, given its planned candidates and the slots inserted before
    /// it in the same batch.
    fn link(&mut self, slot: u32, mut planned: Vec<Vec<Near>>, batch: &[u32]) {
        let level = self.levels[slot as usize];
        for &other in batch {
            let similarity = self.between(slot, other);
            for (l, candidates) in planned.iter_mut().enumerate() {
                if l <= self.levels[other as usize] as usize {
                    candidates.push(Near {
                        similarity,
                        slot: other,
                    });
                }
            }
        }
        for (l, mut candidates) in planned.into_iter().enumerate() {
            candidates.sort_unstable();
            candidates.dedup_by_key(|n| n.slot);
            candidates.retain(|n| n.slot != slot);
            // As many candidates as a search would have found, so the batch's own members do
            // not make the choice slower than a lone insertion's.
            candidates.truncate(self.params.ef_construction);
            // A new node chooses `m` neighbours at every level; level 0 keeps room for twice as
            // many, which the links of later nodes fill.
            let chosen = self.select(&candidates, self.params.m, false);
            self.set_links(slot, l, &chosen);
            for near in chosen {
                self.connect(
                    near.slot,
                    Near {
                        similarity: near.similarity,
                        slot,
                    },
                    l,
                );
            }
        }
        let higher = self
            .entry
            .is_none_or(|entry| level > self.levels[entry as usize]);
        if higher {
            self.entry = Some(slot);
        }
    }

    /// Add `new` to `node`'s neighbours at `level`, choosing again when the list is full.
    fn connect(&mut self, node: u32, new: Near, level: usize) {
        let current = self.near(node, level);
        let Err(at) = current.binary_search_by_key(&new.slot, |n| n.slot) else {
            return;
        };
        let length = current.len();
        if length < self.room(level) {
            // Room left: insert in slot order, in place.
            let s = node as usize;
            if level == 0 {
                let start = s * self.room(0);
                let list = &mut self.base[start..start + length + 1];
                list.copy_within(at..length, at + 1);
                list[at] = new;
                self.base_len[s] += 1;
            } else {
                self.upper[s][level - 1].insert(at, new);
            }
            return;
        }
        let mut grown = current.to_vec();
        grown.push(new);
        grown.sort_unstable();
        let chosen = self.select(&grown, self.cap(level), level == 0);
        self.set_links(node, level, &chosen);
    }

    /// Insert one vector under `key`, returning its slot.
    pub fn insert(&mut self, key: u64, vector: &[i8]) -> u32 {
        let mut scratch = Scratch::default();
        let planned = self.plan(vector, level_of(key), &mut scratch);
        let slot = self.allocate(key, vector);
        self.link(slot, planned, &[]);
        slot
    }

    /// The batch size for an index already holding `count` vectors.
    fn batch_size(count: usize) -> usize {
        (count / 32).clamp(1, 256)
    }

    /// Insert many vectors, in key order, using up to `threads` threads; returns each item's
    /// slot in the order given. The result is the same whatever `threads` is.
    pub fn insert_all(&mut self, items: &[(u64, &[i8])], threads: usize) -> Vec<u32> {
        let mut order: Vec<usize> = (0..items.len()).collect();
        order.sort_unstable_by_key(|&i| items[i].0);
        let mut slots = vec![0u32; items.len()];
        let mut done = 0;
        while done < order.len() {
            let size = Self::batch_size(self.count).min(order.len() - done);
            let batch = &order[done..done + size];
            let plans = self.plan_batch(items, batch, threads);
            let mut placed: Vec<u32> = Vec::with_capacity(size);
            for (&i, plan) in batch.iter().zip(plans) {
                let slot = self.allocate(items[i].0, items[i].1);
                self.link(slot, plan, &placed);
                placed.push(slot);
                slots[i] = slot;
            }
            done += size;
        }
        slots
    }

    fn plan_batch(
        &self,
        items: &[(u64, &[i8])],
        batch: &[usize],
        threads: usize,
    ) -> Vec<Vec<Vec<Near>>> {
        let threads = threads.clamp(1, batch.len().div_ceil(16).max(1));
        if threads == 1 {
            let mut scratch = Scratch::default();
            return batch
                .iter()
                .map(|&i| self.plan(items[i].1, level_of(items[i].0), &mut scratch))
                .collect();
        }
        let chunk = batch.len().div_ceil(threads);
        std::thread::scope(|scope| {
            let handles: Vec<_> = batch
                .chunks(chunk)
                .map(|part| {
                    scope.spawn(move || {
                        let mut scratch = Scratch::default();
                        part.iter()
                            .map(|&i| self.plan(items[i].1, level_of(items[i].0), &mut scratch))
                            .collect::<Vec<_>>()
                    })
                })
                .collect();
            handles
                .into_iter()
                .flat_map(|h| h.join().expect("an index thread panicked"))
                .collect()
        })
    }

    /// Remove the vectors in `slots`, reconnecting the nodes that linked to them.
    pub fn remove(&mut self, slots: &[u32]) {
        let removed: BTreeSet<u32> = slots
            .iter()
            .copied()
            .filter(|&s| (s as usize) < self.live.len() && self.live[s as usize])
            .collect();
        if removed.is_empty() {
            return;
        }
        for &slot in &removed {
            self.live[slot as usize] = false;
        }
        for node in 0..self.keys.len() as u32 {
            if !self.live[node as usize] {
                continue;
            }
            for level in 0..=self.levels[node as usize] as usize {
                let current = self.near(node, level);
                if !current.iter().any(|n| removed.contains(&n.slot)) {
                    continue;
                }
                let mut pool: Vec<u32> = Vec::new();
                for n in current {
                    if removed.contains(&n.slot) {
                        pool.extend(
                            self.links(n.slot, level)
                                .filter(|&m| m != node && self.live[m as usize]),
                        );
                    } else {
                        pool.push(n.slot);
                    }
                }
                pool.sort_unstable();
                pool.dedup();
                let mut candidates: Vec<Near> = pool
                    .into_iter()
                    .map(|other| Near {
                        similarity: self.between(node, other),
                        slot: other,
                    })
                    .collect();
                candidates.sort_unstable();
                let chosen = self.select(&candidates, self.cap(level), level == 0);
                self.set_links(node, level, &chosen);
            }
        }
        for &slot in &removed {
            let s = slot as usize;
            self.base_len[s] = 0;
            self.upper[s].clear();
            self.levels[s] = 0;
            self.inverse[s] = 0.0;
            self.keys[s] = 0;
            self.vectors[s * self.dim..(s + 1) * self.dim].fill(0);
            self.free.insert(slot);
        }
        self.count -= removed.len();
        if self.entry.is_some_and(|e| removed.contains(&e)) {
            // The highest live node, the lowest key among equals.
            self.entry = (0..self.keys.len() as u32)
                .filter(|&s| self.live[s as usize])
                .max_by(|&a, &b| {
                    self.levels[a as usize]
                        .cmp(&self.levels[b as usize])
                        .then(self.keys[b as usize].cmp(&self.keys[a as usize]))
                });
        }
    }

    /// The `k` live vectors most similar to `query`, best first, searching `ef` candidates.
    pub fn search(&self, query: &[i8], k: usize, ef: usize, scratch: &mut Scratch) -> Vec<Near> {
        let Some(entry) = self.entry else {
            return Vec::new();
        };
        let inverse = inverse_norm(query);
        let mut current = vec![Near {
            similarity: self.similarity(query, inverse, entry),
            slot: entry,
        }];
        for level in (1..=self.levels[entry as usize] as usize).rev() {
            current = self.search_layer(query, inverse, &current, 1, level, scratch);
        }
        let mut found = self.search_layer(query, inverse, &current, ef.max(k), 0, scratch);
        found.truncate(k);
        found
    }

    /// Every live vector compared with `query`, keeping the `k` best that `keep` accepts.
    pub fn exact(&self, query: &[i8], k: usize, keep: impl Fn(u32) -> bool) -> Vec<Near> {
        let inverse = inverse_norm(query);
        let mut best: BinaryHeap<Near> = BinaryHeap::with_capacity(k + 1);
        for slot in 0..self.keys.len() as u32 {
            if !self.live[slot as usize] || !keep(slot) {
                continue;
            }
            let near = Near {
                similarity: self.similarity(query, inverse, slot),
                slot,
            };
            if best.len() < k || best.peek().is_some_and(|worst| near < *worst) {
                best.push(near);
                if best.len() > k {
                    best.pop();
                }
            }
        }
        best.into_sorted_vec()
    }

    /// The slots as a saved index records them.
    pub fn saved(&self) -> Vec<SavedSlot> {
        (0..self.keys.len() as u32)
            .map(|slot| SavedSlot {
                key: self.keys[slot as usize],
                live: self.live[slot as usize],
                links: if self.live[slot as usize] {
                    (0..=self.levels[slot as usize] as usize)
                        .map(|l| self.links(slot, l).collect())
                        .collect()
                } else {
                    Vec::new()
                },
            })
            .collect()
    }

    /// An index from saved slots and the vector each live slot holds now. A slot whose
    /// `vectors` entry is None is live but out of date: it has its links, so that `remove` can
    /// reconnect its neighbours, and should be removed straight away.
    pub fn restore(
        dim: usize,
        params: Params,
        slots: Vec<SavedSlot>,
        vectors: &[Option<&[i8]>],
        entry: Option<u32>,
    ) -> Self {
        let mut index = Self::new(dim, params);
        let zero = vec![0i8; dim];
        let mut saved_links = Vec::with_capacity(slots.len());
        for (slot, mut saved) in slots.into_iter().enumerate() {
            saved_links.push(std::mem::take(&mut saved.links));
            let slot = slot as u32;
            index.allocate(saved.key, vectors[slot as usize].unwrap_or(&zero));
            if !saved.live {
                index.live[slot as usize] = false;
                index.keys[slot as usize] = 0;
                index.levels[slot as usize] = 0;
                index.upper[slot as usize].clear();
                index.inverse[slot as usize] = 0.0;
                index.free.insert(slot);
                index.count -= 1;
                continue;
            }
        }
        // Similarities once every vector is in place.
        for (slot, saved) in saved_links.into_iter().enumerate() {
            let slot = slot as u32;
            if !index.live[slot as usize] {
                continue;
            }
            for (level, links) in saved.into_iter().enumerate() {
                if level <= index.levels[slot as usize] as usize {
                    let room = index.room(level);
                    let near: Vec<Near> = links
                        .into_iter()
                        .filter(|&other| (other as usize) < index.keys.len() && other != slot)
                        .take(room)
                        .map(|other| Near {
                            similarity: index.between(slot, other),
                            slot: other,
                        })
                        .collect();
                    index.set_links(slot, level, &near);
                }
            }
        }
        index.entry = entry.filter(|&e| index.live.get(e as usize).copied().unwrap_or(false));
        index
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// SplitMix64, for reproducible test data.
    pub struct Random(pub u64);

    impl Random {
        pub fn next(&mut self) -> u64 {
            self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
            mix(self.0)
        }

        /// A vector of `dim` values from -127 to 127, roughly Gaussian (the sum of four
        /// uniform draws) around `centre`.
        pub fn vector(&mut self, dim: usize, centre: Option<&[i8]>) -> Vec<i8> {
            (0..dim)
                .map(|i| {
                    let noise: i32 = (0..4).map(|_| (self.next() >> 58) as i32 - 32).sum();
                    let base = centre.map_or(0, |c| i32::from(c[i]));
                    (base + noise).clamp(-127, 127) as i8
                })
                .collect()
        }
    }

    /// Vectors on a random `latent`-dimensional subspace, with a little noise: random data
    /// with the low intrinsic dimension real embeddings have.
    pub fn subspace_vectors(count: usize, dim: usize, latent: usize, seed: u64) -> Vec<Vec<i8>> {
        let mut random = Random(seed);
        // The basis is the same for every seed, so queries and data share the subspace.
        let mut basis_random = Random(0x5eed);
        let basis: Vec<Vec<f32>> = (0..latent)
            .map(|_| {
                (0..dim)
                    .map(|_| (basis_random.next() >> 40) as f32 / (1u64 << 24) as f32 - 0.5)
                    .collect()
            })
            .collect();
        (0..count)
            .map(|_| {
                let weights: Vec<f32> = (0..latent)
                    .map(|_| (random.next() >> 40) as f32 / (1u64 << 24) as f32 - 0.5)
                    .collect();
                (0..dim)
                    .map(|i| {
                        let value: f32 = weights.iter().zip(&basis).map(|(w, b)| w * b[i]).sum();
                        let noise = ((random.next() >> 61) as f32) - 3.5;
                        (value * 120.0 + noise).clamp(-127.0, 127.0) as i8
                    })
                    .collect()
            })
            .collect()
    }

    pub fn random_vectors(count: usize, dim: usize, clusters: usize, seed: u64) -> Vec<Vec<i8>> {
        if clusters >= 1000 {
            return subspace_vectors(count, dim, clusters - 1000, seed);
        }
        let mut random = Random(seed);
        let centres: Vec<Vec<i8>> = (0..clusters)
            .map(|_| {
                (0..dim)
                    .map(|_| ((random.next() >> 57) as i32 - 64) as i8)
                    .collect()
            })
            .collect();
        (0..count)
            .map(|_| {
                let centre = (!centres.is_empty())
                    .then(|| &centres[(random.next() % clusters as u64) as usize][..]);
                random.vector(dim, centre)
            })
            .collect()
    }

    fn oracle(vectors: &[(u64, Vec<i8>)], query: &[i8], k: usize) -> Vec<u64> {
        let qi = inverse_norm(query);
        let mut all: Vec<(f32, u64)> = vectors
            .iter()
            .map(|(key, v)| (dot(query, v) as f32 * qi * inverse_norm(v), *key))
            .collect();
        all.sort_by(|a, b| b.0.total_cmp(&a.0).then(a.1.cmp(&b.1)));
        all.into_iter().take(k).map(|(_, key)| key).collect()
    }

    fn recall(
        index: &Hnsw,
        vectors: &[(u64, Vec<i8>)],
        queries: &[Vec<i8>],
        k: usize,
        ef: usize,
    ) -> f64 {
        let mut scratch = Scratch::default();
        let mut hits = 0;
        for query in queries {
            let truth = oracle(vectors, query, k);
            let found: Vec<u64> = index
                .search(query, k, ef, &mut scratch)
                .iter()
                .map(|n| index.key(n.slot))
                .collect();
            hits += truth.iter().filter(|key| found.contains(key)).count();
        }
        hits as f64 / (queries.len() * k) as f64
    }

    fn keyed(vectors: Vec<Vec<i8>>, seed: u64) -> Vec<(u64, Vec<i8>)> {
        vectors
            .into_iter()
            .enumerate()
            .map(|(i, v)| (mix(i as u64 ^ seed), v))
            .collect()
    }

    fn build(vectors: &[(u64, Vec<i8>)], threads: usize) -> Hnsw {
        let mut index = Hnsw::new(vectors[0].1.len(), Params::default());
        let items: Vec<(u64, &[i8])> = vectors.iter().map(|(k, v)| (*k, &v[..])).collect();
        index.insert_all(&items, threads);
        index
    }

    #[test]
    fn recall_against_brute_force_on_random_vectors() {
        // Uniform random vectors have no structure to find, so they need the widest search;
        // vectors on a low-dimensional subspace, like real embeddings, need much less.
        for (count, dim, kind, ef) in [
            (3000, 64, 0, 96),
            (2000, 256, 0, 200),
            (4000, 256, 1012, 64),
            (4000, 256, 1024, 96),
        ] {
            let vectors = keyed(random_vectors(count, dim, kind, 1), 11);
            let queries = random_vectors(200, dim, kind, 2);
            let index = build(&vectors, 4);
            assert_eq!(index.len(), count);
            let r = recall(&index, &vectors, &queries, 10, ef);
            assert!(
                r >= 0.98,
                "recall {r} for {count} vectors of {dim} ({kind}) at ef {ef}"
            );
        }
    }

    #[test]
    fn builds_do_not_depend_on_threads_or_arrival_order() {
        let vectors = keyed(random_vectors(1500, 64, 10, 3), 5);
        let one = build(&vectors, 1);
        let four = build(&vectors, 4);
        let mut reversed = vectors.clone();
        reversed.reverse();
        let shuffled = build(&reversed, 3);
        assert_eq!(one.saved(), four.saved());
        // Slots follow key order, so a reversed arrival gives the same graph.
        assert_eq!(one.saved(), shuffled.saved());
        assert_eq!(one.entry(), four.entry());
    }

    #[test]
    fn single_inserts_find_what_they_added() {
        let vectors = keyed(random_vectors(400, 32, 0, 4), 9);
        let mut index = Hnsw::new(32, Params::default());
        let mut scratch = Scratch::default();
        for (key, vector) in &vectors {
            let slot = index.insert(*key, vector);
            assert_eq!(index.search(vector, 1, 32, &mut scratch)[0].slot, slot);
        }
        assert!(recall(&index, &vectors, &random_vectors(50, 32, 0, 5), 10, 96) >= 0.98);
    }

    #[test]
    fn incremental_updates_match_a_full_rebuild() {
        let all = keyed(random_vectors(3000, 128, 1016, 6), 13);
        let (first, later) = all.split_at(2400);
        let mut index = build(first, 4);
        // Remove every fifth vector of the first build, then add the rest.
        let gone: Vec<u32> = (0..index.slots() as u32)
            .filter(|&s| index.key(s).is_multiple_of(5))
            .collect();
        let gone_keys: Vec<u64> = gone.iter().map(|&s| index.key(s)).collect();
        index.remove(&gone);
        let items: Vec<(u64, &[i8])> = later.iter().map(|(k, v)| (*k, &v[..])).collect();
        index.insert_all(&items, 2);
        let remaining: Vec<(u64, Vec<i8>)> = all
            .iter()
            .filter(|(k, _)| !gone_keys.contains(k))
            .cloned()
            .collect();
        assert_eq!(index.len(), remaining.len());
        let full = build(&remaining, 4);
        // The same vectors under the same keys,
        let live = |index: &Hnsw| {
            let mut found: Vec<(u64, Vec<i8>)> = (0..index.slots() as u32)
                .filter(|&s| index.is_live(s))
                .map(|s| (index.key(s), index.vector(s).to_vec()))
                .collect();
            found.sort();
            found
        };
        assert_eq!(live(&index), live(&full));
        // every slot reachable from the entry point,
        for idx in [&index, &full] {
            let mut seen = vec![false; idx.slots()];
            let mut stack = vec![idx.entry().unwrap()];
            while let Some(s) = stack.pop() {
                if std::mem::replace(&mut seen[s as usize], true) {
                    continue;
                }
                for level in 0..=level_of(idx.key(s)) as usize {
                    stack.extend(idx.links(s, level));
                }
            }
            let reached = (0..idx.slots() as u32)
                .filter(|&s| seen[s as usize])
                .count();
            assert_eq!(reached, idx.len());
        }
        // and the same answers.
        let queries = random_vectors(200, 128, 1016, 7);
        let (r_incremental, r_full) = (
            recall(&index, &remaining, &queries, 10, 96),
            recall(&full, &remaining, &queries, 10, 96),
        );
        assert!(
            r_incremental >= 0.98 && r_full >= 0.98,
            "{r_incremental} {r_full}"
        );
        let mut scratch = Scratch::default();
        let mut same = 0;
        for query in &queries {
            let a: Vec<u64> = index
                .search(query, 10, 96, &mut scratch)
                .iter()
                .map(|n| index.key(n.slot))
                .collect();
            let b: Vec<u64> = full
                .search(query, 10, 96, &mut scratch)
                .iter()
                .map(|n| full.key(n.slot))
                .collect();
            same += a.iter().filter(|k| b.contains(k)).count();
        }
        assert!(same as f64 / 2000.0 >= 0.98, "{same}");
        // Freed slots are reused before new ones are made.
        assert_eq!(index.slots(), index.len());
    }

    #[test]
    fn removing_everything_and_the_entry_point() {
        let vectors = keyed(random_vectors(300, 32, 0, 8), 1);
        let mut index = build(&vectors, 1);
        let entry = index.entry().unwrap();
        index.remove(&[entry]);
        assert_ne!(index.entry(), Some(entry));
        assert_eq!(index.len(), 299);
        let mut scratch = Scratch::default();
        assert_eq!(index.search(&vectors[0].1, 5, 32, &mut scratch).len(), 5);
        let all: Vec<u32> = (0..index.slots() as u32).collect();
        index.remove(&all);
        assert!(index.is_empty());
        assert_eq!(index.entry(), None);
        assert!(index.search(&vectors[0].1, 5, 32, &mut scratch).is_empty());
        assert!(index.exact(&vectors[0].1, 5, |_| true).is_empty());
    }

    #[test]
    fn exact_search_filters_and_ranks() {
        let vectors = keyed(random_vectors(200, 32, 0, 9), 2);
        let index = build(&vectors, 1);
        let even = index.exact(&vectors[3].1, 4, |s| s % 2 == 0);
        assert_eq!(even.len(), 4);
        assert!(even.windows(2).all(|w| w[0] <= w[1]));
        assert!(even.iter().all(|n| n.slot % 2 == 0));
    }

    #[test]
    fn saved_slots_restore_the_same_index() {
        let vectors = keyed(random_vectors(500, 32, 5, 10), 3);
        let index = build(&vectors, 2);
        let saved = index.saved();
        let current: Vec<Option<&[i8]>> = (0..index.slots() as u32)
            .map(|s| index.is_live(s).then(|| index.vector(s)))
            .collect();
        let restored = Hnsw::restore(
            32,
            Params::default(),
            saved.clone(),
            &current,
            index.entry(),
        );
        assert_eq!(restored.saved(), saved);
        let mut scratch = Scratch::default();
        assert_eq!(
            restored.search(&vectors[0].1, 5, 32, &mut scratch),
            index.search(&vectors[0].1, 5, 32, &mut scratch)
        );
    }

    #[test]
    fn levels_come_from_keys() {
        let levels: Vec<u8> = (0..160_000u64).map(level_of).collect();
        let above = levels.iter().filter(|&&l| l >= 1).count();
        // One in sixteen, give or take.
        assert!((8_000..12_000).contains(&above), "{above}");
        assert_eq!(level_of(42), level_of(42));
    }
}
