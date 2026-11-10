// Montgomery multiplication modulo an odd 64-bit modulus: 64-bit little-endian Power ISA
// (powerpc64le, POWER8 and later).
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
//   The ELFv2 ABI that powerpc64le Linux uses, declared extern "C" in Rust.
//     r3  a      first factor, below p
//     r4  b      second factor, below p
//     r5  p      the modulus: odd, below 2^62
//     r6  ninv   -p^-1 mod 2^64
//     r3         the product, below p (returned)
//   Clobbers r7 to r10, volatile registers, the carry bit and cr0. Registers are written as
//   bare numbers, as the FNV routine's are. addic by -1 sets the carry bit exactly when its
//   operand is not zero, and addze adds that carry.
//
// Why it is safe
//   It reads and writes no memory: a leaf with no stack frame, r1 and the link register
//   untouched, no TOC use, no floating-point or vector registers and no state kept, so it is
//   reentrant and thread safe. t + carry stays below 2^63, so nothing overflows.
//
// Symbol, section and alignment
//   The plain name konstruct_montmul, the name Rust links to. `.p2align 4` aligns the entry to
//   16 bytes. On ELF targets src/identity/field/mod.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_montmul
    .p2align 4
konstruct_montmul:
    mulld   7, 3, 4                     // T low
    mulhdu  8, 3, 4                     // T high
    mulld   9, 7, 6                     // m = T low * ninv, modulo 2^64
    mulhdu  10, 9, 5                    // (m * p) high
    addic   7, 7, -1                    // carry bit: T low != 0
    addze   8, 8                        // T high + carry
    add     8, 8, 10                    // t
    cmpld   8, 5
    subf    3, 5, 8                     // t - p...
    bgelr                               // ...when t >= p
    mr      3, 8                        // else t
    blr
