// Word boundaries in a byte string: IBM Z (z/Architecture, s390x), z13 vector facility.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   `vstrc` (VECTOR STRING RANGE COMPARE) gives the index of the first of sixteen bytes that is
//   in one of the ranges `a` to `z` and `0` to `9`, or, with its invert flag, the first that is
//   in neither. The routine alternates the two: from the end of the last word it finds the next
//   word's start, from there its end, and so on, sixteen bytes per instruction. `vll` (VECTOR
//   LOAD WITH LENGTH) loads only the bytes that are left when fewer than sixteen are, and an
//   index past them counts as none found, so nothing past `len` is read and whatever the rest
//   of the register holds does not matter.
//   The vector facility is z13's, which the module docs in src/words/mod.rs justify; `.machine
//   z13` names it for this routine only, as module-level assembly does not inherit features the
//   target lacks, and `.machine pop` restores what the rest of the module is assembled for.
//
// Calling convention
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     r3  len     number of bytes
//     r4  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     r2          the number of offsets written (returned)
//   Clobbers r0, r1, r3 to r5, v16, v17, v20, v21 and the condition code, which the ABI lets a
//   callee change. It uses r6 and r7, which a callee must preserve, by saving them in the
//   register save area the caller's frame provides at 48(r15) and restoring them before it
//   returns, as compiled functions do; it leaves v8 to v15, whose leftmost halves (f8 to f15)
//   a callee must preserve, alone.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each load is of at most the bytes left. It writes
//   only bounds[0] to bounds[n - 1], where n, the count it returns, is at most len + 1, as words
//   and separators alternate, and the two save-area slots that belong to it. The Rust caller
//   passes a slice and a vector with that much spare room. It allocates no stack frame, never
//   changes r15 (the stack pointer) or r14, the return address it branches to, and keeps no
//   state between calls, so it is reentrant and thread safe. z/Architecture is big-endian,
//   which does not matter here: the bytes are compared and indexed in memory order.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align
//   function entries on this architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .machine push
    .machine z13
    .globl konstruct_word_bounds
    .p2align 3
konstruct_word_bounds:
    stmg    %r6, %r7, 48(%r15)          // r6 and r7 into the caller's register save area
    lgr     %r5, %r4                    // the first offset's slot, to count the offsets
    la      %r3, 0(%r3,%r2)             // r3: the end of the bytes
    lgr     %r1, %r2                    // r1: where the search continues
    vzero   %v20                        // v20: the ranges, 'a' to 'z' and '0' to '9'
    vleib   %v20, 0x61, 0
    vleib   %v20, 0x7a, 1
    vleib   %v20, 0x30, 2
    vleib   %v20, 0x39, 3
    vzero   %v21                        // v21: how each bound compares: at least the first,
    vleib   %v21, 0xa0, 0               // at most the second (equal or greater, equal or
    vleib   %v21, 0xc0, 1               // less); the other pairs are never true
    vleib   %v21, 0xa0, 2
    vleib   %v21, 0xc0, 3
2:                                      // the next word's start
    lgr     %r0, %r3
    sgr     %r0, %r1                    // bytes left
    je      9f
    aghi    %r0, -1
    clgfi   %r0, 15
    jle     3f
    lghi    %r0, 15                     // r0: the index of the last byte loaded
3:
    vll     %v16, %r0, 0(%r1)
    vstrcb  %v17, %v16, %v20, %v21, 0   // the index of the first word byte, 16 if none
    vlgvb   %r6, %v17, 7
    clgr    %r6, %r0
    jh      4f                          // none among the bytes loaded
    agr     %r1, %r6
    lgr     %r7, %r1
    sgr     %r7, %r2
    stg     %r7, 0(%r4)                 // a word starts here
    aghi    %r4, 8
5:                                      // its end
    lgr     %r0, %r3
    sgr     %r0, %r1
    je      7f                          // the bytes end inside the word
    aghi    %r0, -1
    clgfi   %r0, 15
    jle     6f
    lghi    %r0, 15
6:
    vll     %v16, %r0, 0(%r1)
    vstrcb  %v17, %v16, %v20, %v21, 8   // the first byte that is not a word byte
    vlgvb   %r6, %v17, 7
    clgr    %r6, %r0
    jh      8f
    agr     %r1, %r6
    lgr     %r7, %r1
    sgr     %r7, %r2
    stg     %r7, 0(%r4)                 // the word ends here
    aghi    %r4, 8
    j       2b
8:                                      // every byte loaded is in the word
    agr     %r1, %r0
    aghi    %r1, 1
    j       5b
4:                                      // no byte loaded is
    agr     %r1, %r0
    aghi    %r1, 1
    j       2b
7:
    sgr     %r3, %r2
    stg     %r3, 0(%r4)                 // the last word ends with the bytes
    aghi    %r4, 8
9:
    sgr     %r4, %r5
    srlg    %r2, %r4, 3                 // offsets written
    lmg     %r6, %r7, 48(%r15)
    br      %r14
    .machine pop
