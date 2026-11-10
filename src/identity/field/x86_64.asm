// Montgomery multiplication modulo an odd 64-bit modulus: x86_64, Intel syntax.
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
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64".
//     rdi  a       first factor, below p
//     rsi  b       second factor, below p
//     rdx  p       the modulus: odd, below 2^62
//     rcx  ninv    -p^-1 mod 2^64
//     rax          the product, below p (returned)
//   Clobbers rdx, r8, r9, r10 and the flags, which System V lets a callee change.
//
// Why it is safe
//   It reads no memory and writes none, touches no stack (a leaf with no pushes or frame, so
//   Windows needs no unwind data), uses no vector registers and keeps no state, so it is
//   reentrant and thread safe. t + carry stays below 2^63, so nothing overflows.
//
// Symbol, section and alignment
//   The plain name konstruct_montmul on ELF, Mach-O and COFF, as the FNV routine explains;
//   `.p2align 4` starts it on a 16-byte boundary. On ELF targets src/identity/field/mod.rs adds
//   the symbol's `.type` and `.size` after the routine.

    .text
    .globl konstruct_montmul
    .p2align 4
konstruct_montmul:
    mov     r8, rdx                     // p, before mul overwrites rdx
    mov     rax, rdi
    mul     rsi                         // rdx:rax = T = a * b
    mov     r9, rdx                     // T high
    mov     r10, rax                    // T low
    imul    rax, rcx                    // m = T low * ninv, modulo 2^64
    mul     r8                          // rdx:rax = m * p
    neg     r10                         // CF = (T low != 0): the carry from the low halves
    adc     r9, rdx                     // t = T high + (m * p) high + carry
    mov     rax, r9
    sub     rax, r8                     // t - p...
    cmovc   rax, r9                     // ...unless t < p
    ret
