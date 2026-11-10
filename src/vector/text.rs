//! Words as a data team writes them, and the features their meaning is hashed from.
//!
//! Identifiers are split where a person reads a word boundary: at punctuation, between a lower
//! case and an upper case letter (`netSales`), before the last capital of a run (`HTTPServer`)
//! and between letters and digits (`amount2`). Each word is folded to lower case, expanded when
//! it is a known abbreviation (`amt`, `qty`, `ltv`), made singular and mapped to the concept it
//! belongs to (`sales` and `turnover` to revenue, `gbp` and `sterling` to pounds). A word then
//! contributes the word itself, its concept (and, more faintly, a concept it is partly: revenue
//! is a value), its character trigrams (so `payment` still meets
//! `payments` or `pmt_payment`) and, with the word before it, a bigram (so `lifetime value` is
//! more than `lifetime` and `value`).

use std::sync::OnceLock;

use crate::fnv::{self, FnvHashMap};
use crate::search::STOPWORDS;

/// A hashed feature and its weight.
pub type Feature = (u64, f32);

/// The namespaces features are hashed in, so a word and a trigram with the same letters differ.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Space {
    Word = 1,
    Concept = 2,
    Bigram = 3,
    Trigram = 4,
    /// A column a formula reads, by its words.
    Column = 5,
    ColumnConcept = 6,
    Function = 7,
    Keyword = 8,
    Operator = 9,
    Number = 10,
    Literal = 11,
    /// Three formula tokens in a row, with columns, numbers and strings as placeholders.
    Shape = 12,
}

/// The well-mixed 64-bit hash of a feature: FNV-1a over its namespace and parts, then the
/// MurmurHash3 finaliser, because the projection takes low bits and FNV's are weak. The lowest
/// bit is set for trigrams and clear for everything else, so an index of whole words can skip
/// trigrams without knowing their namespace.
pub fn feature(space: Space, parts: &[&str]) -> u64 {
    let mut state = fnv::hash(fnv::OFFSET_BASIS, &[space as u8]);
    for part in parts {
        state = fnv::hash(state, part.as_bytes());
        state = fnv::hash(state, &[0xff]);
    }
    (mix(state) & !1) | u64::from(space == Space::Trigram)
}

/// Whether a feature is a trigram (see [`feature`]).
pub fn is_trigram(hash: u64) -> bool {
    hash & 1 == 1
}

/// MurmurHash3's 64-bit finaliser: every input bit affects every output bit.
pub fn mix(mut h: u64) -> u64 {
    h ^= h >> 33;
    h = h.wrapping_mul(0xff51_afd7_ed55_8ccd);
    h ^= h >> 33;
    h = h.wrapping_mul(0xc4ce_b9fe_1a85_ec53);
    h ^ (h >> 33)
}

/// The words of `text`, split as an identifier is read and folded to lower case.
pub fn split(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut words = Vec::new();
    let mut current = String::new();
    for (at, &c) in chars.iter().enumerate() {
        if !c.is_alphanumeric() {
            flush(&mut current, &mut words);
            continue;
        }
        if let Some(&previous) = at.checked_sub(1).and_then(|i| chars.get(i)) {
            let next = chars.get(at + 1).copied();
            let boundary = (previous.is_lowercase() && c.is_uppercase())
                || (previous.is_uppercase()
                    && c.is_uppercase()
                    && next.is_some_and(char::is_lowercase))
                || (previous.is_alphabetic() && c.is_numeric())
                || (previous.is_numeric() && c.is_alphabetic());
            if boundary && previous.is_alphanumeric() {
                flush(&mut current, &mut words);
            }
        }
        current.extend(c.to_lowercase());
    }
    flush(&mut current, &mut words);
    words
}

fn flush(current: &mut String, words: &mut Vec<String>) {
    if !current.is_empty() {
        words.push(std::mem::take(current));
    }
}

