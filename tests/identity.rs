//! Property tests of the formula identity test against an independent reading of the same
//! model: formulas expanded symbolically into canonical polynomials over the columns and over
//! the row sums of monomials, with exact integer coefficients. That reading has no NULLs and
//! no empty tables, so it is compared with the verdict without NULLs.

use std::collections::BTreeMap;

use _core::identity::Formulas;
use _core::identity::witness::{Input, Kind, Value};
use proptest::prelude::*;

#[derive(Clone, Debug)]
enum E {
    Var(u8),
    Const(i64),
    Add(Box<E>, Box<E>),
    Sub(Box<E>, Box<E>),
    Mul(Box<E>, Box<E>),
    Neg(Box<E>),
    Sum(Box<E>),
}

fn row_level() -> impl Strategy<Value = E> {
    let leaf = prop_oneof![(0u8..3).prop_map(E::Var), (-3i64..4).prop_map(E::Const)];
    leaf.prop_recursive(3, 12, 2, |inner| {
        prop_oneof![
            (inner.clone(), inner.clone()).prop_map(|(a, b)| E::Add(a.into(), b.into())),
            (inner.clone(), inner.clone()).prop_map(|(a, b)| E::Sub(a.into(), b.into())),
            (inner.clone(), inner.clone()).prop_map(|(a, b)| E::Mul(a.into(), b.into())),
            inner.prop_map(|a| E::Neg(a.into())),
        ]
    })
}

/// A formula of columns and of sums of row-level formulas.
fn formulas() -> impl Strategy<Value = E> {
    let leaf = prop_oneof![
        row_level().prop_map(|e| E::Sum(e.into())),
        (0u8..2).prop_map(E::Var),
        (-3i64..4).prop_map(E::Const),
    ];
    leaf.prop_recursive(2, 8, 2, |inner| {
        prop_oneof![
            (inner.clone(), inner.clone()).prop_map(|(a, b)| E::Add(a.into(), b.into())),
            (inner.clone(), inner.clone()).prop_map(|(a, b)| E::Sub(a.into(), b.into())),
            (inner.clone(), inner).prop_map(|(a, b)| E::Mul(a.into(), b.into())),
        ]
    })
}

/// The same formula written differently: products distributed, sums split, operands swapped.
fn rewrite(e: &E) -> E {
    let r = |x: &E| Box::new(rewrite(x));
    match e {
        E::Var(_) | E::Const(_) => e.clone(),
        E::Add(a, b) => E::Add(r(b), r(a)),
        E::Sub(a, b) => E::Add(r(a), Box::new(E::Neg(r(b)))),
        E::Mul(a, b) => match &**b {
            E::Add(x, y) => E::Add(Box::new(E::Mul(r(a), r(x))), Box::new(E::Mul(r(y), r(a)))),
            _ => E::Mul(r(b), r(a)),
        },
        E::Neg(a) => E::Mul(Box::new(E::Const(-1)), r(a)),
        E::Sum(inner) => match &**inner {
            E::Add(x, y) => E::Add(Box::new(E::Sum(r(x))), Box::new(E::Sum(r(y)))),
            E::Mul(c, x) if matches!(**c, E::Const(_)) => E::Mul(c.clone(), Box::new(E::Sum(r(x)))),
            _ => E::Sum(r(inner)),
        },
    }
}

fn program(e: &E) -> String {
    match e {
        E::Var(v) => format!("(present (col \"x{v}\"))"),
        E::Const(c) => format!("(num \"{c}\" \"1\")"),
        E::Add(a, b) => format!("(add {} {})", program(a), program(b)),
        E::Sub(a, b) => format!("(sub {} {})", program(a), program(b)),
        E::Mul(a, b) => format!("(mul {} {})", program(a), program(b)),
        E::Neg(a) => format!("(neg {})", program(a)),
        E::Sum(a) => format!("(sum {})", program(a)),
    }
}

/// A variable of the canonical form: a column, or the row sum of a monomial of columns.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Key {
    Column(u8),
    RowSum(Vec<u8>),
}

type Canonical = BTreeMap<Vec<Key>, i128>;

fn plus(a: &Canonical, b: &Canonical, sign: i128) -> Canonical {
    let mut out = a.clone();
    for (m, c) in b {
        *out.entry(m.clone()).or_insert(0) += sign * c;
    }
    out.retain(|_, c| *c != 0);
    out
}

