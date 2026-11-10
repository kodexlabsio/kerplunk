// Substring test: IBM Z (z/Architecture, s390x), z13 vector facility.
//
// What it does
//   Returns 1 when the `needle_len` bytes at `needle` occur in the `haystack_len` bytes at
//   `haystack`, else 0. An empty needle occurs in every haystack, and one longer than the
//   haystack in none. The search index tests labels against query words with it
//   (src/substring/mod.rs).
//
// How
//   With n = needle_len, a match can start at offsets 0 to haystack_len - n, the candidates.
//   Up to sixteen candidates at a time: the bytes at the candidates are compared with the
//   needle's first byte, and the bytes n - 1 further on with its last, and `vfene` (VECTOR FIND
//   ELEMENT NOT EQUAL, against zero) gives the index of the first candidate where both match.
//   It is compared in full, byte by byte, and the search continues just after it. `vll` (VECTOR
//   LOAD WITH LENGTH) loads only as many bytes as there are candidates left when fewer than
//   sixteen are, and an index past them counts as none found, so nothing past either string is
//   read and whatever the rest of the register holds does not matter.
//   The vector facility is z13's, which the module docs in src/words/mod.rs justify; `.machine
//   z13` names it for this routine only, as module-level assembly does not inherit features the
//   target lacks, and `.machine pop` restores what the rest of the module is assembled for.
//
// Calling convention
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  haystack      pointer to its first byte (need not be aligned)
//     r3  haystack_len  number of bytes
//     r4  needle        pointer to its first byte (need not be aligned)
//     r5  needle_len    number of bytes
//     r2                1 when the needle occurs, else 0 (returned)
//   Clobbers r0, r1, r3, r5, v16 to v21 and the condition code, which the ABI lets a callee
//   change. It uses r6 and r7, which a callee must preserve, by saving them in the register save
//   area the caller's frame provides at 48(r15) and restoring them before it returns, as
//   compiled functions do; it leaves v8 to v15, whose leftmost halves (f8 to f15) a callee must
//   preserve, alone.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first-byte loads only at candidates, and the last-byte loads n - 1 bytes
//   further, which end at haystack_len - 1. It writes nothing but the two save-area slots that
//   belong to it, allocates no stack frame, never changes r15 (the stack pointer) or r14, the
//   return address it branches to, and keeps no state between calls, so it is reentrant and
//   thread safe. z/Architecture is big-endian, which does not matter here: bytes are compared
//   and indexed in memory order.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align function
//   entries on this architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .machine push
    .machine z13
    .globl konstruct_contains
    .p2align 3
konstruct_contains:
    ltgr    %r5, %r5
    je      8f                          // an empty needle occurs everywhere
    clgr    %r5, %r3
    jh      9f                          // a needle longer than the haystack occurs nowhere
    stmg    %r6, %r7, 48(%r15)          // r6 and r7 into the caller's register save area
    sgr     %r3, %r5
    agr     %r3, %r2                    // r3: the last candidate
    aghi    %r5, -1                     // r5: the offset of the needle's last byte
    vlrepb  %v20, 0(%r4)                // v20: the needle's first byte, sixteen times
    vlrepb  %v21, 0(%r5,%r4)            // v21: its last
    vzero   %v19
2:                                      // r2: the next candidate
    lgr     %r0, %r3
    sgr     %r0, %r2                    // candidates left, less one
    jl      7f                          // none
    clgfi   %r0, 15
    jle     3f
    lghi    %r0, 15                     // r0: the index of the last candidate loaded
3:
    la      %r1, 0(%r5,%r2)
    vll     %v16, %r0, 0(%r2)
    vll     %v17, %r0, 0(%r1)
    vceqb   %v16, %v16, %v20
    vceqb   %v17, %v17, %v21
    vn      %v16, %v16, %v17
    vfeneb  %v18, %v16, %v19            // the index of the first candidate where both match
    vlgvb   %r6, %v18, 7
    clgr    %r6, %r0
    jle     4f
    agr     %r2, %r0                    // none among those loaded
    aghi    %r2, 1
    j       2b
4:
    agr     %r2, %r6                    // r2: the candidate
    lghi    %r6, 1                      // r6: the next byte to compare
5:
    clgr    %r6, %r5
    jhe     6f                          // every byte between the first and last matches
    llgc    %r7, 0(%r6,%r2)
    llgc    %r1, 0(%r6,%r4)
    cgr     %r7, %r1
    jne     10f
    aghi    %r6, 1
    j       5b
10:
    aghi    %r2, 1                      // continue after the candidate
    j       2b
6:
    lmg     %r6, %r7, 48(%r15)
8:
    lghi    %r2, 1
    br      %r14
7:
    lmg     %r6, %r7, 48(%r15)
9:
    lghi    %r2, 0
    br      %r14
    .machine pop