/// Abbreviations data teams write, and names written as one word, each with the words it stands
/// for. Sorted, for binary search.
const ABBREVIATIONS: &[(&str, &[&str])] = &[
    ("acct", &["account"]),
    ("addr", &["address"]),
    ("amt", &["amount"]),
    ("aov", &["average", "order", "value"]),
    ("arpu", &["average", "revenue", "user"]),
    ("arr", &["annual", "recurring", "revenue"]),
    ("avg", &["average"]),
    ("bal", &["balance"]),
    ("cac", &["customer", "acquisition", "cost"]),
    ("ccy", &["currency"]),
    ("clv", &["customer", "lifetime", "value"]),
    ("cnt", &["count"]),
    ("cntry", &["country"]),
    ("cogs", &["cost", "good", "sold"]),
    ("ctry", &["country"]),
    ("cur", &["currency"]),
    ("curr", &["currency"]),
    ("cust", &["customer"]),
    ("dau", &["daily", "active", "user"]),
    ("dept", &["department"]),
    ("desc", &["description"]),
    ("disc", &["discount"]),
    ("dob", &["date", "birth"]),
    ("dt", &["date"]),
    ("emp", &["employee"]),
    ("firstname", &["first", "name"]),
    ("forename", &["first", "name"]),
    ("fx", &["exchange", "rate"]),
    ("gmv", &["gross", "merchandise", "value"]),
    ("hr", &["hour"]),
    ("inv", &["invoice"]),
    ("lastname", &["last", "name"]),
    ("lat", &["latitude"]),
    ("lng", &["longitude"]),
    ("lon", &["longitude"]),
    ("ltv", &["lifetime", "value"]),
    ("mau", &["monthly", "active", "user"]),
    ("mgr", &["manager"]),
    ("mo", &["month"]),
    ("mrr", &["monthly", "recurring", "revenue"]),
    ("mth", &["month"]),
    ("nbr", &["number"]),
    ("num", &["number"]),
    ("org", &["organisation"]),
    ("pct", &["percent"]),
    ("pmt", &["payment"]),
    ("prc", &["price"]),
    ("prod", &["product"]),
    ("qtr", &["quarter"]),
    ("qty", &["quantity"]),
    ("rev", &["revenue"]),
    ("sku", &["product"]),
    ("surname", &["last", "name"]),
    ("tot", &["total"]),
    ("trx", &["transaction"]),
    ("ts", &["timestamp"]),
    ("ttl", &["total"]),
    ("txn", &["transaction"]),
    ("usr", &["user"]),
    ("wk", &["week"]),
    ("yr", &["year"]),
];

/// Words that mean the same thing in data work, in singular form; the first word of each group
/// names its concept. A word belongs to one group at most.
const CONCEPTS: &[&[&str]] = &[
    &[
        "revenue", "sale", "turnover", "income", "earning", "taking", "proceed",
    ],
    &["value", "amount", "total", "sum", "figure"],
    &[
        "refund",
        "refunded",
        "return",
        "returned",
        "chargeback",
        "reimbursement",
        "reversal",
    ],
    &[
        "churn",
        "churned",
        "attrition",
        "cancel",
        "cancelled",
        "canceled",
        "cancellation",
        "lapsed",
        "lapse",
        "retention",
        "retained",
        "inactive",
        "unsubscribed",
        "deactivated",
    ],
    &[
        "customer",
        "client",
        "shopper",
        "buyer",
        "purchaser",
        "consumer",
        "patron",
    ],
    &["user", "member", "visitor"],
    &["price", "msrp", "rrp", "tariff"],
    &["cost", "expense", "spend", "expenditure", "outlay"],
    &["profit", "margin"],
    &["quantity", "unit", "volume"],
    &["count", "number", "tally", "frequency"],
    &["order", "purchase", "basket", "checkout"],
    &["payment", "paid", "pay", "remittance", "settlement"],
    &["transaction", "transfer"],
    &["invoice", "bill", "billing"],
    &[
        "discount",
        "coupon",
        "promo",
        "promotion",
        "voucher",
        "rebate",
    ],
    &["tax", "vat", "gst", "duty"],
    &[
        "shipping",
        "shipment",
        "delivery",
        "freight",
        "postage",
        "fulfilment",
        "fulfillment",
    ],
    &["product", "item", "article", "good", "merchandise"],
    &["category", "class", "classification"],
    &["date", "day"],
    &["timestamp", "time", "datetime"],
    &[
        "created",
        "creation",
        "inserted",
        "signup",
        "registered",
        "joined",
    ],
    &["updated", "modified", "changed"],
    &["month", "monthly"],
    &["year", "annual", "yearly"],
    &["quarter", "quarterly"],
    &["week", "weekly"],
    &["daily"],
    &["pound", "gbp", "sterling", "quid"],
    &["dollar", "usd"],
    &["euro", "eur"],
    &["aud", "australian"],
    &["currency", "money", "monetary"],
    &["address", "street", "postcode", "zip", "zipcode", "postal"],
    &["email", "mail"],
    &["phone", "telephone", "mobile", "tel"],
    &["identifier", "id", "key", "uuid", "guid"],
    &["employee", "staff", "worker", "personnel", "headcount"],
    &[
        "subscription",
        "subscriber",
        "plan",
        "membership",
        "recurring",
    ],
    &["session", "visit"],
    &["country", "nation"],
    &["region", "territory", "area"],
    &["city", "town"],
    &["average", "mean"],
    &["percent", "percentage", "ratio", "share", "proportion"],
    &["lifetime", "ltd"],
    &["status", "state", "stage"],
    &["balance", "outstanding"],
    &["account"],
    &["department", "division", "team"],
    &[
        "organisation",
        "organization",
        "company",
        "business",
        "firm",
        "enterprise",
    ],
    &["vendor", "supplier", "seller", "merchant"],
    &["latitude"],
    &["longitude"],
    &["description", "comment", "note"],
    &["active", "live", "enabled"],
    &["deleted", "removed", "archived"],
    &["weight", "mass"],
    &["first", "earliest", "initial"],
    &["last", "latest", "recent"],
    &["gross"],
    &["net"],
    &["birth", "born"],
    &["name", "title", "label"],
    &["fee", "charge", "commission"],
    &["salary", "wage", "remuneration"],
];

