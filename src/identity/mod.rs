//! Whether two formulas compute the same thing, however differently they are written.
//!
//! Formulas are translated into a straight-line program and evaluated at random points modulo
//! three random primes (randomised polynomial identity testing, by the Schwartz-Zippel lemma);
//! see [`eval`] for what "the same" means and the error it allows. Each formula also gets a
//! 128-bit fingerprint, equal for equal formulas, so any number of them can be grouped with a
//! hash map. Two formulas not shown to be the same come with a [`witness::Witness`] when one is
//! found: values at which they disagree, or the function they disagree on.

pub mod blake2b;
pub mod eval;
pub mod field;
pub mod oracle;
pub mod poly;
pub mod program;
#[cfg(feature = "python")]
pub mod python;
pub mod rational;
pub mod witness;

use eval::{Eval, Memo, Val};
use oracle::Oracle;
pub use program::ParseError;
use program::Program;
pub use witness::Witness;

/// The outcome of comparing two formulas.
#[derive(Clone, Debug, PartialEq)]
pub struct Verdict {
    /// Whether they are shown to be the same, NULLs and empty tables included. False means not
    /// shown to be the same, which is not a proof that they differ (see [`eval`]); a witness of
    /// values is one when it sets source columns alone, as the values it gives an
    /// uninterpreted expression (a CASE condition holding) may be ones it never takes.
    pub equal: bool,
    /// A bound on the probability that `equal` is true by chance although the formulas are not
    /// the same: tiny when it is true, zero when it is false, and 1 when a divisor was zero at
    /// a trial's point, which leaves no guarantee either way.
    pub error_bound: f64,
    /// Whether they are the same when no source value is NULL and every group has rows. True
    /// whenever `equal` is.
    pub equal_without_nulls: bool,
    /// Why they are not the same, when they are not and a reason was found.
    pub witness: Option<Witness>,
}

/// Formulas to fingerprint and compare, added one at a time; see the module.
pub struct Formulas {
    program: Program,
    oracle: Oracle,
    contexts: Vec<[u8; 16]>,
    /// One evaluation with source values that may be NULL, one without NULLs.
    memos: [Memo; 2],
}

impl Default for Formulas {
    fn default() -> Formulas {
        Formulas::with_key(oracle::SEED)
    }
}

impl Formulas {
    /// Formulas under the fixed key, so that their fingerprints agree with every other build's.
    /// A fingerprint match is not a guarantee against a formula built with knowledge of that
    /// key; compare with [`Formulas::with_key`] and a fresh random key for that.
    pub fn new() -> Formulas {
        Formulas::default()
    }

    /// Formulas evaluated at the points, and modulo the primes, that `key` draws.
    pub fn with_key(key: &[u8]) -> Formulas {
        let oracle = Oracle::new(key);
        let memos = [Memo::new(&oracle.field), Memo::new(&oracle.field)];
        Formulas {
            program: Program::new(),
            oracle,
            contexts: Vec::new(),
            memos,
        }
    }

    /// Adds a definition (see [`program`] for how it is written) whose aggregates run under
    /// `context`, and returns its index.
    pub fn add(&mut self, text: &str, context: &str) -> Result<u32, ParseError> {
        let definition = self.program.add(text, context)?;
        while self.contexts.len() < self.program.contexts.len() {
            let context = &self.program.contexts[self.contexts.len()];
            self.contexts.push(self.oracle.context(context));
        }
        Ok(definition)
    }

    pub fn len(&self) -> usize {
        self.program.definitions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.program.definitions.is_empty()
    }

    fn eval(&mut self, nulls: bool) -> Eval<'_> {
        Eval {
            program: &self.program,
            oracle: &self.oracle,
            contexts: &self.contexts,
            memo: &mut self.memos[usize::from(!nulls)],
            nulls,
        }
    }

    /// The definition's value, with source values that may be NULL or without NULLs.
    pub fn value(&mut self, definition: u32, nulls: bool) -> Val {
        self.eval(nulls).definition(definition)
    }

    /// The definition's 128-bit fingerprint: equal for formulas that are the same, NULLs
    /// included.
    pub fn fingerprint(&mut self, definition: u32) -> u128 {
        let value = self.value(definition, true);
        self.oracle.fingerprint(|f| {
            f.lanes(&value.lanes);
            f.number(value.presence.len() as u64);
            for (monomial, c) in value.presence.terms() {
                f.number(monomial.len() as u64);
                for (atom, boolean) in monomial {
                    f.lanes(atom).number(u64::from(*boolean));
                }
                f.lanes(&c);
            }
        })
    }

    pub fn compare(&mut self, a: u32, b: u32) -> Verdict {
        let (x, y) = (self.value(a, true), self.value(b, true));
        if x.same(&y) {
            return Verdict {
                equal: true,
                error_bound: eval::error_bound(&x, &y, &self.oracle.field),
                equal_without_nulls: true,
                witness: None,
            };
        }
        let error_bound = if x.singular || y.singular { 1.0 } else { 0.0 };
        let equal_without_nulls = self.value(a, false).same(&self.value(b, false));
        let (ca, cb) = (
            self.program.definitions[a as usize].context,
            self.program.definitions[b as usize].context,
        );
        if ca != cb {
            // The same formulas under one context: then only the conditions differ.
            let blank = vec![self.oracle.context(""); self.contexts.len()];
            let mut memo = Memo::new(&self.oracle.field);
            let mut eval = Eval {
                program: &self.program,
                oracle: &self.oracle,
                contexts: &blank,
                memo: &mut memo,
                nulls: true,
            };
            if eval.definition(a).same(&eval.definition(b)) {
                return Verdict {
                    equal: false,
                    error_bound,
                    equal_without_nulls,
                    witness: Some(Witness::conditions()),
                };
            }
        }
        let witness = witness::explain(&mut self.eval(true), a, b);
        Verdict {
            equal: false,
            error_bound,
            equal_without_nulls,
            witness,
        }
    }
}

#[cfg(test)]
mod tests;
