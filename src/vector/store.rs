//! The saved index: a compact binary file beside `graph.json`.
//!
//! The file holds the graph of the index and every node's blocks. For each slot it records the
//! node's key (a hash of its id) and a hash of the vector the slot was linked with, so a reader
//! whose graph has changed knows which slots still hold the vector their node has now. The
//! blocks come with a digest of everything they were computed from ([`super::embed::fingerprint`]):
//! a reader whose graph has the same digest takes them as they are rather than embedding the
//! graph again, and any other reader computes its own.
//!
//! Layout, little-endian throughout:
//!
//! ```text
//! magic "KVEC"  u32 format  u32 embedding version  u16 dimensions  u16 m  u16 ef_construction
//! u16 0  u32 slots  u32 entry point (all ones when empty)
//! for each slot: u64 key (0 when free), u64 vector hash
//! for each live slot, for each of its levels: varint count, then varint gaps between the
//!   sorted neighbour slots
//! u64 fingerprint of the embedded graph
//! u32 features, then each feature's u64 hash, ascending
//! u32 nodes, then for each, by key: u64 key, the sketch (i8 each), the structure block
//!   (i8 each) and f32 scale, then the words, lineage and formula rows, each a varint count
//!   and, for each feature, a varint gap between feature numbers and an f32 weight
//! u64 FNV-1a of everything before it
//! ```

use crate::fnv;

use super::embed::{NodeRows, SKETCH, STRUCTURE};
use super::hnsw::{Params, SavedSlot, level_of};
use super::text::Feature;

const MAGIC: &[u8; 4] = b"KVEC";
pub const FORMAT: u32 = 2;

/// A saved index, as read.
#[derive(Debug, PartialEq)]
pub struct Saved {
    pub version: u32,
    pub dim: usize,
    pub params: Params,
    pub entry: Option<u32>,
    pub slots: Vec<SavedSlot>,
    pub hashes: Vec<u64>,
    /// The digest of the graph the rows were computed from.
    pub fingerprint: u64,
    /// Each node's key and blocks, by key.
    pub rows: Vec<(u64, NodeRows)>,
    /// Every feature of the rows, ascending.
    pub features: Vec<u64>,
}

fn varint(out: &mut Vec<u8>, mut value: u32) {
    while value >= 0x80 {
        out.push((value as u8) | 0x80);
        value >>= 7;
    }
    out.push(value as u8);
}

