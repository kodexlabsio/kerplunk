//! Known identities and non-identities, written in the program notation.

use super::field::{LANES, Modulus};
use super::rational::Q;
use super::witness::{Input, Kind, Value};
use super::*;

fn col(name: &str) -> String {
    format!("(col \"{name}\")")
}

/// A column known not to be NULL.
fn known(name: &str) -> String {
    format!("(present (col \"{name}\"))")
}

fn num(n: i64, d: i64) -> String {
    format!("(num \"{n}\" \"{d}\")")
}

fn int(n: i64) -> String {
    num(n, 1)
}

fn big(digits: &str) -> String {
    format!("(num \"{digits}\" \"1\")")
}

fn op(name: &str, args: &[&str]) -> String {
    format!("({name} {})", args.join(" "))
}

fn named(kind: &str, name: &str, args: &[&str]) -> String {
    format!("({kind} \"{name}\" {})", args.join(" "))
}

fn add(args: &[&str]) -> String {
    op("add", args)
}

fn mul(args: &[&str]) -> String {
    op("mul", args)
}

fn sub(a: &str, b: &str) -> String {
    op("sub", &[a, b])
}

fn div(a: &str, b: &str) -> String {
    op("div", &[a, b])
}

fn sum(x: &str) -> String {
    op("sum", &[x])
}

fn case(parts: &[&str]) -> String {
    op("case", parts)
}

fn gt(a: &str, b: &str) -> String {
    named("fn", "GT", &[a, b])
}

const NULL: &str = "(lit \"null\" \"\")";
const COUNT: &str = "(count)";

fn verdict(a: &str, b: &str) -> Verdict {
    verdict_in(a, "", b, "")
}

fn verdict_in(a: &str, context_a: &str, b: &str, context_b: &str) -> Verdict {
    let mut formulas = Formulas::with_key(b"tests");
    let x = formulas.add(a, context_a).unwrap();
    let y = formulas.add(b, context_b).unwrap();
    formulas.compare(x, y)
}

fn assert_same(a: &str, b: &str) {
    let found = verdict(a, b);
    assert!(found.equal, "{a} and {b} should be the same: {found:?}");
    assert!(found.equal_without_nulls);
    assert!(
        found.error_bound > 0.0 && found.error_bound < 1e-30,
        "{found:?}"
    );
}

fn assert_different(a: &str, b: &str) -> Witness {
    let found = verdict(a, b);
    assert!(!found.equal, "{a} and {b} should differ");
    assert_eq!(found.error_bound, 0.0);
    found.witness.expect("a witness")
}

/// Different, but the same without NULLs or empty groups.
fn assert_same_without_nulls(a: &str, b: &str) -> Witness {
    let found = verdict(a, b);
    assert!(!found.equal, "{a} and {b} should differ with NULLs");
    assert!(
        found.equal_without_nulls,
        "{a} and {b} should be the same without NULLs"
    );
    found.witness.expect("a witness")
}

fn column(inputs: &[(Input, Value)], name: &str) -> Value {
    inputs
        .iter()
        .find(|(input, _)| matches!(input, Input::Column(n) if n == name))
        .map(|(_, q)| *q)
        .unwrap_or_else(|| panic!("no value for {name} in {inputs:?}"))
}

fn q(n: i64) -> Value {
    Some(Q::integer(n))
}

