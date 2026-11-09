//! Which relations a name such as `orders`, `main.orders` or `model.shop.orders` refers to.
//!
//! A relation answers to its name, its id and its warehouse relation key, and to any end of
//! those that starts after a dot, so `orders` and `main.orders` both find `shop.main.orders`. A
//! source system's table (`outside_warehouse`) answers only to its full name and id, so the short
//! names keep meaning the warehouse table it lands as.

use std::collections::HashMap;

/// One relation as the index is built from it. Names and ids are already in lower case.
#[derive(Clone, Copy, Debug)]
pub struct Relation<'a> {
    pub name: &'a str,
    pub id: &'a str,
    /// The warehouse relation key, if the relation has one (an empty key is kept, but no name
    /// ends with it).
    pub relation: Option<&'a str>,
    pub outside_warehouse: bool,
}

/// Relations by every name they answer to.
#[derive(Debug, Default)]
pub struct RelationNames {
    /// Each name to the relations answering to it, in the order they were given.
    keys: HashMap<Box<str>, Vec<u32>>,
    names: Vec<Box<str>>,
    relations: Vec<Option<Box<str>>>,
}

/// The name itself, then each end of it that starts after a dot.
fn ends(key: &str) -> impl Iterator<Item = &str> {
    std::iter::once(key).chain(key.match_indices('.').map(move |(at, _)| &key[at + 1..]))
}

impl RelationNames {
    pub fn new<'a>(relations: impl IntoIterator<Item = Relation<'a>>) -> Self {
        let mut index = Self::default();
        for (ordinal, relation) in relations.into_iter().enumerate() {
            let ordinal = ordinal as u32;
            let mut add = |key: &str| {
                let found = index.keys.entry(key.into()).or_default();
                if found.last() != Some(&ordinal) {
                    found.push(ordinal);
                }
            };
            if relation.outside_warehouse {
                add(relation.name);
                add(relation.id);
            } else {
                let key = relation.relation.filter(|k| !k.is_empty());
                for name in [Some(relation.name), Some(relation.id), key]
                    .into_iter()
                    .flatten()
                {
                    ends(name).for_each(&mut add);
                }
            }
            index.names.push(relation.name.into());
            index.relations.push(relation.relation.map(Into::into));
        }
        index
    }

    /// The relations `needle` (in lower case) names, in the order they were given. When some
    /// match it exactly, by name or relation key, those win: alone, or over other matches.
    pub fn find(&self, needle: &str) -> Vec<u32> {
        let Some(matches) = self.keys.get(needle) else {
            return Vec::new();
        };
        let exact: Vec<u32> = matches
            .iter()
            .copied()
            .filter(|&o| {
                &*self.names[o as usize] == needle
                    || self.relations[o as usize].as_deref() == Some(needle)
            })
            .collect();
        if exact.len() == 1 || (!exact.is_empty() && matches.len() > exact.len()) {
            exact
        } else {
            matches.clone()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn relation<'a>(name: &'a str, id: &'a str, key: Option<&'a str>) -> Relation<'a> {
        Relation {
            name,
            id,
            relation: key,
            outside_warehouse: false,
        }
    }

    #[test]
    fn ends_start_after_each_dot() {
        assert_eq!(ends("a.b.c").collect::<Vec<_>>(), ["a.b.c", "b.c", "c"]);
        assert_eq!(ends("a.").collect::<Vec<_>>(), ["a.", ""]);
    }

    #[test]
    fn short_and_qualified_names() {
        let names = RelationNames::new([
            relation("orders", "model.shop.orders", Some("wh.main.orders")),
            relation(
                "stg_orders",
                "model.shop.stg_orders",
                Some("wh.main.stg_orders"),
            ),
        ]);
        assert_eq!(names.find("orders"), [0]);
        assert_eq!(names.find("main.orders"), [0]);
        assert_eq!(names.find("shop.stg_orders"), [1]);
        assert_eq!(names.find("rders"), Vec::<u32>::new());
    }

    #[test]
    fn exact_names_win_over_suffixes() {
        let names = RelationNames::new([
            relation("orders", "source.shop.raw.orders", Some("wh.raw.orders")),
            relation("orders", "model.shop.orders", Some("wh.main.orders")),
            relation("x", "table.wh.main.x", Some("orders")),
        ]);
        // All three answer to "orders"; all three are exact, so all are kept.
        assert_eq!(names.find("orders"), [0, 1, 2]);
        assert_eq!(names.find("raw.orders"), [0]);
    }

    #[test]
    fn one_exact_match_wins_alone() {
        let names = RelationNames::new([
            relation("customers", "model.a.customers", Some("wh.a.customers")),
            relation("dim", "model.b.dim", Some("wh.b.customers")),
        ]);
        assert_eq!(names.find("customers"), [0]);
    }

    #[test]
    fn tables_outside_the_warehouse_answer_to_full_names_only() {
        let names = RelationNames::new([Relation {
            name: "shopify.orders",
            id: "table.shopify.orders",
            relation: Some("shopify.orders"),
            outside_warehouse: true,
        }]);
        assert_eq!(names.find("orders"), Vec::<u32>::new());
        assert_eq!(names.find("shopify.orders"), [0]);
        assert_eq!(names.find("table.shopify.orders"), [0]);
    }

    #[test]
    fn an_empty_relation_key_matches_nothing_but_is_exact() {
        let names = RelationNames::new([relation("a.", "model.a.", Some(""))]);
        // "a." ends with a dot, so the empty end matches, and the empty key makes it exact.
        assert_eq!(names.find(""), [0]);
        assert_eq!(names.find("a."), [0]);
    }
}
