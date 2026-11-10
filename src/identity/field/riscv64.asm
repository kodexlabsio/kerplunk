// Montgomery multiplication modulo an odd 64-bit modulus: RISC-V RV64GC.
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
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  a      first factor, below p
//     a1  b      second factor, below p
//     a2  p      the modulus: odd, below 2^62
//     a3  ninv   -p^-1 mod 2^64
//     a0         the product, below p (returned)
//   Clobbers t0 to t4, caller-saved temporaries. The multiplies are from the M extension,
//   which the G in RV64GC includes; module-level assembly does not inherit the target's
//   extensions, so `.option arch, +m` names it for this routine. The final subtraction is
//   branch-free: a mask of all ones when t >= p selects p.
//
// Why it is safe
//   It reads and writes no memory, never touches sp or ra (a leaf), uses no floating-point or
//   vector registers and keeps no state, so it is reentrant and thread safe. t + carry stays
//   below 2^63, so nothing overflows.
//
// Symbol, section and alignment
//   The plain name konstruct_montmul, the name Rust links to. `.p2align 2` aligns the entry to
//   4 bytes. On ELF targets src/identity/field/mod.rs adds the symbol's `.type` and `.size`.

    .text
    .option push
    .option arch, +m
    .globl konstruct_montmul
    .p2align 2
konstruct_montmul:
    mul     t0, a0, a1                  // T low
    mulhu   t1, a0, a1                  // T high
    mul     t2, t0, a3                  // m = T low * ninv, modulo 2^64
    mulhu   t3, t2, a2                  // (m * p) high
    snez    t4, t0                      // the carry from the low halves: T low != 0
    add     t1, t1, t3
    add     t1, t1, t4                  // t
    sltu    t4, t1, a2                  // 1 when t < p
    addi    t4, t4, -1                  // all ones when t >= p, else zero
    and     t4, t4, a2
    sub     a0, t1, t4                  // t - p, or t
    ret
    .option pop
