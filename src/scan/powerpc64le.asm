// Orchestrator words: 64-bit little-endian Power, POWER8 vector instructions.
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
//   `vbpermq` (POWER8) gathers the result into a 16-bit mask of the candidates that match one of
//   the pairs. `lxvd2x` loads each doubleword in little-endian order, so the bit indices
//   `vbpermq` takes are arranged to match and bit k is candidate k. Each of those is checked in
//   full by two overlapping four-byte loads, at the candidate and three bytes on, against the
//   word's two halves; POWER8 has no count of trailing zeros, so each is isolated with x AND -x
//   and located with `cntlzd`. The scan ends as soon as both words are found. When fewer than
//   sixteen candidates are left, one more block ends at the last candidate and overlaps the one
//   before (a candidate seen twice is found twice, which changes nothing). Fewer than sixteen
//   candidates in all are checked one at a time.
//
// Calling convention
//   The 64-bit ELF ABI version 2 (ELFv2), which every little-endian Power Linux uses, declared
//   extern "C" in Rust.
//     r3  data  pointer to its first byte (need not be aligned)
//     r4  len   number of bytes
//     r3        the words found (returned)
//   Clobbers r0, r4 to r12, v0 to v9 (VSX registers 32 to 41) and cr0, all volatile under ELFv2.
//   It leaves v20 to v31, which a callee must preserve, alone, and does not use VRSAVE, which
//   ELFv2 does not require a callee to maintain.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the vector loads at candidates 0 to len - 7 and six
//   bytes on cover up to data[len - 1] (the last block ends with the last candidate), and the
//   four-byte loads at a candidate and three bytes on end at its sixth byte. It writes no
//   memory, has no stack frame and never touches r1, r2 (the TOC pointer, which it does not
//   need) or the link register, through which it returns, and keeps no state between calls, so
//   it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). As it uses no
//   TOC, its global and local entry points are the same address and it needs no `.localentry`.
//   `.text` is the ELF code section; `.p2align 4` aligns the entry to 16 bytes, as compilers
//   align function entries for POWER's instruction fetch.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_gate_words
    .p2align 4
konstruct_gate_words:
    li      5, 0                        // r5: the words found
    cmpldi  4, 7
    blt     0, 9f                       // fewer than seven bytes hold neither word
    addi    4, 4, -7
    add     4, 3, 4                     // r4: the last candidate
    lis     8, 0x6672
    ori     8, 8, 0x6961                // r8: "airf" as four bytes load
    lis     9, 0x776f
    ori     9, 9, 0x6c66                // r9: "flow"
    lis     10, 0x7367
    ori     10, 10, 0x6164              // r10: "dags"
    lis     11, 0x7265
    ori     11, 11, 0x7473              // r11: "ster"
    addi    6, 3, 15
    cmpld   6, 4
    bgt     0, 7f                       // fewer than 16 candidates: one at a time
    li      7, 6                        // r7: the offset of the second load
    li      6, 0x61
    mtvsrwz 36, 6
    vspltb  4, 4, 7                     // v4: "a", sixteen times
    li      6, 0x77
    mtvsrwz 37, 6
    vspltb  5, 5, 7                     // v5: "w"
    li      6, 0x64
    mtvsrwz 40, 6
    vspltb  8, 8, 7                     // v8: "d"
    li      6, 0x72
    mtvsrwz 41, 6
    vspltb  9, 9, 7                     // v9: "r"
    li      6, 0
    lvsl    6, 0, 6                     // v6: bytes 0 to 15
    vspltisb 7, 3
    vslb    6, 6, 7                     // the index of the first bit of each byte, 0 to 120,
    xxswapd 38, 38                      // with the halves swapped as lxvd2x leaves the data
2:                                      // r3: the block's first candidate
    lxvd2x  32, 0, 3
    lxvd2x  33, 3, 7
    vcmpequb 2, 0, 4
    vcmpequb 3, 1, 5
    vand    2, 2, 3                     // candidates that start and end like "airflow"
    vcmpequb 0, 0, 8
    vcmpequb 1, 1, 9
    vand    0, 0, 1                     // and like "dagster"
    vor     0, 0, 2
    vbpermq 0, 0, 6
    mfvsrd  6, 32                       // bit k: candidate k starts and ends like a word
    cmpldi  6, 0
    beq     0, 5f
3:
    neg     12, 6
    and     12, 12, 6
    cntlzd  12, 12
    subfic  12, 12, 63
    add     12, 3, 12                   // r12: the candidate
    lwz     0, 0(12)                    // its first four bytes
    lwz     12, 3(12)                   // and its last four
    cmplw   0, 8
    bne     0, 4f
    cmplw   12, 9
    bne     0, 4f
    ori     5, 5, 1
    b       10f
4:
    cmplw   0, 10
    bne     0, 10f
    cmplw   12, 11
    bne     0, 10f
    ori     5, 5, 2
10:
    cmpldi  5, 3
    beq     0, 9f                       // both found
    addi    12, 6, -1
    and.    6, 6, 12                    // the next candidate in the block
    bne     0, 3b
5:
    addi    3, 3, 16
    addi    12, 3, 15
    cmpld   12, 4
    ble     0, 2b                       // another whole block
    cmpld   3, 4
    bgt     0, 9f                       // no candidates left
    addi    3, 4, -15                   // the last block ends with the last candidate
    b       2b
7:                                      // one candidate at a time
    lwz     0, 0(3)
    lwz     12, 3(3)
    cmplw   0, 8
    bne     0, 11f
    cmplw   12, 9
    bne     0, 11f
    ori     5, 5, 1
    b       12f
11:
    cmplw   0, 10
    bne     0, 12f
    cmplw   12, 11
    bne     0, 12f
    ori     5, 5, 2
12:
    cmpldi  5, 3
    beq     0, 9f
    addi    3, 3, 1
    cmpld   3, 4
    ble     0, 7b
9:
    mr      3, 5
    blr