/// Concepts that are partly others, sorted: revenue is a value (an amount, a total), so a
/// project that names nothing revenue still answers a question about revenue with its amounts,
/// below anything named so.
const RELATED: &[(&str, &str)] = &[("revenue", "value")];

/// The concept that `concept` is partly, if any.
pub fn related(concept: &str) -> Option<&'static str> {
    RELATED
        .binary_search_by(|(kind, _)| kind.cmp(&concept))
        .ok()
        .map(|at| RELATED[at].1)
}

fn concepts() -> &'static FnvHashMap<&'static str, &'static str> {
    static CONCEPT_OF: OnceLock<FnvHashMap<&'static str, &'static str>> = OnceLock::new();
    CONCEPT_OF.get_or_init(|| {
        let mut map = FnvHashMap::default();
        for group in CONCEPTS {
            for word in *group {
                map.entry(*word).or_insert(group[0]);
            }
        }
        map
    })
}

/// The concept a normalised word belongs to.
pub fn concept(word: &str) -> Option<&'static str> {
    concepts().get(word).copied()
}

/// The singular of a lower-case word, by the regular English rules: `categories` to
/// `category`, `sales` to `sale`; `status`, `address` and `analysis` stay as they are.
pub fn singular(word: &str) -> String {
    let n = word.len();
    if n > 4 && word.ends_with("ies") {
        return format!("{}y", &word[..n - 3]);
    }
    if n > 3
        && word.ends_with('s')
        && !word.ends_with("ss")
        && !word.ends_with("us")
        && !word.ends_with("is")
    {
        return word[..n - 1].to_owned();
    }
    word.to_owned()
}

/// The normal forms of one lower-case word: an abbreviation's expansion, or the word made
/// singular. Stopwords give nothing.
pub fn normalise(word: &str) -> Vec<String> {
    if STOPWORDS.binary_search(&word).is_ok() {
        return Vec::new();
    }
    let lookup = |w: &str| {
        ABBREVIATIONS
            .binary_search_by(|(key, _)| key.cmp(&w))
            .ok()
            .map(|at| {
                ABBREVIATIONS[at]
                    .1
                    .iter()
                    .map(|s| (*s).to_owned())
                    .collect()
            })
    };
    if let Some(expansion) = lookup(word) {
        return expansion;
    }
    let one = singular(word);
    lookup(&one).unwrap_or_else(|| vec![one])
}

/// The normalised words of `text`, in order.
pub fn terms(text: &str) -> Vec<String> {
    split(text).iter().flat_map(|w| normalise(w)).collect()
}

/// How much each kind of text feature weighs within a word.
pub const WORD: f32 = 1.0;
pub const CONCEPT: f32 = 0.8;
pub const BIGRAM: f32 = 0.6;
pub const RELATED_CONCEPT: f32 = 0.4;
/// The weight of a word's trigrams together.
pub const TRIGRAMS: f32 = 0.55;
/// Words of digits only say little and have no useful trigrams.
pub const DIGITS: f32 = 0.3;

/// The text features of `text`, each weight multiplied by `weight`, appended to `out`.
pub fn text_features(text: &str, weight: f32, out: &mut Vec<Feature>) {
    word_features(&terms(text), weight, out);
}

/// The features of a run of normalised words.
pub fn word_features(words: &[String], weight: f32, out: &mut Vec<Feature>) {
    let mut previous: Option<&str> = None;
    for word in words {
        if word.chars().all(|c| c.is_numeric()) {
            out.push((feature(Space::Word, &[word]), DIGITS * weight));
            previous = None;
            continue;
        }
        out.push((feature(Space::Word, &[word]), WORD * weight));
        let meaning = concept(word);
        if let Some(meaning) = meaning {
            out.push((feature(Space::Concept, &[meaning]), CONCEPT * weight));
            if let Some(part) = related(meaning) {
                out.push((feature(Space::Concept, &[part]), RELATED_CONCEPT * weight));
            }
        }
        let canonical = meaning.unwrap_or(word);
        if let Some(previous) = previous {
            out.push((
                feature(Space::Bigram, &[previous, canonical]),
                BIGRAM * weight,
            ));
        }
        previous = Some(canonical);
        trigrams(word, weight, out);
    }
}

