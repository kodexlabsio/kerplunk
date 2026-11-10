//! Ranked word search over a graph's nodes.
//!
//! A node's words are the runs of ASCII letters and digits in its label, relation key and
//! synonyms, and separately in its description. A query word scores by its rarity across all
//! nodes' names: most when it is one of a node's words, less when it is only part of the label,
//! and a little when the description has it. Plural query words match their singular too. Exact
//! names, business terms and relations rank higher; tests lower.
//!
//! Text arrives in lower case, as Python's `str.lower()` makes it, so the results do not depend
//! on how the two languages fold case.

use std::cmp::Ordering;

use crate::fnv::FnvHashMap;
use crate::graph;

/// Words a question is made of that say nothing about what it is asking for.
const STOPWORDS: &[&str] = &[
    "a", "about", "all", "an", "and", "any", "are", "as", "at", "be", "by", "can", "could", "do",
    "does", "find", "for", "from", "get", "give", "how", "i", "if", "in", "into", "is", "it",
    "list", "me", "my", "of", "on", "or", "our", "should", "show", "tell", "that", "the", "their",
    "there", "these", "this", "those", "to", "use", "used", "uses", "using", "we", "what", "when",
    "where", "which", "who", "why", "will", "with", "would",
];

/// The runs of ASCII letters and digits in `text`. Bytes of other characters are never ASCII in
/// UTF-8, so scanning bytes splits exactly where a character scan would.
pub fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
        .filter(|w| !w.is_empty())
}

/// A query word and the singular forms it also matches.
fn forms(token: &str) -> Vec<&str> {
    let mut forms = vec![token];
    if token.len() > 3 && token.ends_with('s') {
        forms.push(&token[..token.len() - 1]);
    }
    if token.len() > 4 && token.ends_with("es") {
        forms.push(&token[..token.len() - 2]);
    }
    forms
}

/// How a node's kind weighs its score.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Standing {
    /// A business term: what a person asking in words usually means.
    Term,
    Relation,
    Test,
    Other,
}

impl Standing {
    fn weigh(self, score: f64) -> f64 {
        match self {
            Self::Term => score * 1.5,
            Self::Relation => score * 1.2,
            Self::Test => score * 0.5,
            Self::Other => score,
        }
    }
}

/// One node as the index is built from it; all text in lower case.
#[derive(Clone, Debug)]
pub struct Document<'a> {
    pub id: &'a str,
    pub kind: &'a str,
    pub standing: Standing,
    pub name: &'a str,
    pub label: &'a str,
    /// The label, relation key and synonyms, whose words are the node's names.
    pub names: &'a str,
    pub description: &'a str,
    pub synonyms: Vec<&'a str>,
}

#[derive(Debug)]
struct Entry {
    id: Box<str>,
    /// The id's length in characters, as Python counts it.
    id_length: usize,
    kind: Box<str>,
    standing: Standing,
    name: Box<str>,
    label: Box<str>,
    /// Word numbers, sorted.
    names: Vec<u32>,
    description: Vec<u32>,
    synonyms: Vec<Box<str>>,
}

/// The search index of one graph.
#[derive(Debug, Default)]
pub struct SearchIndex {
    entries: Vec<Entry>,
    vocabulary: FnvHashMap<Box<str>, u32>,
    /// For each word number, how many nodes have the word among their names.
    frequency: Vec<u32>,
}

