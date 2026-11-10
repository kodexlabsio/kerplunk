// Substring test: LoongArch64 with LSX.
//
// What it does
//   Returns 1 when the `needle_len` bytes at `needle` occur in the `haystack_len` bytes at
//   `haystack`, else 0. An empty needle occurs in every haystack, and one longer than the
//   haystack in none. The search index tests labels against query words with it
//   (src/substring/mod.rs).
//
// How
//   With n = needle_len, a match can start at offsets 0 to haystack_len - n, the candidates.
//   Sixteen candidates at a time: the bytes at the candidates are compared with the needle's
//   first byte, and the bytes n - 1 further on with its last, and `vmskltz.b` of the two
//   comparisons ANDed gives a 16-bit mask of the candidates where both match. Each is then
//   compared in full, byte by byte, as `ctz.d` finds it. When fewer than sixteen candidates are
//   left, one more block ends at the last candidate and overlaps the one before (a candidate seen
//   twice fails twice). A haystack with fewer than sixteen candidates is tested one candidate at
//   a time.
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  haystack      pointer to its first byte (need not be aligned)
//     $a1  haystack_len  number of bytes
//     $a2  needle        pointer to its first byte (need not be aligned)
//     $a3  needle_len    number of bytes
//     $a0                1 when the needle occurs, else 0 (returned)
//   Clobbers $a1, $t0 to $t8, $vr0, $vr1, $vr4 and $vr5, all argument or temporary registers a
//   callee may change. It leaves $vr24 to $vr31, whose low halves ($f24 to $f31) a callee must
//   preserve, alone.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first-byte loads at candidates 0 to haystack_len - n, and the last-byte loads
//   n - 1 bytes further, which end at haystack_len - 1. It writes no memory, never touches $sp
//   or $ra (it is a leaf that returns through $ra untouched) and keeps no state between calls,
//   so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of every LoongArch
//   instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_contains
    .p2align 2
konstruct_contains:
    beqz    $a3, 8f                     // an empty needle occurs everywhere
    bltu    $a1, $a3, 9f                // a needle longer than the haystack occurs nowhere
    sub.d   $a1, $a1, $a3
    add.d   $a1, $a0, $a1               // $a1: the last candidate
    addi.d  $t0, $a3, -1                // $t0: the offset of the needle's last byte
    ld.bu   $t1, $a2, 0                 // $t1: the needle's first byte
    ldx.bu  $t2, $a2, $t0               // $t2: its last
    addi.d  $t3, $a0, 15
    bltu    $a1, $t3, 7f                // fewer than 16 candidates: one at a time
    vreplgr2vr.b $vr4, $t1
    vreplgr2vr.b $vr5, $t2
2:                                      // $a0: the block's first candidate
    vld     $vr0, $a0, 0
    vldx    $vr1, $a0, $t0
    vseq.b  $vr0, $vr0, $vr4
    vseq.b  $vr1, $vr1, $vr5
    vand.v  $vr0, $vr0, $vr1
    vmskltz.b $vr0, $vr0
    vpickve2gr.hu $t3, $vr0, 0          // bit k: candidate k matches the first and last bytes
    beqz    $t3, 5f
3:
    ctz.d   $t4, $t3
    add.d   $t4, $a0, $t4               // $t4: the candidate
    ori     $t5, $zero, 1               // $t5: the next byte to compare
4:
    bgeu    $t5, $t0, 8f                // every byte between the first and last matches
    ldx.bu  $t6, $t4, $t5
    ldx.bu  $t7, $a2, $t5
    bne     $t6, $t7, 6f
    addi.d  $t5, $t5, 1
    b       4b
6:
    addi.d  $t8, $t3, -1
    and     $t3, $t3, $t8               // the next candidate in the block
    bnez    $t3, 3b
5:
    addi.d  $a0, $a0, 16
    addi.d  $t3, $a0, 15
    bgeu    $a1, $t3, 2b                // another whole block
    bltu    $a1, $a0, 9f                // no candidates left
    addi.d  $a0, $a1, -15               // the last block ends with the last candidate
    b       2b
7:                                      // one candidate at a time
    ld.bu   $t6, $a0, 0
    bne     $t6, $t1, 11f
    ldx.bu  $t6, $a0, $t0
    bne     $t6, $t2, 11f
    ori     $t5, $zero, 1
10:
    bgeu    $t5, $t0, 8f
    ldx.bu  $t6, $a0, $t5
    ldx.bu  $t7, $a2, $t5
    bne     $t6, $t7, 11f
    addi.d  $t5, $t5, 1
    b       10b
11:
    addi.d  $a0, $a0, 1
    bgeu    $a1, $a0, 7b
9:
    move    $a0, $zero
    ret
8:
    ori     $a0, $zero, 1
    ret