#[test]
fn algebraic_rewrites() {
    let (a, b, c) = (col("a"), col("b"), col("c"));
    // (a + b)^2 = a^2 + 2ab + b^2, NULL exactly when a or b is
    assert_same(
        &op("pow", &[&add(&[&a, &b]), "2"]),
        &add(&[
            &mul(&[&a, &a]),
            &mul(&[&int(2), &a, &b]),
            &op("pow", &[&b, "2"]),
        ]),
    );
    // (a - b)(a + b) = a^2 - b^2
    assert_same(
        &mul(&[&sub(&a, &b), &add(&[&b, &a])]),
        &sub(&mul(&[&a, &a]), &mul(&[&b, &b])),
    );
    // a(b + c) = ab + ca
    assert_same(
        &mul(&[&a, &add(&[&b, &c])]),
        &add(&[&mul(&[&a, &b]), &mul(&[&c, &a])]),
    );
    // a/c + b/c = (a + b)/c
    assert_same(
        &add(&[&div(&a, &c), &div(&b, &c)]),
        &div(&add(&[&a, &b]), &c),
    );
    // 0.1 + 0.2 = 0.3, exactly, and 1.5a = 3a/2
    assert_same(&add(&[&num(1, 10), &num(2, 10)]), &num(3, 10));
    assert_same(
        &mul(&[&num(15, 10), &a]),
        &div(&mul(&[&int(3), &a]), &int(2)),
    );
    assert_same(&sub(&a, &sub(&b, &c)), &add(&[&sub(&a, &b), &c]));
    assert_same(&op("neg", &[&op("neg", &[&a])]), &a);
    // a - a is 0 only where a is not NULL.
    let witness = assert_same_without_nulls(&sub(&a, &a), &int(0));
    assert_eq!(witness.results, Some((None, q(0))));
    assert_same(&sub(&known("a"), &known("a")), &int(0));
}

#[test]
fn constants_do_not_wrap_around_a_modulus() {
    // Modulo 2^61 - 1 each of these pairs was one formula.
    let a = known("a");
    for (x, y) in [
        (
            mul(&[&a, &int(1_073_741_824), &int(2_147_483_648)]),
            a.clone(),
        ),
        (mul(&[&a, &op("pow", &[&int(2), "61"])]), a.clone()),
        (
            mul(&[&a, &num(1, 2)]),
            mul(&[&a, &int(1_152_921_504_606_846_976)]),
        ),
        (mul(&[&a, &big("2305843009213693951")]), int(0)),
        (mul(&[&a, &big("2305843009213693952")]), a.clone()),
        (
            mul(&[&a, &big("10000000000000000000")]),
            mul(&[&a, &num(1, 10)]),
        ),
    ] {
        let witness = assert_different(&x, &y);
        assert_eq!(witness.kind, Kind::Values, "{x} against {y}");
        let (va, vb) = witness.results.unwrap();
        assert_ne!(va, vb);
    }
    // ... while true identities between large constants still hold.
    assert_same(
        &mul(&[&a, &op("pow", &[&int(2), "61"])]),
        &mul(&[&a, &big("2305843009213693952")]),
    );
    assert_same(
        &mul(&[&a, &big("1000000000000000000000000000000")]),
        &mul(&[&a, &op("pow", &[&int(10), "30"])]),
    );
}

#[test]
fn uninterpreted_functions_are_congruent() {
    let (a, b) = (col("a"), col("b"));
    assert_same(
        &named("fn", "ROUND", &[&add(&[&a, &b]), &int(2)]),
        &named("fn", "ROUND", &[&add(&[&b, &a]), &int(2)]),
    );
    assert_same(
        &named("sym", "EQ", &[&a, &b]),
        &named("sym", "EQ", &[&b, &a]),
    );
    let witness = assert_different(&named("fn", "GT", &[&a, &b]), &named("fn", "GT", &[&b, &a]));
    assert_eq!(witness.kind, Kind::Functions);
    assert_eq!(witness.functions, ["GT"]);
}