fn times(a: &Canonical, b: &Canonical) -> Canonical {
    let mut out = Canonical::new();
    for (m1, c1) in a {
        for (m2, c2) in b {
            let mut m: Vec<Key> = m1.iter().chain(m2).cloned().collect();
            m.sort();
            *out.entry(m).or_insert(0) += c1 * c2;
        }
    }
    out.retain(|_, c| *c != 0);
    out
}

fn canonical(e: &E) -> Canonical {
    match e {
        E::Var(v) => Canonical::from([(vec![Key::Column(*v)], 1)]),
        E::Const(0) => Canonical::new(),
        E::Const(c) => Canonical::from([(vec![], i128::from(*c))]),
        E::Add(a, b) => plus(&canonical(a), &canonical(b), 1),
        E::Sub(a, b) => plus(&canonical(a), &canonical(b), -1),
        E::Mul(a, b) => times(&canonical(a), &canonical(b)),
        E::Neg(a) => plus(&Canonical::new(), &canonical(a), -1),
        E::Sum(inner) => canonical(inner)
            .into_iter()
            .map(|(m, c)| {
                let columns = m
                    .iter()
                    .map(|k| match k {
                        Key::Column(v) => *v,
                        Key::RowSum(_) => unreachable!("no sums inside sums"),
                    })
                    .collect();
                (vec![Key::RowSum(columns)], c)
            })
            .collect(),
    }
}

/// The canonical form's value with the columns `outer` outside aggregates and `rows` inside.
fn evaluate(form: &Canonical, outer: &BTreeMap<u8, i128>, rows: &[BTreeMap<u8, i128>]) -> i128 {
    let key_value = |k: &Key| match k {
        Key::Column(v) => outer.get(v).copied().unwrap_or(0),
        Key::RowSum(m) => rows
            .iter()
            .map(|row| m.iter().map(|v| row[v]).product::<i128>())
            .sum(),
    };
    form.iter()
        .map(|(m, c)| c * m.iter().map(key_value).product::<i128>())
        .sum()
}

fn columns(inputs: &[(Input, Value)]) -> BTreeMap<u8, i128> {
    inputs
        .iter()
        .map(|(input, q)| match input {
            Input::Column(name) => {
                let q = q.expect("a column known not to be NULL is never NULL");
                assert_eq!(q.denominator(), 1);
                (name[1..].parse().unwrap(), q.numerator())
            }
            Input::Expression(m) => panic!("no uninterpreted values here: {m:?}"),
        })
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(400))]

    #[test]
    fn verdicts_agree_with_symbolic_expansion(a in formulas(), b in formulas(), same in any::<bool>()) {
        let b = if same { rewrite(&a) } else { b };
        let mut identity = Formulas::with_key(b"property tests");
        let x = identity.add(&program(&a), "").unwrap();
        let y = identity.add(&program(&b), "").unwrap();
        let verdict = identity.compare(x, y);
        let (ca, cb) = (canonical(&a), canonical(&b));
        prop_assert_eq!(verdict.equal_without_nulls, ca == cb, "{:?} against {:?}", a, b);
        prop_assert!(!verdict.equal || verdict.equal_without_nulls);
        if verdict.equal {
            prop_assert!(verdict.error_bound < 1e-30);
            prop_assert_eq!(identity.fingerprint(x), identity.fingerprint(y));
        } else if !verdict.equal_without_nulls {
            prop_assert_ne!(identity.fingerprint(x), identity.fingerprint(y));
            let witness = verdict.witness.expect("a polynomial difference has values");
            prop_assert_eq!(witness.kind, Kind::Values);
            let outer = columns(&witness.inputs);
            let rows: Vec<BTreeMap<u8, i128>> = witness.rows.iter().map(|r| columns(r)).collect();
            let (va, vb) = witness.results.unwrap();
            let (va, vb) = (va.expect("not NULL"), vb.expect("not NULL"));
            prop_assert_eq!(va.denominator(), 1);
            prop_assert_eq!(evaluate(&ca, &outer, &rows), va.numerator());
            prop_assert_eq!(evaluate(&cb, &outer, &rows), vb.numerator());
            prop_assert_ne!(va, vb);
        }
    }
}
