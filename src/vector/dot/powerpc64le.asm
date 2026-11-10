// Inner product of two signed byte vectors: 64-bit little-endian Power with VSX (POWER8).
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   VSX and AltiVec as POWER8 has them, the powerpc64le baseline: one implementation for every
//   machine the wheels run on.
//
// Method
//   Sixteen bytes of each vector at a time, loaded with `lxvd2x`, which needs no alignment. On
//   a little-endian machine it swaps the two doublewords, but it swaps both vectors alike and
//   an inner product does not depend on the order of its terms. `vupkhsb` and `vupklsb` widen
//   the bytes to 16 bits with their sign, and `vmsumshm` multiplies the 16-bit lanes and adds
//   neighbouring products into four 32-bit sums, modulo 2^32. The sums are added together by
//   two rotations (`vsldoi`) and adds, which leave the total in every lane, and the last
//   len % 16 bytes are added one at a time.
//
// Calling convention
//   The 64-bit ELF ABI version 2 (ELFv2), declared extern "C" in Rust.
//     r3  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     r4  b    pointer to the second vector, likewise
//     r5  len  number of bytes in each
//     r3       the inner product, sign-extended to 64 bits as ELFv2 returns an int (returned)
//   Clobbers r3 to r9, the count register, cr0 and v0 to v7, all volatile under ELFv2 (v20 to
//   v31, which it keeps, are not touched).
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], and reads nothing when len is 0; the Rust caller
//   passes two slices of equal length, so every byte read is inside a live allocation. It
//   writes no memory, has no stack frame and never touches r1, r2 or the link register,
//   through which it returns, and keeps no state between calls, so it is reentrant and thread
//   safe. As it uses no TOC, it needs no `.localentry`.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_dot_i8, as with konstruct_fnv1a64. `.p2align 4`
//   aligns the entry to 16 bytes.
//   On ELF targets src/vector/dot/mod.rs adds the symbol's `.type` and `.size` after the
//   routine.

    .text
    .globl konstruct_dot_i8
    .p2align 4
konstruct_dot_i8:
    vxor    6, 6, 6                     // four 32-bit sums
    srdi.   6, 5, 4                     // whole blocks of 16 bytes
    beq     0, 3f
    mtctr   6
    li      7, 0                        // the offset of the next block
2:
    lxvd2x  32, 3, 7                    // 16 bytes of a into v0
    lxvd2x  33, 4, 7                    // and of b into v1
    vupkhsb 2, 0                        // half of a's bytes, widened with their sign
    vupklsb 3, 0                        // the other half
    vupkhsb 4, 1                        // and of b
    vupklsb 5, 1
    vmsumshm 6, 2, 4, 6                 // products, neighbours added, into the sums
    vmsumshm 6, 3, 5, 6
    addi    7, 7, 16
    bdnz    2b
    add     3, 3, 7                     // past the whole blocks
    add     4, 4, 7
3:
    vsldoi  7, 6, 6, 8                  // the sums rotated by two lanes, added,
    vadduwm 6, 6, 7
    vsldoi  7, 6, 6, 4                  // then by one: every lane holds the total
    vadduwm 6, 6, 7
    mfvsrwz 8, 38                       // one lane of v6 (VSX register 38)
    andi.   5, 5, 15                    // the bytes after the last whole block
    beq     0, 5f
    mtctr   5
4:
    lbz     6, 0(3)
    lbz     7, 0(4)
    extsb   6, 6
    extsb   7, 7
    mullw   6, 6, 7
    add     8, 8, 6
    addi    3, 3, 1
    addi    4, 4, 1
    bdnz    4b
5:
    extsw   3, 8                        // a 32-bit result, sign-extended
    blr
