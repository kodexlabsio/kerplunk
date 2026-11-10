// Inner product of two signed byte vectors: LoongArch64 with LSX.
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   LSX, the 128-bit SIMD extension, which the loongarch64-unknown-linux-gnu target enables in
//   its baseline: one implementation for every machine.
//
// Method
//   Sixteen bytes of each vector at a time (`vld` needs no alignment on LoongArch Linux).
//   `vmulwev.h.b` and `vmulwod.h.b` multiply the even and the odd signed bytes into 16-bit
//   products, `vhaddw.w.h` adds neighbouring products into 32-bit lanes, and `vadd.w` adds
//   them to the sums. Two widening horizontal adds then leave the total in the low 32 bits of
//   the register, and the last len % 16 bytes are added one at a time.
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     $a1  b    pointer to the second vector, likewise
//     $a2  len  number of bytes in each
//     $a0       the inner product, sign-extended to 64 bits as the psABI returns an int
//   Clobbers $a0 to $a4, $t0, $t1 and $vr0 to $vr6. The psABI keeps $f24 to $f31, the low
//   halves of $vr24 to $vr31, which it does not touch.
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], and reads nothing when len is 0; the Rust caller
//   passes two slices of equal length, so every byte read is inside a live allocation. It
//   writes no memory and never touches $sp or $ra, and keeps no state between calls, so it is
//   reentrant and thread safe.
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
    vxor.v      $vr6, $vr6, $vr6        // four 32-bit sums
    srli.d      $a3, $a2, 4             // whole blocks of 16 bytes
    beqz        $a3, 3f
2:
    vld         $vr0, $a0, 0            // 16 bytes of a
    vld         $vr1, $a1, 0            // and of b
    vmulwev.h.b $vr2, $vr0, $vr1        // products of the even bytes, 16 bits each
    vmulwod.h.b $vr3, $vr0, $vr1        // and of the odd
    vhaddw.w.h  $vr2, $vr2, $vr2        // neighbouring products added into 32 bits
    vhaddw.w.h  $vr3, $vr3, $vr3
    vadd.w      $vr6, $vr6, $vr2
    vadd.w      $vr6, $vr6, $vr3
    addi.d      $a0, $a0, 16
    addi.d      $a1, $a1, 16
    addi.d      $a3, $a3, -1
    bnez        $a3, 2b
3:
    vhaddw.d.w  $vr6, $vr6, $vr6        // neighbouring sums into 64 bits,
    vhaddw.q.d  $vr6, $vr6, $vr6        // then both into 128: the total, in the low word
    vpickve2gr.w $a4, $vr6, 0
    andi        $a2, $a2, 15            // the bytes after the last whole block
    beqz        $a2, 5f
4:
    ld.b        $t0, $a0, 0
    ld.b        $t1, $a1, 0
    mul.w       $t0, $t0, $t1
    add.w       $a4, $a4, $t0
    addi.d      $a0, $a0, 1
    addi.d      $a1, $a1, 1
    addi.d      $a2, $a2, -1
    bnez        $a2, 4b
5:
    addi.w      $a0, $a4, 0             // a 32-bit result, sign-extended
    ret