#[test]
fn case_rewrites() {
    let (a, b, c, d) = (col("a"), col("b"), col("c"), col("d"));
    let condition = gt(&c, &int(0));
    // CASE WHEN c THEN a ELSE b END + d = CASE WHEN c THEN a + d ELSE b + d END
    assert_same(
        &add(&[&case(&[&condition, &a, &b]), &d]),
        &case(&[&condition, &add(&[&a, &d]), &add(&[&b, &d])]),
    );
    assert_same(&case(&[&condition, &a, &a]), &a);
    // A second WHEN with the same condition is never taken.
    assert_same(
        &case(&[&condition, &a, &condition, &b, &d]),
        &case(&[&condition, &a, &d]),
    );
    let witness = assert_different(&case(&[&condition, &a, &b]), &case(&[&condition, &b, &a]));
    assert_eq!(witness.kind, Kind::Values);
    let (x, y) = witness.results.unwrap();
    let holds = witness
        .inputs
        .iter()
        .find(|(input, _)| matches!(input, Input::Expression(m) if m.name == "CASE"))
        .map(|(_, q)| *q)
        .unwrap();
    let (va, vb) = (column(&witness.inputs, "a"), column(&witness.inputs, "b"));
    assert_eq!((x, y), if holds == q(1) { (va, vb) } else { (vb, va) });
}

#[test]
fn a_null_from_case_is_null_in_arithmetic() {
    let (a, b, c) = (known("a"), known("b"), col("c"));
    let some = case(&[&gt(&c, &int(0)), &a, NULL]);
    // CASE WHEN c > 0 THEN a END * 0 is NULL when c > 0 does not hold, not 0.
    let witness = assert_different(&mul(&[&some, &int(0)]), &int(0));
    assert_eq!(witness.kind, Kind::Values);
    assert_eq!(witness.results, Some((None, q(0))));
    // NULL itself is never an input to choose a value for.
    assert!(
        witness
            .inputs
            .iter()
            .all(|(i, _)| !matches!(i, Input::Expression(m) if m.name == "NULL"))
    );
    // b + CASE.. - CASE.. is NULL when the CASE is, so its SUM skips those rows.
    let witness = assert_different(&sum(&sub(&add(&[&b, &some]), &some)), &sum(&b));
    assert_eq!(witness.kind, Kind::Values);
    // COALESCE(MAX(CASE..) * 0, 5) is 5 when the MAX is NULL, else 0.
    let max = named("agg", "MAX", &[&some]);
    let found = verdict(
        &op("coalesce", &[&mul(&[&max, &int(0)]), &int(5)]),
        &op("coalesce", &[&int(0), &int(5)]),
    );
    assert!(!found.equal && !found.equal_without_nulls);
    // COALESCE itself.
    assert_same(&op("coalesce", &[&known("x"), &int(0)]), &known("x"));
    assert_same(&op("coalesce", &[NULL, &col("x")]), &col("x"));
    let witness = assert_different(&op("coalesce", &[&col("x"), &int(0)]), &col("x"));
    assert_eq!(witness.results, Some((q(0), None)));
    assert_eq!(column(&witness.inputs, "x"), None);
}

#[test]
fn sums_are_linear_over_the_rows_where_their_argument_is_present() {
    let (a, b) = (known("a"), known("b"));
    assert_same(&sum(&add(&[&a, &b])), &add(&[&sum(&a), &sum(&b)]));
    assert_same(&sum(&mul(&[&int(3), &a])), &mul(&[&int(3), &sum(&a)]));
    assert_same(&sub(&sum(&a), &sum(&b)), &sum(&sub(&a, &b)));
    assert_same(&sum(&div(&a, &int(4))), &div(&sum(&a), &int(4)));
    assert_same(&op("avg", &[&a]), &div(&sum(&a), COUNT));
    let condition = gt(&b, &int(0));
    assert_same(
        &sum(&case(&[&condition, &a, &b])),
        &add(&[
            &sum(&case(&[&condition, &a, &int(0)])),
            &sum(&case(&[&condition, &int(0), &b])),
        ]),
    );
    // With columns that may be NULL these hold only without NULLs.
    let (x, y) = (col("x"), col("y"));
    let witness = assert_same_without_nulls(&sub(&sum(&x), &sum(&y)), &sum(&sub(&x, &y)));
    assert_eq!(witness.kind, Kind::Values);
    assert!(witness.rows.iter().flatten().any(|(_, v)| v.is_none()));
    assert_same_without_nulls(&op("avg", &[&x]), &div(&sum(&x), COUNT));
    assert_same(&op("avg", &[&x]), &div(&sum(&x), &op("countof", &[&x])));
    // SUM(1) is NULL over no rows, COUNT(*) is 0.
    let witness = assert_same_without_nulls(&sum(&int(1)), COUNT);
    assert!(witness.aggregates && witness.rows.is_empty());
    assert_eq!(witness.results, Some((None, q(0))));
    assert_same(&op("countof", &[&a]), COUNT);
    // A row with x means the table has rows: SUM(x) - SUM(0) is NULL exactly when SUM(x) is.
    assert_same(&sum(&x), &sub(&sum(&x), &sum(&int(0))));
    assert_same(
        &mul(&[&sum(&x), &sum(&int(1))]),
        &mul(&[&sum(&int(1)), &sum(&x)]),
    );
}

