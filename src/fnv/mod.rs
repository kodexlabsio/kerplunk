//! FNV-1a 64-bit hashing, for the hash maps that intern node ids and names.
//!
//! The hash itself is hand-written assembly, one routine for each architecture Konstruct
//! supports, in `src/fnv/`: x86_64, aarch64, riscv64, powerpc64le, s390x and loongarch64. LLVM
//! assembles them through `global_asm!` on every operating system. Node ids are short strings
//! from the user's own project, so a fast, unkeyed hash suits them better than SipHash's
//! resistance to chosen keys.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

// The routines take 64-bit pointers and lengths, so a 32-bit ABI on one of these architectures
// (x86_64's x32, aarch64's ILP32) is refused as well: it would pass 32-bit ones.
#[cfg(not(all(
    target_pointer_width = "64",
    any(
        target_arch = "x86_64",
        target_arch = "aarch64",
        target_arch = "riscv64",
        all(target_arch = "powerpc64", target_endian = "little"),
        target_arch = "s390x",
        target_arch = "loongarch64",
    ),
)))]
compile_error!(
    "Konstruct supports x86_64, aarch64, riscv64, powerpc64le, s390x and loongarch64, with 64-bit pointers"
);

/// The state FNV-1a 64 starts from.
pub const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;

#[cfg(target_arch = "x86_64")]
routine!(include_str!("x86_64.asm"), "konstruct_fnv1a64");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_fnv1a64");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_fnv1a64");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_fnv1a64");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_fnv1a64");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_fnv1a64");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_fnv1a64"]
    fn konstruct_fnv1a64(data: *const u8, len: usize, state: u64) -> u64;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_fnv1a64"]
    fn konstruct_fnv1a64(data: *const u8, len: usize, state: u64) -> u64;
}

/// `state` continued over `bytes`: from [`OFFSET_BASIS`], the FNV-1a 64 hash of `bytes`.
pub fn hash(state: u64, bytes: &[u8]) -> u64 {
    // SAFETY: the routine reads exactly `bytes.len()` bytes from `bytes.as_ptr()`, all inside
    // the slice, writes no memory and follows the declared calling convention.
    unsafe { konstruct_fnv1a64(bytes.as_ptr(), bytes.len(), state) }
}

/// A [`Hasher`] that feeds everything written to it through FNV-1a 64.
#[derive(Clone, Copy, Debug)]
pub struct Fnv1a(u64);

impl Default for Fnv1a {
    fn default() -> Self {
        Self(OFFSET_BASIS)
    }
}

impl Hasher for Fnv1a {
    fn write(&mut self, bytes: &[u8]) {
        self.0 = hash(self.0, bytes);
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

/// A `HashMap` keyed through [`Fnv1a`].
pub type FnvHashMap<K, V> = HashMap<K, V, BuildHasherDefault<Fnv1a>>;

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn published_vectors() {
        // From the FNV-1a 64 test suite of the algorithm's authors.
        for (input, expected) in [
            ("", 0xcbf2_9ce4_8422_2325),
            ("a", 0xaf63_dc4c_8601_ec8c),
            ("b", 0xaf63_df4c_8601_f1a5),
            ("c", 0xaf63_de4c_8601_eff2),
            ("foo", 0xdcb2_7518_fed9_d577),
            ("foob", 0xdd12_0e79_0c25_12af),
            ("fooba", 0xcac1_65af_a2fe_f40a),
            ("foobar", 0x8594_4171_f739_67e8),
        ] {
            assert_eq!(hash(OFFSET_BASIS, input.as_bytes()), expected, "{input:?}");
        }
    }

    #[test]
    fn precomputed_hashes() {
        let every_byte: Vec<u8> = (0..=255).collect();
        assert_eq!(hash(OFFSET_BASIS, &every_byte), 0x4242_dc52_49c3_3625);
        assert_eq!(
            hash(OFFSET_BASIS, "résumé".as_bytes()),
            0x577f_0085_d6e2_ca5c
        );
        assert_eq!(
            hash(OFFSET_BASIS, b"model.big.m010#c01"),
            0xac44_b548_855f_acc9
        );
    }

    #[test]
    fn hashing_a_str_feeds_its_bytes_then_a_terminator() {
        use std::hash::Hash;
        let mut hasher = Fnv1a::default();
        "model.shop.orders".hash(&mut hasher);
        assert_eq!(hasher.finish(), 0x003b_dba0_8ae8_3324);
    }

    proptest! {
        #[test]
        fn hashing_continues_across_parts(bytes in prop::collection::vec(any::<u8>(), 0..300), cut in 0usize..300) {
            let cut = cut.min(bytes.len());
            let (head, tail) = bytes.split_at(cut);
            prop_assert_eq!(hash(hash(OFFSET_BASIS, head), tail), hash(OFFSET_BASIS, &bytes));
        }

        #[test]
        fn distinct_bytes_give_distinct_hashes(state in any::<u64>(), a in any::<u8>(), b in any::<u8>()) {
            // XOR with the byte, then multiplying by an odd prime, are both one to one.
            prop_assert_eq!(hash(state, &[a]) == hash(state, &[b]), a == b);
        }
    }
}
