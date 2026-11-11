//! A persistent map keyed by random identities, for the factors of a presence.
//!
//! Each version shares every node but the path it changed with the version it was made from,
//! so a lineage of presences that each add a factor to the last costs a path per factor, not a
//! copy of every factor before it. The keys are identities drawn by the oracle, uniform in each
//! lane, so their bits make a balanced trie of 16 ways a level without rebalancing. How the trie
//! is shaped depends on the order of the insertions; what it holds does not, and nothing is
//! read from its shape.

use std::sync::Arc;

use super::field::Lanes;

const WIDTH: usize = 16;
const LEVELS: u32 = 16;

#[derive(Debug)]
enum Node<V> {
    /// Entries whose keys take the same path through every level: one, but for a collision of
    /// the 64 bits a path is made of.
    Leaf(Vec<(Lanes, V)>),
    Branch(Box<[Option<Arc<Node<V>>>; WIDTH]>),
}

#[derive(Debug)]
pub struct Map<V> {
    root: Option<Arc<Node<V>>>,
    len: usize,
}

impl<V> Clone for Map<V> {
    fn clone(&self) -> Map<V> {
        Map {
            root: self.root.clone(),
            len: self.len,
        }
    }
}

impl<V> Default for Map<V> {
    fn default() -> Map<V> {
        Map { root: None, len: 0 }
    }
}

fn path(key: &Lanes) -> u64 {
    key.0[0] ^ key.0[1].rotate_left(21) ^ key.0[2].rotate_left(42)
}

fn slot(path: u64, level: u32) -> usize {
    ((path >> (4 * level)) & 15) as usize
}

impl<V: Clone> Map<V> {
    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// Whether the two are one version, unchanged.
    pub fn same(&self, other: &Map<V>) -> bool {
        match (&self.root, &other.root) {
            (None, None) => true,
            (Some(a), Some(b)) => Arc::ptr_eq(a, b),
            _ => false,
        }
    }

    pub fn get(&self, key: &Lanes) -> Option<&V> {
        let path = path(key);
        let mut node = self.root.as_deref()?;
        let mut level = 0;
        loop {
            match node {
                Node::Leaf(entries) => {
                    return entries.iter().find(|(k, _)| k == key).map(|(_, v)| v);
                }
                Node::Branch(children) => {
                    node = children[slot(path, level)].as_deref()?;
                    level += 1;
                }
            }
        }
    }

    pub fn insert(&mut self, key: Lanes, value: V) {
        let (root, added) = insert(self.root.as_ref(), key, value, path(&key), 0);
        self.root = Some(root);
        self.len += usize::from(added);
    }

    pub fn remove(&mut self, key: &Lanes) -> Option<V> {
        let (root, value) = remove(self.root.as_ref()?, key, path(key), 0)?;
        self.root = root;
        self.len -= 1;
        Some(value)
    }

    /// Every entry, in no particular order.
    pub fn for_each(&self, mut f: impl FnMut(&Lanes, &V)) {
        fn walk<V>(node: &Node<V>, f: &mut impl FnMut(&Lanes, &V)) {
            match node {
                Node::Leaf(entries) => entries.iter().for_each(|(k, v)| f(k, v)),
                Node::Branch(children) => children.iter().flatten().for_each(|c| walk(c, f)),
            }
        }
        if let Some(root) = &self.root {
            walk(root, &mut f);
        }
    }
}