#[test]
fn a_case_without_else_is_null_where_no_branch_is_taken() {
    let (a, b) = (known("a"), known("b"));
    let condition = gt(&b, &int(0));
    let some = case(&[&condition, &a, NULL]);
    let zero = case(&[&condition, &a, &int(0)]);
    // NULL against 0 when no row satisfies the condition, with or without NULLs.
    let found = verdict(&sum(&some), &sum(&zero));
    assert!(!found.equal && !found.equal_without_nulls);
    let witness = found.witness.unwrap();
    assert_eq!(witness.results.map(|(x, _)| x), Some(None));
    // COUNT(CASE WHEN c THEN 1 END) counts the rows where c holds.
    let ones = case(&[&condition, &int(1), NULL]);
    let flags = case(&[&condition, &int(1), &int(0)]);
    assert_same_without_nulls(&op("countof", &[&ones]), &sum(&flags));
    assert_same(&op("countof", &[&ones]), &op("countof", &[&some]));
    // AVG skips the NULL rows in its count as well.
    assert_same(
        &op("avg", &[&some]),
        &div(&sum(&some), &op("countof", &[&some])),
    );
    let witness = assert_different(&op("avg", &[&some]), &op("avg", &[&zero]));
    assert_eq!(witness.kind, Kind::Values);
}

#[test]
fn products_of_sums_are_not_sums_of_products() {
    let (a, b) = (known("a"), known("b"));
    let witness = assert_different(&sum(&mul(&[&a, &b])), &mul(&[&sum(&a), &sum(&b)]));
    assert_eq!(witness.kind, Kind::Values);
    let (mut products, mut sa, mut sb) = (Q::ZERO, Q::ZERO, Q::ZERO);
    for row in &witness.rows {
        let (x, y) = (column(row, "a").unwrap(), column(row, "b").unwrap());
        products = products.checked_add(x.checked_mul(y).unwrap()).unwrap();
        sa = sa.checked_add(x).unwrap();
        sb = sb.checked_add(y).unwrap();
    }
    assert_eq!(
        witness.results,
        Some((Some(products), Some(sa.checked_mul(sb).unwrap())))
    );
    assert!(witness.rows.len() >= 2);
    let witness = assert_different(&mul(&[&int(2), COUNT]), &add(&[COUNT, &int(1)]));
    assert_eq!(witness.kind, Kind::Values);
}

#[test]
fn aggregates_and_windows_under_other_conditions_are_other_formulas() {
    for formula in [
        sum(&known("amount")),
        named("win", "SUM OVER ()", &[&col("a")]),
        named("win", "ROW_NUMBER OVER (ORDER BY a)", &[]),
        named("agg", "MAX", &[&col("a")]),
    ] {
        let found = verdict_in(&formula, "WHERE: x > 0", &formula, "");
        assert!(!found.equal, "{formula}");
        assert_eq!(found.witness.unwrap().kind, Kind::Conditions);
        assert!(verdict_in(&formula, "GROUP BY: day", &formula, "GROUP BY: day").equal);
    }
    assert!(verdict_in(&col("amount"), "WHERE: x", &col("amount"), "").equal);
    let found = verdict_in(&sum(&known("amount")), "WHERE: x", &sum(&known("tax")), "");
    assert_eq!(found.witness.unwrap().kind, Kind::Values);
}

