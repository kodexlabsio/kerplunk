// Montgomery multiplication modulo an odd 64-bit modulus: AArch64 (ARMv8-A).
//
// What it does
//   Returns a * b * 2^-64 mod p, for an odd p below 2^62 and a, b below p. With elements held
//   in Montgomery form (x * 2^64 mod p), that is their product in the same form. The formula
//   identity test multiplies with it in each of its three prime fields (src/identity/field/mod.rs).
//   REDC: with T = a * b (below 2^124) and m = (T mod 2^64) * (-p^-1) mod 2^64, T + m * p is a
//   multiple of 2^64, and t = (T + m * p) / 2^64 is below 2p, so one conditional subtraction
//   of p brings it below p. The low halves of T and m * p add to exactly 0 or 2^64: the carry
//   into the high half is 1 precisely when the low half of T is not zero.
//
// Calling convention
//   AAPCS64, declared extern "C" in Rust (the same on Linux, macOS and Windows on Arm).
//     x0  a      first factor, below p
//     x1  b      second factor, below p
//     x2  p      the modulus: odd, below 2^62
//     x3  ninv   -p^-1 mod 2^64
//     x0         the product, below p (returned)
//   Clobbers x4 to x7, caller-saved temporaries, and the flags.
//
// Why it is safe
//   It reads and writes no memory, never touches sp or x30 (a leaf), does not use x18, uses no
//   floating-point or SIMD registers and keeps no state, so it is reentrant and thread safe.
//   t + carry stays below 2^63, so nothing overflows.
//
// Symbol, section and alignment
//   The plain name konstruct_montmul, the name Rust links to (with a leading \x01 byte in its
//   link_name, so LLVM uses it as written on Mach-O too). `.p2align 2` aligns the entry to the
//   4-byte instruction size. On ELF targets src/identity/field/mod.rs adds `.type` and `.size`.

    .text
    .globl konstruct_montmul
    .p2align 2
konstruct_montmul:
    mul     x4, x0, x1                  // T low
    umulh   x5, x0, x1                  // T high
    mul     x6, x4, x3                  // m = T low * ninv, modulo 2^64
    umulh   x7, x6, x2                  // (m * p) high
    cmp     x4, #0
    cinc    x5, x5, ne                  // the carry from the low halves: T low != 0
    add     x5, x5, x7                  // t
    subs    x0, x5, x2                  // t - p...
    csel    x0, x0, x5, hs              // ...unless t < p
    ret
