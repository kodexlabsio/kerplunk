// Inner product of two signed byte vectors: AArch64 (ARMv8-A) with NEON, GNU syntax.
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   Advanced SIMD (NEON), which every ARMv8-A processor has. The dot-product extension
//   (`sdot`) is optional in ARMv8.0, so it is not used: one implementation for every machine.
//
// Method
//   Sixteen bytes of each vector at a time (`ldr q` needs no alignment): `smull` and `smull2`
//   multiply the low and high eight bytes into 16-bit products, and `sadalp` adds neighbouring
//   products into the 32-bit lanes of two accumulators. The lanes are added together at the
//   end (`addv`) and the last len % 16 bytes are added one at a time.
//
// Calling convention
//   AAPCS64, declared extern "C" in Rust.
//     x0  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     x1  b    pointer to the second vector, likewise
//     x2  len  number of bytes in each
//     w0       the inner product (returned)
//   Clobbers x0 to x5, v0 to v5 and the condition flags. AAPCS64 keeps only the low halves of
//   v8 to v15, which it does not touch, nor x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], with post-indexed loads that need no alignment,
//   and reads nothing when len is 0; the Rust caller passes two slices of equal length, so
//   every byte read is inside a live allocation. It writes no memory and never touches the
//   stack or the link register (a leaf, so it needs no frame record and no unwind data), and
//   keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_dot_i8 on ELF, Mach-O and COFF alike, as with
//   konstruct_fnv1a64. `.p2align 4` aligns the entry and the loop to 16 bytes.
//   On ELF targets src/vector/dot/mod.rs adds the symbol's `.type` and `.size` after the
//   routine.

    .text
    .globl konstruct_dot_i8
    .p2align 4
konstruct_dot_i8:
    movi    v4.4s, #0                   // two accumulators of four 32-bit sums
    movi    v5.4s, #0
    lsr     x3, x2, #4                  // whole blocks of 16 bytes
    cbz     x3, 3f
2:
    ldr     q0, [x0], #16               // 16 bytes of a; advance a
    ldr     q1, [x1], #16               // and of b
    smull   v2.8h, v0.8b, v1.8b         // the low eight products, 16 bits each
    smull2  v3.8h, v0.16b, v1.16b       // the high eight
    sadalp  v4.4s, v2.8h                // neighbouring products added into the sums
    sadalp  v5.4s, v3.8h
    subs    x3, x3, #1
    b.ne    2b
3:
    add     v4.4s, v4.4s, v5.4s
    addv    s4, v4.4s                   // the four lanes together
    fmov    w5, s4
    ands    x2, x2, #15                 // the bytes after the last whole block
    b.eq    5f
4:
    ldrsb   w3, [x0], #1
    ldrsb   w4, [x1], #1
    madd    w5, w3, w4, w5
    subs    x2, x2, #1
    b.ne    4b
5:
    mov     w0, w5
    ret
