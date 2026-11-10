// Inner product of two signed byte vectors: RISC-V RV64GC.
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   RV64GC, the riscv64gc-unknown-linux-gnu baseline. The vector extension (RVV 1.0) is not in
//   that baseline and many RISC-V processors in use lack it, so the routine is scalar: one
//   implementation for every machine. The multiply is from the M extension, which module-level
//   assembly does not inherit from the target, so `.option arch, +m` names it.
//
// Method
//   Four bytes of each vector at a time, into four independent sums so the multiplies overlap,
//   then the last len % 4 bytes one at a time. `mulw` and `addw` keep each sum a sign-extended
//   32-bit value, which is how the psABI returns a 32-bit integer.
//
// Calling convention
//   The RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     a1  b    pointer to the second vector, likewise
//     a2  len  number of bytes in each
//     a0       the inner product (returned)
//   Clobbers a0 to a7 and t0 to t6, all caller-saved.
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], a byte at a time (so alignment does not arise),
//   and reads nothing when len is 0; the Rust caller passes two slices of equal length, so
//   every byte read is inside a live allocation. It writes no memory, never touches sp or ra,
//   uses no floating-point or vector registers and keeps no state between calls, so it is
//   reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_dot_i8, as with konstruct_fnv1a64. `.p2align 2`
//   aligns the entry to 4 bytes, the size of the uncompressed instructions used here.
//   On ELF targets src/vector/dot/mod.rs adds the symbol's `.type` and `.size` after the
//   routine.

    .text
    .option push
    .option arch, +m
    .globl konstruct_dot_i8
    .p2align 2
konstruct_dot_i8:
    li      a3, 0                       // four sums
    li      a4, 0
    li      a5, 0
    li      a6, 0
    srli    a7, a2, 2                   // whole groups of four bytes
    beqz    a7, 3f
2:
    lb      t0, 0(a0)
    lb      t1, 0(a1)
    lb      t2, 1(a0)
    lb      t3, 1(a1)
    lb      t4, 2(a0)
    lb      t5, 2(a1)
    lb      t6, 3(a0)
    mulw    t0, t0, t1
    lb      t1, 3(a1)
    mulw    t2, t2, t3
    mulw    t4, t4, t5
    mulw    t6, t6, t1
    addw    a3, a3, t0
    addw    a4, a4, t2
    addw    a5, a5, t4
    addw    a6, a6, t6
    addi    a0, a0, 4
    addi    a1, a1, 4
    addi    a7, a7, -1
    bnez    a7, 2b
3:
    addw    a3, a3, a4
    addw    a5, a5, a6
    addw    a3, a3, a5
    andi    a2, a2, 3                   // the bytes after the last whole group
    beqz    a2, 5f
4:
    lb      t0, 0(a0)
    lb      t1, 0(a1)
    mulw    t0, t0, t1
    addw    a3, a3, t0
    addi    a0, a0, 1
    addi    a1, a1, 1
    addi    a2, a2, -1
    bnez    a2, 4b
5:
    mv      a0, a3
    ret
    .option pop