pub fn write(saved: &Saved) -> Vec<u8> {
    let mut out = Vec::with_capacity(32 + saved.slots.len() * 60);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&FORMAT.to_le_bytes());
    out.extend_from_slice(&saved.version.to_le_bytes());
    for value in [saved.dim, saved.params.m, saved.params.ef_construction, 0] {
        out.extend_from_slice(&(value as u16).to_le_bytes());
    }
    out.extend_from_slice(&(saved.slots.len() as u32).to_le_bytes());
    out.extend_from_slice(&saved.entry.unwrap_or(u32::MAX).to_le_bytes());
    for (slot, hash) in saved.slots.iter().zip(&saved.hashes) {
        let (key, hash) = if slot.live { (slot.key, *hash) } else { (0, 0) };
        out.extend_from_slice(&key.to_le_bytes());
        out.extend_from_slice(&hash.to_le_bytes());
    }
    for slot in saved.slots.iter().filter(|s| s.live) {
        for links in &slot.links {
            let mut sorted = links.clone();
            sorted.sort_unstable();
            varint(&mut out, sorted.len() as u32);
            let mut previous = 0;
            for link in sorted {
                varint(&mut out, link - previous);
                previous = link;
            }
        }
    }
    out.extend_from_slice(&saved.fingerprint.to_le_bytes());
    // The features the rows use, whatever `saved.features` says.
    let mut features: Vec<u64> = saved
        .rows
        .iter()
        .flat_map(|(_, rows)| [&rows.words, &rows.lineage, &rows.formula])
        .flat_map(|row| row.iter().map(|f| f.0))
        .collect();
    features.sort_unstable();
    features.dedup();
    out.extend_from_slice(&(features.len() as u32).to_le_bytes());
    for feature in &features {
        out.extend_from_slice(&feature.to_le_bytes());
    }
    out.extend_from_slice(&(saved.rows.len() as u32).to_le_bytes());
    for (key, rows) in &saved.rows {
        out.extend_from_slice(&key.to_le_bytes());
        out.extend(rows.sketch.iter().chain(&rows.structure).map(|&x| x as u8));
        out.extend_from_slice(&rows.scale.to_le_bytes());
        for row in [&rows.words, &rows.lineage, &rows.formula] {
            varint(&mut out, row.len() as u32);
            let mut previous = 0u32;
            for &(feature, weight) in row {
                // Rows are in feature order, so their numbers only grow.
                let number = features.binary_search(&feature).expect("a listed feature") as u32;
                varint(&mut out, number - previous);
                previous = number;
                out.extend_from_slice(&weight.to_le_bytes());
            }
        }
    }
    let checksum = fnv::hash(fnv::OFFSET_BASIS, &out);
    out.extend_from_slice(&checksum.to_le_bytes());
    out
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn take<const N: usize>(&mut self) -> Option<[u8; N]> {
        let slice = self.bytes.get(self.at..self.at + N)?;
        self.at += N;
        slice.try_into().ok()
    }

    fn u16(&mut self) -> Option<u16> {
        self.take().map(u16::from_le_bytes)
    }

    fn u32(&mut self) -> Option<u32> {
        self.take().map(u32::from_le_bytes)
    }

    fn u64(&mut self) -> Option<u64> {
        self.take().map(u64::from_le_bytes)
    }

    fn f32(&mut self) -> Option<f32> {
        self.take().map(f32::from_le_bytes)
    }

    fn bytes(&mut self, count: usize) -> Option<Vec<i8>> {
        let slice = self.bytes.get(self.at..self.at + count)?;
        self.at += count;
        Some(slice.iter().map(|&b| b as i8).collect())
    }

    /// A row of `(feature, weight)`, its features numbered in `features`.
    fn row(&mut self, features: &[u64]) -> Option<Vec<Feature>> {
        let length = self.varint()? as usize;
        if length > self.bytes.len() - self.at {
            return None;
        }
        let mut row = Vec::with_capacity(length);
        let mut number = 0u32;
        for at in 0..length {
            let gap = self.varint()?;
            if at > 0 && gap == 0 {
                return None;
            }
            number = number.checked_add(gap)?;
            row.push((*features.get(number as usize)?, self.f32()?));
        }
        Some(row)
    }

    fn varint(&mut self) -> Option<u32> {
        let mut value = 0u32;
        for shift in (0..35).step_by(7) {
            let byte = *self.bytes.get(self.at)?;
            self.at += 1;
            value |= u32::from(byte & 0x7f).checked_shl(shift)?;
            if byte & 0x80 == 0 {
                return Some(value);
            }
        }
        None
    }
}

