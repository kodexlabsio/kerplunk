// Orchestrator words: z/Architecture (s390x) with the vector facility of z13, GNU syntax.
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
//   `vfeneb` finds the first candidate in the result that matches one of the pairs. It is
//   checked in full by two overlapping four-byte loads, at the candidate and three bytes on,
//   against the word's two halves, and the search goes on after it. The scan ends as soon as both
//   words are found. The last block of fewer than sixteen candidates is loaded with `vll`, which
//   reads only the bytes it is told to.
//
// Calling convention
//   The s390x ELF psABI (the z/Architecture Linux ABI), declared extern "C" in Rust.
//     r2  data  pointer to its first byte (need not be aligned)
//     r3  len   number of bytes
//     r2        the words found (returned)
//   Clobbers r0 to r5 and v16 to v27 and v19, all volatile under the ABI. It leaves r6 to r15
//   and v8 to v15, which a callee must preserve, alone, and so needs no save area.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the loads with `vll` at a candidate and six bytes on
//   take only as many bytes as there are candidates left in the block, so end at data[len - 1]
//   for the last, and the four-byte loads at a candidate and three bytes on end at its sixth
//   byte. It writes no memory, allocates no stack frame, never changes r15 (the stack pointer) or
//   r14, the return address it branches to, and keeps no state between calls, so it is reentrant
//   and thread safe. z/Architecture is big-endian: the four-byte loads are compared with the
//   words' bytes in memory order.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align function
//   entries on this architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .machine push
    .machine z13
    .globl konstruct_gate_words
    .p2align 3
konstruct_gate_words:
    lghi    %r4, 0                      // r4: the words found
    clgfi   %r3, 7
    jl      9f                          // fewer than seven bytes hold neither word
    aghi    %r3, -7
    agr     %r3, %r2                    // r3: the last candidate
    vzero   %v19
    vrepib  %v20, 0x61                  // v20: "a", sixteen times
    vrepib  %v21, 0x77                  // v21: "w"
    vrepib  %v22, 0x64                  // v22: "d"
    vrepib  %v23, 0x72                  // v23: "r"
2:                                      // r2: the next candidate
    lgr     %r0, %r3
    sgr     %r0, %r2                    // candidates left, less one
    jl      9f                          // none
    clgfi   %r0, 15
    jle     3f
    lghi    %r0, 15                     // r0: the index of the last candidate loaded
3:
    la      %r1, 6(%r2)
    vll     %v16, %r0, 0(%r2)
    vll     %v17, %r0, 0(%r1)
    vceqb   %v24, %v16, %v20
    vceqb   %v25, %v17, %v21
    vn      %v24, %v24, %v25            // candidates that start and end like "airflow"
    vceqb   %v26, %v16, %v22
    vceqb   %v27, %v17, %v23
    vn      %v26, %v26, %v27            // and like "dagster"
    vo      %v24, %v24, %v26
    vfeneb  %v18, %v24, %v19            // the index of the first candidate that matches a pair
    vlgvb   %r1, %v18, 7
    clgr    %r1, %r0
    jle     4f
    agr     %r2, %r0                    // none among those loaded
    aghi    %r2, 1
    j       2b
4:
    agr     %r2, %r1                    // r2: the candidate
    llgf    %r5, 0(%r2)                 // its first four bytes
    llgf    %r1, 3(%r2)                 // and its last four
    clfi    %r5, 0x61697266             // "airf"
    jne     5f
    clfi    %r1, 0x666c6f77             // "flow"
    jne     5f
    oill    %r4, 1
    j       6f
5:
    clfi    %r5, 0x64616773             // "dags"
    jne     6f
    clfi    %r1, 0x73746572             // "ster"
    jne     6f
    oill    %r4, 2
6:
    clgfi   %r4, 3
    je      9f                          // both found
    aghi    %r2, 1                      // continue after the candidate
    j       2b
9:
    lgr     %r2, %r4
    br      %r14
    .machine pop