#[test]
fn uninterpreted_differences_name_the_function() {
    let a = col("a");
    let rounded = named("fn", "ROUND", &[&a, &int(0)]);
    let witness = assert_different(&rounded, &a);
    assert_eq!(witness.kind, Kind::Functions);
    assert_eq!(witness.functions, ["ROUND"]);
    let witness = assert_different(
        &named("fn", "UPPER", &[&named("fn", "TRIM", &[&col("s")])]),
        &named("fn", "UPPER", &[&col("s")]),
    );
    assert_eq!(witness.functions, ["TRIM"]);
    let witness = assert_different(
        &named("fn", "ROUND", &[&add(&[&a, &int(1)])]),
        &named("fn", "ROUND", &[&add(&[&a, &int(2)])]),
    );
    assert_eq!(witness.functions, ["ROUND"]);
    assert_eq!((witness.only_a.len(), witness.only_b.len()), (1, 1));
    let witness = assert_different(&named("agg", "MIN", &[&a]), &named("agg", "MAX", &[&a]));
    assert_eq!(witness.functions, ["MAX", "MIN"]);
    // A shared function result is an input like a column, and its arguments are not.
    let round = named("fn", "ROUND", &[&a]);
    let witness = assert_different(&add(&[&round, &int(1)]), &add(&[&round, &int(2)]));
    assert_eq!(witness.kind, Kind::Values);
    assert!(matches!(&witness.inputs[..], [(Input::Expression(m), _)] if m.name == "ROUND"));
}

#[test]
fn references_are_the_formulas_they_name() {
    let mut formulas = Formulas::new();
    let inner = formulas
        .add(&mul(&[&known("price"), &known("quantity")]), "")
        .unwrap();
    let outer = formulas
        .add(&sum(&sub("(ref 0)", &known("discount"))), "c")
        .unwrap();
    let inline = formulas
        .add(
            &sub(
                &sum(&mul(&[&known("quantity"), &known("price")])),
                &sum(&known("discount")),
            ),
            "c",
        )
        .unwrap();
    assert_eq!(inner, 0);
    assert!(formulas.compare(outer, inline).equal);
    assert_eq!(formulas.fingerprint(outer), formulas.fingerprint(inline));
    assert_ne!(formulas.fingerprint(inner), formulas.fingerprint(outer));
    // A long chain of references evaluates without recursing down it.
    let mut last = formulas.add(&col("x"), "").unwrap();
    for _ in 0..20_000 {
        last = formulas
            .add(&add(&[&format!("(ref {last})"), &int(1)]), "")
            .unwrap();
    }
    let direct = formulas.add(&add(&[&col("x"), &int(20_000)]), "").unwrap();
    assert!(formulas.compare(last, direct).equal);
    let summed = formulas.add(&sum(&format!("(ref {last})")), "").unwrap();
    let count = op("countof", &[&col("x")]);
    let expected = formulas
        .add(&add(&[&sum(&col("x")), &mul(&[&int(20_000), &count])]), "")
        .unwrap();
    assert!(formulas.compare(summed, expected).equal);
}

#[test]
fn verdicts_and_witnesses_do_not_depend_on_the_key() {
    let texts = [
        sum(&add(&[&known("a"), &known("b")])),
        add(&[&sum(&known("b")), &sum(&known("a"))]),
        sum(&mul(&[&known("a"), &known("b")])),
        named("fn", "ROUND", &[&col("a")]),
    ];
    let run = |formulas: &mut Formulas| -> (Vec<u128>, Verdict) {
        for text in &texts {
            formulas.add(text, "").unwrap();
        }
        let prints = (0..texts.len() as u32)
            .map(|d| formulas.fingerprint(d))
            .collect();
        (prints, formulas.compare(0, 2))
    };
    let (first, verdict) = run(&mut Formulas::new());
    assert_eq!(first, run(&mut Formulas::new()).0);
    let (other, other_verdict) = run(&mut Formulas::with_key(b"another key"));
    assert_ne!(first, other);
    let groups = |p: &[u128]| (p[0] == p[1], p[0] == p[2], p[2] == p[3]);
    assert_eq!(groups(&first), (true, false, false));
    assert_eq!(groups(&first), groups(&other));
    assert_eq!(verdict.witness, other_verdict.witness);
}

