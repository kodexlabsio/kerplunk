//! The parts of Konstruct that run natively: graph traversal, name resolution and search.
//!
//! The Python package `konstruct` keeps its whole API; these modules hold the work that is
//! faster as compiled code, and the `python` feature exposes them as `konstruct._core`. Without
//! that feature the crate builds and tests as plain Rust.

/// Assembles one routine: `$source` is its file, `include_str!("<arch>.asm")` beside the module
/// that declares it, and `$symbol` the name it defines. On ELF targets the symbol also gets its
/// type and size, so debuggers, profilers and `nm -S` see a function of known length; these are
/// ELF directives, which the routines leave out so that each assembles for Mach-O and COFF too.
/// `.` is the end of the routine, as each file ends in its `.text` section.
macro_rules! routine {
    ($source:expr, $symbol:literal) => {
        #[cfg(any(target_vendor = "apple", target_os = "windows"))]
        core::arch::global_asm!($source);
        #[cfg(not(any(target_vendor = "apple", target_os = "windows")))]
        core::arch::global_asm!(
            $source,
            concat!(".type ", $symbol, ", %function"),
            concat!(".size ", $symbol, ", . - ", $symbol),
        );
    };
}

pub mod fnv;
pub mod graph;
pub mod keys;
pub mod names;
pub mod search;

#[cfg(feature = "python")]
mod python;
