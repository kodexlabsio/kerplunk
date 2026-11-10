//! One ranking from two: the word search's and the vector search's.
//!
//! Reciprocal rank fusion (Cormack, Clarke and Büttcher, 2009): a node scores `1 / (K + rank)`
//! in each list it appears in, so agreement between the lists counts for more than a high
//! score in either. A node named exactly what was asked for, by the word search's rule, ranks
//! above every other, so exact names still win.

/// Damps the difference between the first ranks, as the method recommends; smaller than its
/// usual 60 because the lists here are short.
const K: f64 = 30.0;

/// What an exact name adds: more than any other node can score, which is 1, for first in
/// both lists.
const EXACT: f64 = 2.0;

/// A node in the fused ranking: its ordinal, its score (2 or more for an exact name, otherwise
/// up to 1 for first in both lists) and, when the vector search found it, what that rests on.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fused<B> {
    pub ordinal: u32,
    pub score: f64,
    pub basis: Option<B>,
}

/// How much each list counts: equally for a question in words; only the vector search for a
/// question that reads as a formula, whose words name the formula's inputs rather than the
/// column asked for (exact names still come first).
pub fn weights(formula: bool) -> (f64, f64) {
    if formula { (0.0, 1.0) } else { (1.0, 1.0) }
}

/// Fuse `words` (best first, each with whether it is an exact name) and `meaning` (best first,
/// each with its basis), each list weighed as `weights` says, keeping the best `limit`. Equal
/// scores (a node first in one list and second in the other against one the other way round)
/// go to the node the vector search ranked higher, which matched more than the words, then as
/// `order` orders the nodes.
pub fn fuse<B: Copy>(
    words: &[(u32, bool)],
    meaning: &[(u32, B)],
    (in_words, in_meaning): (f64, f64),
    limit: usize,
    order: impl Fn(u32, u32) -> std::cmp::Ordering,
) -> Vec<Fused<B>> {
    let mut fused: Vec<Fused<B>> = Vec::with_capacity(words.len() + meaning.len());
    let mut position = crate::fnv::FnvHashMap::<u32, usize>::default();
    let unit = (in_words + in_meaning) / (K + 1.0);
    for (rank, &(ordinal, exact)) in words.iter().enumerate() {
        if in_words == 0.0 && !exact {
            continue;
        }
        position.insert(ordinal, fused.len());
        fused.push(Fused {
            ordinal,
            score: in_words / (K + 1.0 + rank as f64) / unit + if exact { EXACT } else { 0.0 },
            basis: None,
        });
    }
    for (rank, &(ordinal, basis)) in meaning.iter().enumerate() {
        let add = in_meaning / (K + 1.0 + rank as f64) / unit;
        match position.get(&ordinal) {
            Some(&at) => {
                fused[at].score += add;
                fused[at].basis = Some(basis);
            }
            None => {
                position.insert(ordinal, fused.len());
                fused.push(Fused {
                    ordinal,
                    score: add,
                    basis: Some(basis),
                });
            }
        }
    }
    let ranks: crate::fnv::FnvHashMap<u32, usize> = meaning
        .iter()
        .enumerate()
        .map(|(rank, m)| (m.0, rank))
        .collect();
    let rank = |ordinal: u32| ranks.get(&ordinal).copied().unwrap_or(usize::MAX);
    fused.sort_by(|a, b| {
        b.score
            .total_cmp(&a.score)
            .then(rank(a.ordinal).cmp(&rank(b.ordinal)))
            .then_with(|| order(a.ordinal, b.ordinal))
    });
    fused.truncate(limit);
    fused
}

#[cfg(test)]
mod tests {
    use super::*;

    fn by_ordinal(a: u32, b: u32) -> std::cmp::Ordering {
        a.cmp(&b)
    }

    #[test]
    fn agreement_wins_and_exact_names_win_outright() {
        let words = [(1, false), (2, false), (3, true)];
        let meaning = [(2, 'm'), (4, 'm'), (1, 'w')];
        let fused = fuse(&words, &meaning, weights(false), 10, by_ordinal);
        let order: Vec<u32> = fused.iter().map(|f| f.ordinal).collect();
        // 3 is an exact name; 1 and 2 are in both lists, 2 nearer the top on balance.
        assert_eq!(order, [3, 2, 1, 4]);
        assert!(fused[0].score > 1.0);
        assert!((fused[1].score - (31.0 / 32.0 + 1.0) / 2.0).abs() < 1e-9);
        assert_eq!(fused[3].basis, Some('m'));
        assert_eq!(fused[0].basis, None);
        assert_eq!(
            fuse(&words, &meaning, weights(false), 2, by_ordinal).len(),
            2
        );
        assert!(fuse::<char>(&[], &[], weights(false), 5, by_ordinal).is_empty());
        // First in both lists scores one.
        assert!(
            (fuse(&[(7, false)], &[(7, 'x')], weights(true), 1, by_ordinal)[0].score - 1.0).abs()
                < 1e-12
        );
        // A formula question leans on the vector search.
        let formula = fuse(
            &[(1, false), (3, true)],
            &[(2, 'f'), (1, 'w')],
            weights(true),
            5,
            by_ordinal,
        );
        let order: Vec<u32> = formula.iter().map(|f| f.ordinal).collect();
        assert_eq!(order, [3, 2, 1]);
        // Equal scores: the vector search's order, then the order given.
        let tied = fuse(
            &[(5, false), (6, false)],
            &[(6, 'm'), (5, 'm')],
            weights(false),
            2,
            by_ordinal,
        );
        assert_eq!(tied[0].score, tied[1].score);
        assert_eq!(tied[0].ordinal, 6);
        let reversed = fuse::<char>(&[], &[(8, 'm')], weights(false), 1, |a, b| b.cmp(&a));
        assert_eq!(reversed[0].ordinal, 8);
    }
}
