//! The parts of Konstruct that run natively: graph traversal, name resolution and search.
//!
//! The Python package `konstruct` keeps its whole API; these modules hold the work that is
//! faster as compiled code, and the `python` feature exposes them as `konstruct._core`. Without
//! that feature the crate builds and tests as plain Rust.

pub mod graph;
pub mod names;
pub mod search;

#[cfg(feature = "python")]
mod python;
