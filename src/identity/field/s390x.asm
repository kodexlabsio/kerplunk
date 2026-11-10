// Montgomery multiplication modulo an odd 64-bit modulus: IBM Z (z/Architecture, s390x).
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
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  a      first factor, below p
//     r3  b      second factor, below p
//     r4  p      the modulus: odd, below 2^62
//     r5  ninv   -p^-1 mod 2^64
//     r2         the product, below p (returned)
//   Clobbers r0, r1, r3, r5 and the condition code, which the ABI lets a callee change.
//   mlgr multiplies r1 by its operand into the even-odd pair r0:r1 (high:low); msgr keeps the
//   low 64 bits of a product, which are the same for signed and unsigned operands.
//
// Why it is safe
//   It reads and writes no memory, needs no register save area, never touches r15 (the stack
//   pointer) or r14, the return address it branches to, uses no floating-point or vector
//   registers and keeps no state, so it is reentrant and thread safe. t + carry stays below
//   2^63, so nothing overflows. z/Architecture's byte order does not matter: nothing is
//   loaded from memory.
//
// Symbol, section and alignment
//   The plain name konstruct_montmul, the name Rust links to. `.p2align 3` aligns the entry to
//   8 bytes. On ELF targets src/identity/field/mod.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_montmul
    .p2align 3
konstruct_montmul:
    lgr     %r1, %r2
    mlgr    %r0, %r3                    // r0:r1 = T = a * b
    lgr     %r2, %r1                    // T low
    msgr    %r1, %r5                    // m = T low * ninv, modulo 2^64
    lgr     %r3, %r0                    // T high
    mlgr    %r0, %r4                    // r0 = (m * p) high
    algr    %r3, %r0                    // T high + (m * p) high
    ltgr    %r2, %r2                    // the carry from the low halves: T low != 0
    je      2f
    aghi    %r3, 1
2:
    lgr     %r2, %r3                    // t
    clgr    %r3, %r4
    jl      3f                          // t < p: return t
    slgr    %r2, %r4                    // else t - p
3:
    br      %r14
