// Orchestrator words: AArch64 (ARMv8-A) with NEON, GNU syntax.
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
//   comparisons for each word ANDed and the two words ORed, and the result, narrowed by `shrn` to
//   four bits a candidate, gives a 64-bit mask of the candidates that match one of the pairs.
//   Each of those is checked in full by two overlapping four-byte loads, at the candidate and
//   three bytes on, against the word's two halves, as `rbit` and `clz` find it. The scan ends as
//   soon as both words are found. When fewer than sixteen candidates are left, one more block
//   ends at the last candidate and overlaps the one before (a candidate seen twice is found
//   twice, which changes nothing). Fewer than sixteen candidates in all are checked one at a time.
//
// Calling convention
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  data  pointer to its first byte (need not be aligned)
//     x1  len   number of bytes
//     x0        the words found (returned)
//   Clobbers x1 to x16, v0 to v7 and the condition flags, all of which AAPCS64 lets a callee
//   change. It leaves v8 to v15, whose low halves a callee must preserve, alone, and does not
//   touch x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the vector loads at candidates 0 to len - 7 and six
//   bytes on cover up to data[len - 1] (the last block ends with the last candidate), and the
//   four-byte loads at a candidate and three bytes on end at its sixth byte. It writes no
//   memory, never touches the stack or the link register (it is a leaf, so it needs no frame
//   record and no unwind data) and keeps no state between calls, so it is reentrant and thread
//   safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_gate_words
    .p2align 2
konstruct_gate_words:
    mov     x3, #0                      // x3: the words found
    cmp     x1, #7
    b.lo    9f                          // fewer than seven bytes hold neither word
    sub     x1, x1, #7
    add     x1, x0, x1                  // x1: the last candidate
    mov     w13, #0x6961
    movk    w13, #0x6672, lsl #16       // w13: "airf" as four bytes load
    mov     w14, #0x6c66
    movk    w14, #0x776f, lsl #16       // w14: "flow"
    mov     w15, #0x6164
    movk    w15, #0x7367, lsl #16       // w15: "dags"
    mov     w16, #0x7473
    movk    w16, #0x7265, lsl #16       // w16: "ster"
    add     x4, x0, #15
    cmp     x4, x1
    b.hi    7f                          // fewer than 16 candidates: one at a time
    movi    v4.16b, #0x61               // v4: "a", sixteen times
    movi    v5.16b, #0x77               // v5: "w"
    movi    v6.16b, #0x64               // v6: "d"
    movi    v7.16b, #0x72               // v7: "r"
2:                                      // x0: the block's first candidate
    ldr     q0, [x0]
    ldr     q1, [x0, #6]
    cmeq    v2.16b, v0.16b, v4.16b
    cmeq    v3.16b, v1.16b, v5.16b
    and     v2.16b, v2.16b, v3.16b      // candidates that start and end like "airflow"
    cmeq    v0.16b, v0.16b, v6.16b
    cmeq    v1.16b, v1.16b, v7.16b
    and     v0.16b, v0.16b, v1.16b      // and like "dagster"
    orr     v0.16b, v0.16b, v2.16b
    shrn    v0.8b, v0.8h, #4
    fmov    x5, d0
    and     x5, x5, #0x8888888888888888 // bit 4k + 3: candidate k starts and ends like a word
    cbz     x5, 5f
3:
    rbit    x6, x5
    clz     x6, x6
    add     x6, x0, x6, lsr #2          // x6: the candidate
    ldr     w7, [x6]                    // its first four bytes
    ldr     w8, [x6, #3]                // and its last four
    cmp     w7, w13
    b.ne    4f
    cmp     w8, w14
    b.ne    4f
    orr     x3, x3, #1
    b       6f
4:
    cmp     w7, w15
    b.ne    6f
    cmp     w8, w16
    b.ne    6f
    orr     x3, x3, #2
6:
    cmp     x3, #3
    b.eq    9f                          // both found
    sub     x9, x5, #1
    and     x5, x5, x9                  // the next candidate in the block
    cbnz    x5, 3b
5:
    add     x0, x0, #16
    add     x4, x0, #15
    cmp     x4, x1
    b.ls    2b                          // another whole block
    cmp     x0, x1
    b.hi    9f                          // no candidates left
    sub     x0, x1, #15                 // the last block ends with the last candidate
    b       2b
7:                                      // one candidate at a time
    ldr     w7, [x0]
    ldr     w8, [x0, #3]
    cmp     w7, w13
    b.ne    10f
    cmp     w8, w14
    b.ne    10f
    orr     x3, x3, #1
    b       11f
10:
    cmp     w7, w15
    b.ne    11f
    cmp     w8, w16
    b.ne    11f
    orr     x3, x3, #2
11:
    cmp     x3, #3
    b.eq    9f
    add     x0, x0, #1
    cmp     x0, x1
    b.ls    7b
9:
    mov     x0, x3
    ret