#[test]
fn a_collision_built_from_the_fixed_key_is_caught_by_a_fresh_one() {
    // With the fixed key public, c can be chosen to agree with a's value in every lane, so
    // a + (a - c) fingerprints as a. That is why comparisons draw a fresh key.
    let mut fixed = Formulas::new();
    let a = fixed.add(&col("a"), "").unwrap();
    let lanes = fixed.value(a, true).lanes;
    let moduli: [Modulus; LANES] = fixed.oracle.field.moduli;
    let values: Vec<u128> = (0..LANES)
        .map(|i| u128::from(moduli[i].value(lanes.0[i])))
        .collect();
    let primes: Vec<u128> = moduli.iter().map(|m| u128::from(m.p())).collect();
    let crafted = add(&[&col("a"), &sub(&col("a"), &big(&crt(&values, &primes)))]);
    let b = fixed.add(&crafted, "").unwrap();
    assert_eq!(fixed.fingerprint(a), fixed.fingerprint(b));
    let mut fresh = Formulas::with_key(b"a key drawn at random");
    let (x, y) = (
        fresh.add(&col("a"), "").unwrap(),
        fresh.add(&crafted, "").unwrap(),
    );
    // Different; the constant is too large for the exact values a witness shows.
    assert!(!fresh.compare(x, y).equal);
}

/// The number below the product of the primes that leaves `values` modulo them, in decimal.
fn crt(values: &[u128], primes: &[u128]) -> String {
    let inverse = |a: u128, p: u128| -> u128 {
        let (mut result, mut base, mut exponent) = (1u128, a % p, p - 2);
        while exponent > 0 {
            if exponent & 1 == 1 {
                result = result * base % p;
            }
            base = base * base % p;
            exponent >>= 1;
        }
        result
    };
    // Each prime is below 2^61, so products of two residues fit in 128 bits.
    let (p0, p1, p2) = (primes[0], primes[1], primes[2]);
    let k1 = (values[1] + p1 - values[0] % p1) % p1 * inverse(p0 % p1, p1) % p1;
    let x1 = values[0] + p0 * k1;
    let k2 = (values[2] + p2 - x1 % p2) % p2 * inverse(p0 % p2 * (p1 % p2) % p2, p2) % p2;
    // x = x1 + p0 p1 k2 can pass 2^128, so it is added up in decimal.
    let mut digits = x1.to_string();
    let mut step = (p0 * p1).to_string();
    let mut k = k2;
    while k > 0 {
        if k & 1 == 1 {
            digits = add_decimal(&digits, &step);
        }
        step = add_decimal(&step, &step);
        k >>= 1;
    }
    digits
}

fn add_decimal(a: &str, b: &str) -> String {
    let digits = |s: &str| -> Vec<u8> { s.bytes().rev().map(|d| d - b'0').collect() };
    let (a, b) = (digits(a), digits(b));
    let mut out = Vec::new();
    let mut carry = 0;
    for i in 0..a.len().max(b.len()) {
        let s = a.get(i).copied().unwrap_or(0) + b.get(i).copied().unwrap_or(0) + carry;
        out.push(s % 10 + b'0');
        carry = s / 10;
    }
    if carry > 0 {
        out.push(carry + b'0');
    }
    out.reverse();
    String::from_utf8(out).unwrap()
}

#[test]
fn a_divisor_that_vanishes_gives_no_guarantee() {
    let (a, b) = (known("a"), known("b"));
    let found = verdict(&div(&a, &sub(&b, &b)), &div(&b, &sub(&a, &a)));
    assert_eq!(found.error_bound, 1.0);
}