/// The saved index in `bytes`, or None when they are not one this build can read: another
/// format, a damaged file or a truncated one.
pub fn read(bytes: &[u8]) -> Option<Saved> {
    let body = bytes.len().checked_sub(8)?;
    let checksum = u64::from_le_bytes(bytes[body..].try_into().ok()?);
    if fnv::hash(fnv::OFFSET_BASIS, &bytes[..body]) != checksum {
        return None;
    }
    let mut reader = Reader {
        bytes: &bytes[..body],
        at: 0,
    };
    if &reader.take::<4>()? != MAGIC || reader.u32()? != FORMAT {
        return None;
    }
    let version = reader.u32()?;
    let dim = reader.u16()? as usize;
    let params = Params {
        m: reader.u16()? as usize,
        ef_construction: reader.u16()? as usize,
    };
    reader.u16()?;
    let count = reader.u32()? as usize;
    let entry = Some(reader.u32()?).filter(|&e| e != u32::MAX);
    if count > body / 16 {
        return None;
    }
    let mut slots = Vec::with_capacity(count);
    let mut hashes = Vec::with_capacity(count);
    for _ in 0..count {
        let key = reader.u64()?;
        hashes.push(reader.u64()?);
        slots.push(SavedSlot {
            key,
            live: key != 0,
            links: Vec::new(),
        });
    }
    for slot in slots.iter_mut().filter(|s| s.live) {
        for _ in 0..=level_of(slot.key) {
            let length = reader.varint()? as usize;
            if length > 1024 {
                return None;
            }
            let mut links = Vec::with_capacity(length);
            let mut previous = 0u32;
            for _ in 0..length {
                previous = previous.checked_add(reader.varint()?)?;
                if previous as usize >= count {
                    return None;
                }
                links.push(previous);
            }
            slot.links.push(links);
        }
    }
    let fingerprint = reader.u64()?;
    let feature_count = reader.u32()? as usize;
    if feature_count > body / 8 {
        return None;
    }
    let features: Vec<u64> = (0..feature_count)
        .map(|_| reader.u64())
        .collect::<Option<_>>()?;
    if features.windows(2).any(|pair| pair[0] >= pair[1]) {
        return None;
    }
    let node_count = reader.u32()? as usize;
    if node_count > body / (8 + SKETCH + STRUCTURE + 4 + 3) {
        return None;
    }
    let mut rows = Vec::with_capacity(node_count);
    for _ in 0..node_count {
        let key = reader.u64()?;
        let sketch = reader.bytes(SKETCH)?;
        let structure = reader.bytes(STRUCTURE)?;
        let scale = reader.f32()?;
        let words = reader.row(&features)?;
        let lineage = reader.row(&features)?;
        let formula = reader.row(&features)?;
        rows.push((
            key,
            NodeRows {
                words,
                lineage,
                formula,
                structure,
                scale,
                sketch,
            },
        ));
    }
    (reader.at == body).then_some(Saved {
        version,
        dim,
        params,
        entry,
        slots,
        hashes,
        fingerprint,
        rows,
        features,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Saved {
        let keys = [
            0x8000_0000_0000_0001u64,
            0,
            0x8000_0000_0000_0042,
            1 << 63 | 7,
        ];
        let slots: Vec<SavedSlot> = keys
            .iter()
            .enumerate()
            .map(|(i, &key)| SavedSlot {
                key,
                live: key != 0,
                links: if key == 0 {
                    Vec::new()
                } else {
                    (0..=level_of(key))
                        .map(|l| vec![(i as u32 + 1 + l as u32) % 4, 300 % 4])
                        .collect()
                },
            })
            .collect();
        Saved {
            version: 3,
            dim: 256,
            params: Params::default(),
            entry: Some(0),
            hashes: vec![11, 0, 13, 14],
            slots,
            fingerprint: 0x1234_5678_9abc_def0,
            features: vec![1, 5, 900],
            rows: vec![
                (
                    3,
                    NodeRows {
                        words: vec![(5, 0.5), (900, -0.25)],
                        lineage: Vec::new(),
                        formula: vec![(5, 1.0)],
                        structure: (0..STRUCTURE as i8).collect(),
                        scale: 2.5,
                        sketch: (0..SKETCH as i8).map(|x| -x).collect(),
                    },
                ),
                (
                    7,
                    NodeRows {
                        words: vec![(1, 0.125)],
                        lineage: vec![(900, 0.75)],
                        formula: Vec::new(),
                        structure: vec![-128; STRUCTURE],
                        scale: 1.0,
                        sketch: vec![127; SKETCH],
                    },
                ),
            ],
        }
    }

    #[test]
    fn a_saved_index_reads_back() {
        let saved = sample();
        let bytes = write(&saved);
        let mut read_back = read(&bytes).expect("readable");
        // Links come back sorted.
        for slot in &mut read_back.slots {
            for links in &mut slot.links {
                links.sort_unstable();
            }
        }
        let mut expected = sample();
        for slot in &mut expected.slots {
            for links in &mut slot.links {
                links.sort_unstable();
            }
        }
        assert_eq!(read_back, expected);
    }

    #[test]
    fn damaged_or_foreign_files_are_refused() {
        let bytes = write(&sample());
        assert!(read(&bytes[..bytes.len() - 1]).is_none());
        assert!(read(&[]).is_none());
        let mut flipped = bytes.clone();
        flipped[20] ^= 1;
        assert!(read(&flipped).is_none());
        let mut foreign = bytes.clone();
        foreign[0] = b'X';
        let body = foreign.len() - 8;
        let checksum = fnv::hash(fnv::OFFSET_BASIS, &foreign[..body]);
        foreign[body..].copy_from_slice(&checksum.to_le_bytes());
        assert!(read(&foreign).is_none());
        let mut varints = Vec::new();
        varint(&mut varints, 300);
        varint(&mut varints, u32::MAX);
        let mut reader = Reader {
            bytes: &varints,
            at: 0,
        };
        assert_eq!(reader.varint(), Some(300));
        assert_eq!(reader.varint(), Some(u32::MAX));
        assert_eq!(reader.varint(), None);
    }
}
