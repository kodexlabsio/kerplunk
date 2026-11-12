//! The decimal digits of an unsigned 64-bit integer, by hand-written assembly for each
//! architecture Konstruct supports (`src/write/digits/`): a division by ten as a multiplication
//! by its reciprocal, a digit at a time.

#[cfg(target_arch = "x86_64")]
routine!(include_str!("x86_64.asm"), "konstruct_json_digits");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_json_digits");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_json_digits");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_json_digits");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_json_digits");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_json_digits");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_json_digits"]
    fn konstruct_json_digits(value: u64, buffer: *mut u8) -> usize;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_json_digits"]
    fn konstruct_json_digits(value: u64, buffer: *mut u8) -> usize;
}

/// Writes the digits of `value` to the end of `buffer` and returns how many there are: they are
/// `buffer[20 - n..]`.
pub fn of(value: u64, buffer: &mut [u8; 20]) -> usize {
    // SAFETY: the routine writes only the 20 bytes at `buffer` and reads no memory, and returns
    // how many of them are digits, at most 20; it follows the declared calling convention.
    unsafe { konstruct_json_digits(value, buffer.as_mut_ptr()) }
}