#[test]
fn a_case_of_many_conditions() {
    // Repeated conditions are found with a hash set, so 2,000 of them take linear time.
    let x = col("x");
    let mut parts: Vec<String> = Vec::new();
    for i in 0..2000 {
        parts.push(named("sym", "EQ", &[&x, &int(i % 1500)]));
        parts.push(col(&format!("c{i}")));
    }
    parts.push(int(0));
    let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
    let mut formulas = Formulas::new();
    let a = formulas.add(&case(&refs), "").unwrap();
    // The last 500 WHENs repeat earlier conditions and are never taken.
    let b = formulas
        .add(&case(&[&refs[..3000], &refs[4000..]].concat()), "")
        .unwrap();
    assert!(formulas.compare(a, b).equal);
}

#[test]
fn presences_kept_whole_stay_apart_by_what_they_stand_for() {
    // Past the limit on terms, the presence of a sum or product and that of a COALESCE of the
    // same operands are atoms of their own: at x1 = 1 and the rest NULL the first is NULL and
    // the second is 0.
    let (x, y) = (
        op("coalesce", &[&col("x1"), &col("x2")]),
        op("coalesce", &[&col("y1"), &col("y2")]),
    );
    let either = mul(&[&op("coalesce", &[&x, &y]), &int(0)]);
    for both in [mul(&[&add(&[&x, &y]), &int(0)]), mul(&[&x, &y, &int(0)])] {
        let found = verdict(&both, &either);
        assert!(!found.equal, "{both} and {either} should differ");
        assert!(found.equal_without_nulls);
    }
}

#[test]
fn a_branch_that_is_always_null_gives_no_value() {
    let (a, b, c, d) = (
        col("a"),
        col("b"),
        gt(&col("c"), &int(0)),
        gt(&col("d"), &int(0)),
    );
    // NULL wherever the branch is not taken, a + 1 wherever it is.
    assert_same(
        &add(&[&case(&[&c, &a, NULL]), &int(1)]),
        &case(&[&c, &add(&[&a, &int(1)]), NULL]),
    );
    assert_same(
        &add(&[&case(&[&c, NULL, &a]), &int(1)]),
        &case(&[&c, NULL, &add(&[&a, &int(1)])]),
    );
    assert_same(
        &mul(&[&case(&[&c, &a, &d, NULL, &b]), &int(2)]),
        &case(&[&c, &mul(&[&int(2), &a]), &d, NULL, &mul(&[&b, &int(2)])]),
    );
    assert_same(
        &op("coalesce", &[&case(&[&c, &a, NULL]), &int(0)]),
        &case(&[&c, &op("coalesce", &[&a, &int(0)]), &int(0)]),
    );
    // The branches that are not NULL still decide the value.
    assert_different(
        &add(&[&case(&[&c, &a, &d, &b, NULL]), &int(1)]),
        &case(&[&c, &add(&[&a, &int(1)]), &d, &add(&[&a, &int(1)]), NULL]),
    );
}

#[test]
fn a_column_read_through_an_outer_join_may_be_null() {
    let (amount, credit) = (known("amount"), known("credit"));
    let (joined, kept) = (op_outer("orders", &amount), credit.clone());
    // Never NULL in its own table, but NULL where a customer has no order.
    assert_same_without_nulls(&joined, &amount);
    let witness =
        assert_same_without_nulls(&sub(&sum(&joined), &sum(&kept)), &sum(&sub(&joined, &kept)));
    assert_eq!(witness.kind, Kind::Values);
    assert!(witness.rows.iter().any(|row| row.iter().any(
        |(input, value)| matches!(input, Input::Expression(m) if m.name == "orders")
            && *value == q(0)
    )));
    // Read twice through the same join, it is the same column.
    assert_same(&add(&[&joined, &int(0)]), &joined);
    assert_same(
        &sum(&mul(&[&int(2), &joined])),
        &mul(&[&sum(&joined), &int(2)]),
    );
}

fn op_outer(key: &str, x: &str) -> String {
    format!("(outer \"{key}\" {x})")
}
