//! The parts of Konstruct that run natively: graph traversal, name resolution, search and
//! formula identity.
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
pub mod identity;
pub mod keys;
pub mod names;
pub mod search;
pub mod substring;
pub mod words;

#[cfg(test)]
mod testing;

#[cfg(feature = "python")]
mod python;

/// Why this processor cannot run the core, or `None` when it can.
///
/// The routines use the vector instructions each architecture's baseline guarantees, and every
/// baseline is checked when the crate is built except s390x's: Rust's target assumes a z10, while
/// the routines use the vector facility of z13 and later. An older machine, such as a z196 or
/// zEC12 running a distribution built for it, would stop with an illegal instruction, so the
/// extension refuses to load there instead.
pub fn unsupported_processor() -> Option<&'static str> {
    #[cfg(all(target_arch = "s390x", target_os = "linux"))]
    {
        // unsigned long, 64 bits on s390x.
        unsafe extern "C" {
            fn getauxval(kind: u64) -> u64;
        }
        const AT_HWCAP: u64 = 16;
        // SAFETY: getauxval reads the auxiliary vector the kernel gave the process; it has no
        // preconditions and returns 0 for an entry it does not have.
        s390x_without_vectors(unsafe { getauxval(AT_HWCAP) })
    }
    #[cfg(not(all(target_arch = "s390x", target_os = "linux")))]
    {
        None
    }
}

/// The refusal for an s390x whose Linux hardware capabilities (`AT_HWCAP`) are `hwcap`.
#[cfg_attr(not(target_arch = "s390x"), allow(dead_code))]
fn s390x_without_vectors(hwcap: u64) -> Option<&'static str> {
    const HWCAP_S390_VXRS: u64 = 1 << 11;
    (hwcap & HWCAP_S390_VXRS == 0).then_some(
        "Konstruct needs an IBM z13 or later on s390x: this processor has no vector facility",
    )
}

#[cfg(test)]
mod tests {
    #[test]
    fn this_processor_runs_the_core() {
        // The tests run the routines, so the machine running them must be one the core accepts.
        assert_eq!(super::unsupported_processor(), None);
    }

    #[test]
    fn an_s390x_without_the_vector_facility_is_refused() {
        // A z13's capabilities include VXRS (bit 11); a zEC12's stop at TE (bit 10).
        assert_eq!(super::s390x_without_vectors(0x3fff), None);
        assert!(
            super::s390x_without_vectors(0x07ff)
                .unwrap()
                .contains("z13")
        );
    }
}