impl SearchIndex {
    pub fn new<'a>(documents: impl IntoIterator<Item = Document<'a>>) -> Self {
        let mut index = Self::default();
        documents
            .into_iter()
            .for_each(|document| index.add(document));
        index
    }

    /// Add the next node; nodes are numbered in the order added.
    pub fn add(&mut self, document: Document<'_>) {
        let names = self.word_numbers(document.names);
        for &word in &names {
            self.frequency[word as usize] += 1;
        }
        let description = self.word_numbers(document.description);
        self.entries.push(Entry {
            id: document.id.into(),
            id_length: document.id.chars().count(),
            kind: document.kind.into(),
            standing: document.standing,
            name: document.name.into(),
            label: document.label.into(),
            names,
            description,
            synonyms: document.synonyms.into_iter().map(Into::into).collect(),
        });
    }

    fn word_numbers(&mut self, text: &str) -> Vec<u32> {
        let mut numbers: Vec<u32> = words(text)
            .map(|word| {
                let next = graph::ordinal(self.vocabulary.len());
                *self.vocabulary.entry(word.into()).or_insert_with(|| {
                    self.frequency.push(0);
                    next
                })
            })
            .collect();
        numbers.sort_unstable();
        numbers.dedup();
        numbers
    }

    /// The nodes matching `text` (in lower case), best first, at most `limit` of them, each with
    /// its score. `needle` is the whole query, trimmed; a node named exactly that ranks first.
    /// With `kinds`, only nodes of those kinds are returned.
    pub fn search(
        &self,
        text: &str,
        needle: &str,
        limit: Option<usize>,
        kinds: &[&str],
    ) -> Vec<(u32, f64)> {
        let tokens: Vec<&str> = words(text)
            .filter(|w| STOPWORDS.binary_search(w).is_err())
            .collect();
        if tokens.is_empty() {
            return Vec::new();
        }
        let total = self.entries.len().max(1) as f64;
        // Each query word's forms, with their word numbers (None when no node has the word) and
        // the word's weight: rarer words weigh more.
        let query: Vec<(Vec<&str>, Vec<u32>, f64)> = tokens
            .iter()
            .map(|token| {
                let forms = forms(token);
                let numbers: Vec<u32> = forms
                    .iter()
                    .filter_map(|f| self.vocabulary.get(*f).copied())
                    .collect();
                let most = numbers
                    .iter()
                    .map(|&n| self.frequency[n as usize])
                    .max()
                    .unwrap_or(0);
                let weight = (1.0 + total / f64::from(1 + most)).ln();
                (forms, numbers, weight)
            })
            .collect();
        let mut scored: Vec<(u32, f64)> = Vec::new();
        for (ordinal, entry) in self.entries.iter().enumerate() {
            if !kinds.is_empty() && !kinds.contains(&&*entry.kind) {
                continue;
            }
            let mut score = 0.0;
            for (forms, numbers, weight) in &query {
                if numbers.iter().any(|n| entry.names.binary_search(n).is_ok()) {
                    score += 3.0 * weight;
                } else if forms.iter().any(|f| entry.label.contains(f)) {
                    score += 1.5 * weight;
                }
                if numbers
                    .iter()
                    .any(|n| entry.description.binary_search(n).is_ok())
                {
                    score += 0.5 * weight;
                }
            }
            if score == 0.0 {
                continue;
            }
            let synonym =
                entry.standing == Standing::Term && entry.synonyms.iter().any(|s| &**s == needle);
            if &*entry.label == needle || &*entry.name == needle || synonym {
                score += 10.0;
            }
            scored.push((graph::ordinal(ordinal), entry.standing.weigh(score)));
        }
        let order = |a: &(u32, f64), b: &(u32, f64)| -> Ordering {
            let (left, right) = (&self.entries[a.0 as usize], &self.entries[b.0 as usize]);
            b.1.total_cmp(&a.1)
                .then(left.id_length.cmp(&right.id_length))
                .then_with(|| left.id.cmp(&right.id))
        };
        if let Some(limit) = limit
            && limit < scored.len()
        {
            if limit == 0 {
                return Vec::new();
            }
            scored.select_nth_unstable_by(limit - 1, order);
            scored.truncate(limit);
        }
        scored.sort_unstable_by(order);
        scored
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document<'a>(
        id: &'a str,
        kind: &'a str,
        label: &'a str,
        description: &'a str,
    ) -> Document<'a> {
        let standing = match kind {
            "term" => Standing::Term,
            "model" | "table" => Standing::Relation,
            "test" => Standing::Test,
            _ => Standing::Other,
        };
        let name = label.rsplit('.').next().unwrap_or(label);
        Document {
            id,
            kind,
            standing,
            name,
            label,
            names: label,
            description,
            synonyms: Vec::new(),
        }
    }

    #[test]
    fn stopwords_are_sorted_for_binary_search() {
        assert!(STOPWORDS.windows(2).all(|pair| pair[0] < pair[1]));
    }

    #[test]
    fn words_are_ascii_letter_and_digit_runs() {
        assert_eq!(
            words("orders.amount_usd, 2nd-é x").collect::<Vec<_>>(),
            ["orders", "amount", "usd", "2nd", "x"]
        );
        assert_eq!(forms("classes"), ["classes", "classe", "class"]);
        assert_eq!(forms("bus"), ["bus"]);
    }

    #[test]
    fn names_rank_above_descriptions_and_rare_words_above_common_ones() {
        let index = SearchIndex::new([
            document("model.a.orders", "model", "orders", ""),
            document("model.a.orders#amount", "column", "orders.amount", ""),
            document(
                "model.a.refunds#amount",
                "column",
                "refunds.amount",
                "money returned for orders",
            ),
        ]);
        let results = index.search("orders", "orders", None, &[]);
        let order: Vec<u32> = results.iter().map(|r| r.0).collect();
        assert_eq!(order, [0, 1, 2]);
        assert!(results[2].1 < results[1].1);
        assert_eq!(
            index.search("amount", "amount", Some(5), &["column"]).len(),
            2
        );
        assert!(index.search("the of", "the of", None, &[]).is_empty());
        assert!(index.search("nothing", "nothing", None, &[]).is_empty());
    }

    #[test]
    fn plurals_and_label_substrings_match() {
        let index = SearchIndex::new([
            document("model.a.payment", "model", "payment", ""),
            document("t", "test", "paymentsx", ""),
        ]);
        let results = index.search("payments", "payments", None, &[]);
        assert_eq!(results.iter().map(|r| r.0).collect::<Vec<_>>(), [0, 1]);
    }

    #[test]
    fn limits_keep_the_best() {
        let documents: Vec<Document<'_>> = ["c", "bb", "a"]
            .iter()
            .map(|&id| document(id, "column", "x", ""))
            .collect();
        let index = SearchIndex::new(documents);
        let all = index.search("x", "y", None, &[]);
        // Equal scores: shorter ids first, then by id.
        assert_eq!(all.iter().map(|r| r.0).collect::<Vec<_>>(), [2, 0, 1]);
        assert_eq!(index.search("x", "y", Some(2), &[]), all[..2]);
        assert!(index.search("x", "y", Some(0), &[]).is_empty());
    }

    #[test]
    fn synonyms_of_terms_count_as_exact_names() {
        let mut term = document("term.revenue", "term", "revenue", "");
        term.synonyms = vec!["sales"];
        term.names = "revenue sales";
        let index = SearchIndex::new([term, document("model.a.sales", "column", "sales", "")]);
        let results = index.search("sales", "sales", None, &[]);
        assert_eq!(results[0].0, 0);
    }
}
