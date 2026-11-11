// Orchestrator words: LoongArch64 with LSX.
//
// What it does
//   Looks through the `len` bytes at `data` for the words "airflow" and "dagster", and returns
//   which of them occur: bit 0 is set when "airflow" does and bit 1 when "dagster" does. They
//   are the first things Konstruct asks of every Python file in a repository, to find the files
//   that declare Airflow DAGs and Dagster definitions (src/scan/mod.rs), so nearly every file is
//   scanned from end to end and found to hold neither.
//
// How
//   Both words have seven bytes, so a candidate at offset k, for k from 0 to len - 7, is an
//   occurrence of "airflow" when its first byte is `a` and the byte six on is `w`, and of
//   "dagster" when they are `d` and `r`. Sixteen candidates at a time: the bytes at the
//   candidates and the bytes six further on are compared with those four letters, the two
//   comparisons for each word ANDed and the two words ORed, and
//   `vmskltz.b` of the result gives a 16-bit mask of the candidates that match one of the pairs.
//   Each of those is checked in full, a byte at a time, as `ctz.d` finds it. The scan ends as
//   soon as both words are found. When fewer than sixteen candidates are left, one more block
//   ends at the last candidate and overlaps the one before (a candidate seen twice is found
//   twice, which changes nothing). Fewer than sixteen candidates in all are checked one at a time.
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  data  pointer to its first byte (need not be aligned)
//     $a1  len   number of bytes
//     $a0        the words found (returned)
//   Clobbers $a1, $a2, $t0 to $t6, $t8 and $vr0 to $vr7, all caller-saved registers.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the vector loads at candidates 0 to len - 7 and six
//   bytes on cover up to data[len - 1] (the last block ends with the last candidate), and the
//   byte loads of a check are at most six bytes after a candidate. It writes no memory, never
//   touches $sp or $ra (it is a leaf that returns through $ra untouched) and keeps no state
//   between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of every LoongArch
//   instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_gate_words
    .p2align 2
konstruct_gate_words:
    move    $a2, $zero                  // $a2: the words found
    ori     $t0, $zero, 7
    bltu    $a1, $t0, 9f                // fewer than seven bytes hold neither word
    addi.d  $a1, $a1, -7
    add.d   $a1, $a0, $a1               // $a1: the last candidate
    addi.d  $t3, $a0, 15
    bltu    $a1, $t3, 7f                // fewer than 16 candidates: one at a time
    ori     $t0, $zero, 0x61
    vreplgr2vr.b $vr4, $t0              // $vr4: "a", sixteen times
    ori     $t0, $zero, 0x77
    vreplgr2vr.b $vr5, $t0              // $vr5: "w"
    ori     $t0, $zero, 0x64
    vreplgr2vr.b $vr6, $t0              // $vr6: "d"
    ori     $t0, $zero, 0x72
    vreplgr2vr.b $vr7, $t0              // $vr7: "r"
2:                                      // $a0: the block's first candidate
    vld     $vr0, $a0, 0
    vld     $vr1, $a0, 6
    vseq.b  $vr2, $vr0, $vr4
    vseq.b  $vr3, $vr1, $vr5
    vand.v  $vr2, $vr2, $vr3            // candidates that start and end like "airflow"
    vseq.b  $vr0, $vr0, $vr6
    vseq.b  $vr1, $vr1, $vr7
    vand.v  $vr0, $vr0, $vr1            // and like "dagster"
    vor.v   $vr0, $vr0, $vr2
    vmskltz.b $vr0, $vr0
    vpickve2gr.hu $t3, $vr0, 0          // bit k: candidate k starts and ends like a word
    beqz    $t3, 5f
3:
    ctz.d   $t4, $t3
    add.d   $t4, $a0, $t4               // $t4: the candidate
    ld.bu   $t5, $t4, 0
    ori     $t6, $zero, 0x61
    bne     $t5, $t6, 14f
    ld.bu   $t5, $t4, 1
    ori     $t6, $zero, 0x69
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 2
    ori     $t6, $zero, 0x72
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 3
    ori     $t6, $zero, 0x66
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 4
    ori     $t6, $zero, 0x6c
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 5
    ori     $t6, $zero, 0x6f
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 6
    ori     $t6, $zero, 0x77
    bne     $t5, $t6, 6f
    ori     $a2, $a2, 1
    b       15f
14:
    ori     $t6, $zero, 0x64
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 1
    ori     $t6, $zero, 0x61
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 2
    ori     $t6, $zero, 0x67
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 3
    ori     $t6, $zero, 0x73
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 4
    ori     $t6, $zero, 0x74
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 5
    ori     $t6, $zero, 0x65
    bne     $t5, $t6, 6f
    ld.bu   $t5, $t4, 6
    ori     $t6, $zero, 0x72
    bne     $t5, $t6, 6f
    ori     $a2, $a2, 2
15:
6:
    ori     $t0, $zero, 3
    beq     $a2, $t0, 9f                // both found
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
    ld.bu   $t5, $a0, 0
    ori     $t6, $zero, 0x61
    bne     $t5, $t6, 16f
    ld.bu   $t5, $a0, 1
    ori     $t6, $zero, 0x69
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 2
    ori     $t6, $zero, 0x72
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 3
    ori     $t6, $zero, 0x66
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 4
    ori     $t6, $zero, 0x6c
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 5
    ori     $t6, $zero, 0x6f
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 6
    ori     $t6, $zero, 0x77
    bne     $t5, $t6, 11f
    ori     $a2, $a2, 1
    b       17f
16:
    ori     $t6, $zero, 0x64
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 1
    ori     $t6, $zero, 0x61
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 2
    ori     $t6, $zero, 0x67
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 3
    ori     $t6, $zero, 0x73
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 4
    ori     $t6, $zero, 0x74
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 5
    ori     $t6, $zero, 0x65
    bne     $t5, $t6, 11f
    ld.bu   $t5, $a0, 6
    ori     $t6, $zero, 0x72
    bne     $t5, $t6, 11f
    ori     $a2, $a2, 2
17:
11:
    ori     $t0, $zero, 3
    beq     $a2, $t0, 9f
    addi.d  $a0, $a0, 1
    bgeu    $a1, $a0, 7b
9:
    move    $a0, $a2
    ret