/// The character trigrams of `^word$`, together weighing [`TRIGRAMS`].
fn trigrams(word: &str, weight: f32, out: &mut Vec<Feature>) {
    let padded: Vec<char> = std::iter::once('^')
        .chain(word.chars())
        .chain(std::iter::once('$'))
        .collect();
    if padded.len() < 4 {
        return;
    }
    let count = padded.len() - 2;
    let each = TRIGRAMS * weight / (count as f32).sqrt();
    let mut buffer = String::with_capacity(12);
    for window in padded.windows(3) {
        buffer.clear();
        buffer.extend(window);
        out.push((feature(Space::Trigram, &[&buffer]), each));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_split_where_people_read_words() {
        assert_eq!(split("net_sales_amt"), ["net", "sales", "amt"]);
        assert_eq!(split("netSalesAmt"), ["net", "sales", "amt"]);
        assert_eq!(split("HTTPServerLog"), ["http", "server", "log"]);
        assert_eq!(split("amount2usd"), ["amount", "2", "usd"]);
        assert_eq!(split("orders.Amount (GBP)"), ["orders", "amount", "gbp"]);
        assert_eq!(split("Kündigung_Datum"), ["kündigung", "datum"]);
        assert!(split("  ..__ ").is_empty());
    }

    #[test]
    fn words_are_expanded_made_singular_and_mapped_to_concepts() {
        assert_eq!(terms("refund_amt_gbp"), ["refund", "amount", "gbp"]);
        assert_eq!(terms("customer_ltv"), ["customer", "lifetime", "value"]);
        assert_eq!(terms("categories of sales"), ["category", "sale"]);
        assert_eq!(terms("status address"), ["status", "address"]);
        assert_eq!(terms("amts"), ["amount"]);
        assert_eq!(concept("sale"), Some("revenue"));
        assert_eq!(concept("pound"), Some("pound"));
        assert_eq!(concept("sterling"), Some("pound"));
        assert_eq!(concept("unknown"), None);
        assert_eq!(
            terms("Surname, firstName"),
            ["last", "name", "first", "name"]
        );
        assert_eq!(related("revenue"), Some("value"));
        assert_eq!(related("salary"), None);
    }

    #[test]
    fn tables_are_sorted_and_unambiguous() {
        assert!(ABBREVIATIONS.windows(2).all(|pair| pair[0].0 < pair[1].0));
        assert!(RELATED.windows(2).all(|pair| pair[0].0 < pair[1].0));
        let heads: std::collections::BTreeSet<&str> =
            CONCEPTS.iter().map(|group| group[0]).collect();
        for (kind, part) in RELATED {
            assert!(
                heads.contains(kind) && heads.contains(part),
                "{kind}: {part}"
            );
        }
        let mut seen = std::collections::BTreeSet::new();
        for word in CONCEPTS.iter().flat_map(|group| group.iter()) {
            assert!(seen.insert(*word), "{word} is in two groups");
            assert_eq!(normalise(word).len(), 1, "{word} is not a normal form");
        }
        // Every expansion is a word the concept table can read.
        for (_, words) in ABBREVIATIONS {
            for word in *words {
                assert_eq!(normalise(word), [*word], "{word}");
            }
        }
    }

    #[test]
    fn features_are_stable_and_namespaced() {
        assert_eq!(
            feature(Space::Word, &["amount"]),
            feature(Space::Word, &["amount"])
        );
        assert_ne!(
            feature(Space::Word, &["amount"]),
            feature(Space::Concept, &["amount"])
        );
        assert_ne!(
            feature(Space::Bigram, &["a", "bc"]),
            feature(Space::Bigram, &["ab", "c"])
        );
        assert!(is_trigram(feature(Space::Trigram, &["^am"])));
        assert!(!is_trigram(feature(Space::Word, &["^am"])));
        let mut out = Vec::new();
        text_features("Net sales amt", 1.0, &mut out);
        // net, sales (+ revenue and value), amount (+ value), two bigrams and the trigrams.
        assert!(out.len() > 8);
        let words: Vec<u64> = out.iter().map(|f| f.0).collect();
        assert!(words.contains(&feature(Space::Concept, &["revenue"])));
        assert!(words.contains(&feature(Space::Bigram, &["revenue", "value"])));
        let mut digits = Vec::new();
        text_features("2024", 1.0, &mut digits);
        assert_eq!(digits, [(feature(Space::Word, &["2024"]), DIGITS)]);
    }
}
