// Inner product of two signed byte vectors: IBM Z with the z/Architecture vector facility.
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   The vector facility of z13 (2015), named for the assembler with `.machine z13` between
//   `.machine push` and `.machine pop`, so the rest of the crate's assembly keeps the target's
//   own. Rust's s390x target assumes only z10, and a manylinux_2_28 wheel installs on any
//   machine with glibc 2.28 or newer, whatever its processor (Debian's s390x baseline is z196),
//   so the facility is a requirement of Konstruct on s390x: konstruct._core refuses to load,
//   with an error that says so, when the kernel does not report it (src/lib.rs,
//   `unsupported_processor`). There is one implementation.
//
// Method
//   Sixteen bytes of each vector at a time (`vl` needs no alignment). `vuphb` and `vuplb`
//   widen the bytes to 16 bits with their sign; `vmaeh` and `vmaoh` multiply the even and the
//   odd 16-bit lanes and add the products to four 32-bit sums, modulo 2^32. `vsumqf` adds the
//   four sums into one 128-bit value, whose low 32 bits are the total modulo 2^32, and the last
//   len % 16 bytes are added one at a time. The machine is big-endian, which does not matter:
//   both vectors are loaded alike and the order of the terms does not change the sum.
//
// Calling convention
//   The s390x ELF ABI, declared extern "C" in Rust.
//     r2  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     r3  b    pointer to the second vector, likewise
//     r4  len  number of bytes in each
//     r2       the inner product, sign-extended to 64 bits as the ABI returns an int (returned)
//   Clobbers r0 to r5, v0 to v7 and the condition code. The ABI keeps f8 to f15, the
//   high halves of v8 to v15, which it does not touch.
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], and reads nothing when len is 0; the Rust caller
//   passes two slices of equal length, so every byte read is inside a live allocation. It
//   writes no memory, needs no register save area, never touches r15 or r14, the return
//   address it branches to, and keeps no state between calls, so it is reentrant and thread
//   safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_dot_i8, as with konstruct_fnv1a64. `.p2align 3`
//   aligns the entry to 8 bytes.
//   On ELF targets src/vector/dot/mod.rs adds the symbol's `.type` and `.size` after the
//   routine.

    .text
    .machine push
    .machine z13
    .globl konstruct_dot_i8
    .p2align 3
konstruct_dot_i8:
    vzero   %v6                         // four 32-bit sums
    vzero   %v7
    srlg    %r1, %r4, 4                 // whole blocks of 16 bytes
    ltgr    %r1, %r1
    je      3f
2:
    vl      %v0, 0(%r2)                 // 16 bytes of a
    vl      %v1, 0(%r3)                 // and of b
    vuphb   %v2, %v0                    // the high eight bytes of a, widened with their sign
    vuplb   %v3, %v0                    // the low eight
    vuphb   %v4, %v1                    // and of b
    vuplb   %v5, %v1
    vmaeh   %v6, %v2, %v4, %v6          // even and odd products added into the sums
    vmaoh   %v6, %v2, %v4, %v6
    vmaeh   %v6, %v3, %v5, %v6
    vmaoh   %v6, %v3, %v5, %v6
    aghi    %r2, 16
    aghi    %r3, 16
    brctg   %r1, 2b
3:
    vsumqf  %v6, %v6, %v7               // the four sums as one 128-bit value
    vlgvf   %r5, %v6, 3                 // its low 32 bits
    lghi    %r0, 15                     // the bytes after the last whole block
    ngr     %r4, %r0
    je      5f
4:
    lb      %r0, 0(%r2)                 // a byte of each, sign-extended
    lb      %r1, 0(%r3)
    msr     %r0, %r1
    ar      %r5, %r0
    aghi    %r2, 1
    aghi    %r3, 1
    brctg   %r4, 4b
5:
    lgfr    %r2, %r5                    // a 32-bit result, sign-extended
    br      %r14
    .machine pop