fn insert<V: Clone>(
    node: Option<&Arc<Node<V>>>,
    key: Lanes,
    value: V,
    path_of_key: u64,
    level: u32,
) -> (Arc<Node<V>>, bool) {
    let Some(found) = node else {
        return (Arc::new(Node::Leaf(vec![(key, value)])), true);
    };
    match &**found {
        Node::Leaf(entries) => {
            let mut entries = entries.clone();
            if let Some(entry) = entries.iter_mut().find(|(k, _)| *k == key) {
                entry.1 = value;
                return (Arc::new(Node::Leaf(entries)), false);
            }
            let other = path(&entries[0].0);
            if other == path_of_key || level >= LEVELS {
                entries.push((key, value));
                return (Arc::new(Node::Leaf(entries)), true);
            }
            // Two paths part here or below: the leaf moves down a level, shared as it is.
            let mut children: [Option<Arc<Node<V>>>; WIDTH] = Default::default();
            children[slot(other, level)] = Some(found.clone());
            let at = slot(path_of_key, level);
            let (child, added) = insert(children[at].as_ref(), key, value, path_of_key, level + 1);
            children[at] = Some(child);
            (Arc::new(Node::Branch(Box::new(children))), added)
        }
        Node::Branch(children) => {
            let at = slot(path_of_key, level);
            let (child, added) = insert(children[at].as_ref(), key, value, path_of_key, level + 1);
            let mut children = children.clone();
            children[at] = Some(child);
            (Arc::new(Node::Branch(children)), added)
        }
    }
}

type Removed<V> = (Option<Arc<Node<V>>>, V);

fn remove<V: Clone>(
    node: &Arc<Node<V>>,
    key: &Lanes,
    path_of_key: u64,
    level: u32,
) -> Option<Removed<V>> {
    match &**node {
        Node::Leaf(entries) => {
            let at = entries.iter().position(|(k, _)| k == key)?;
            let mut entries = entries.clone();
            let (_, value) = entries.remove(at);
            let rest = (!entries.is_empty()).then(|| Arc::new(Node::Leaf(entries)));
            Some((rest, value))
        }
        Node::Branch(children) => {
            let at = slot(path_of_key, level);
            let (child, value) = remove(children[at].as_ref()?, key, path_of_key, level + 1)?;
            let mut children = children.clone();
            children[at] = child;
            let rest = children
                .iter()
                .any(Option::is_some)
                .then(|| Arc::new(Node::Branch(children)));
            Some((rest, value))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(i: u64) -> Lanes {
        // Keys that agree in their low bits, to make deep paths, and some whose paths collide.
        Lanes([i.wrapping_mul(0x9e37_79b9_7f4a_7c15) << 8, i, i % 7])
    }

    #[test]
    fn versions_hold_what_was_put_in_them() {
        let mut maps = vec![Map::default()];
        for i in 0..2000u64 {
            let mut next = maps.last().unwrap().clone();
            next.insert(key(i), i);
            maps.push(next);
        }
        let last = maps.last().unwrap();
        assert_eq!(last.len(), 2000);
        assert!((0..2000).all(|i| last.get(&key(i)) == Some(&i)));
        // Each earlier version is unchanged.
        assert_eq!(maps[500].len(), 500);
        assert_eq!(maps[500].get(&key(499)), Some(&499));
        assert_eq!(maps[500].get(&key(500)), None);
        let mut fewer = last.clone();
        for i in (0..2000).step_by(2) {
            assert_eq!(fewer.remove(&key(i)), Some(i));
        }
        assert_eq!(fewer.remove(&key(0)), None);
        assert_eq!(fewer.len(), 1000);
        assert!((0..2000).all(|i| fewer.get(&key(i)) == (i % 2 == 1).then_some(&i)));
        assert_eq!(last.len(), 2000);
        let mut seen = 0;
        fewer.for_each(|k, v| {
            assert_eq!(*k, key(*v));
            seen += 1;
        });
        assert_eq!(seen, 1000);
        // Keys whose paths are the same share a leaf.
        let mut same = Map::default();
        let (a, b) = (Lanes([1, 2, 3]), Lanes([1 ^ (5u64 << 21), 2 ^ 5, 3]));
        assert_eq!(path(&a), path(&Lanes([a.0[0], a.0[1], a.0[2]])));
        same.insert(a, 1);
        same.insert(b, 2);
        same.insert(a, 3);
        assert_eq!(
            (same.get(&a), same.get(&b), same.len()),
            (Some(&3), Some(&2), 2)
        );
        assert!(same.clone().same(&same) && !same.same(&Map::default()));
    }
}
