// The JSON scan: IBM Z (z/Architecture, s390x), z13 vector facility.
//
// What it does
//   Scans the `len` bytes at `data`, which start at position `base` of a document, and writes
//   as 32-bit integers from `positions`, in order, the position of every structural character
//   (braces, brackets, colon and comma) outside strings, every quote that is not escaped, and
//   the first byte of every other run of bytes outside strings that are not whitespace
//   (src/json/mod.rs defines the scan exactly). Returns how many positions it wrote. The state
//   in `scan` carries over from one call to the next: whether the last byte was inside a
//   string, whether the next one is escaped, whether the last byte continued a number or word,
//   and the position of the first control character JSON does not allow. Konstruct reads
//   graph.json with it (src/json/mod.rs).
//
// How
//   The z13 vector facility has no instruction that gathers a comparison into a bit mask, so
//   instead of classifying every byte the scan jumps over the runs of bytes that change
//   nothing, sixteen at a time, with `vstrc` (VECTOR STRING RANGE COMPARE), which gives the
//   index of the first of sixteen bytes in, or with its invert flag not in, up to eight ranges:
//   - outside a string, over whitespace (space, tab, line feed, carriage return);
//   - inside a number or word, over the bytes that continue it: up to a control character or
//     space, a quote, a backslash or a structural character;
//   - inside a string, up to a quote, a backslash or a control character.
//   The byte it stops at is classified on its own, as RV64GC classifies every byte
//   (src/json/riscv64.asm), and so is the byte after a backslash. `vll` (VECTOR LOAD WITH
//   LENGTH) loads only the bytes that are left when fewer than sixteen are, and an index past
//   them counts as none found, so nothing past `len` is read and whatever the rest of the
//   register holds does not matter.
//   The vector facility is z13's, which the module docs in src/words/mod.rs justify; `.machine
//   z13` names it for this routine only, as module-level assembly does not inherit features the
//   target lacks, and `.machine pop` restores what the rest of the module is assembled for.
//
// Calling convention
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     r3  len        number of bytes
//     r4  base       the position of the first byte in the document
//     r5  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     r6  positions  where the positions go: room for len of them, 4-byte aligned
//     r2             the number of positions written (returned)
//   Clobbers r0 to r5, v16, v17 and v20 to v25 and the condition code, which the ABI lets a
//   callee change. It uses r6 to r13, which a callee must preserve, by saving them in the
//   register save area the caller's frame provides at 48(r15) and restoring them before it
//   returns, as compiled functions do; it leaves v8 to v15, whose leftmost halves (f8 to f15)
//   a callee must preserve, alone.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each load is of at most the bytes left. It writes
//   only `scan`, positions[0] to positions[n - 1], where n, the count it returns, is at most
//   len, as no byte is written twice, and the save-area slots that belong to it. The Rust
//   caller passes a slice, the state and a vector with that much spare room. It allocates no
//   stack frame, never changes r15 (the stack pointer) or r14, the return address it branches
//   to, and keeps its state only in `scan`, so it is reentrant and thread safe. z/Architecture
//   is big-endian, which does not matter here: bytes are compared and indexed in memory order,
//   and each position is stored as the 32-bit integer Rust reads.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align
//   function entries on this architecture. The macro konstruct_json_skip is named so that it
//   cannot meet another routine's in the crate's assembly, and removed after use.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    // Moves r1 over the bytes up to the first that `vstrcb` with the ranges \ranges and the
    // flags \flags finds, then classifies that byte; at the end of the bytes, goes back to 2.
    .macro konstruct_json_skip ranges, flags
20:
    lgr     %r0, %r3
    sgr     %r0, %r1                    // bytes left
    je      2b
    aghi    %r0, -1
    clgfi   %r0, 15
    jle     21f
    lghi    %r0, 15                     // r0: the index of the last byte loaded
21:
    vll     %v16, %r0, 0(%r1)
    vstrcb  %v17, %v16, \ranges, %v21, \flags
    vlgvb   %r11, %v17, 7
    clgr    %r11, %r0
    jh      22f                         // none among the bytes loaded
    agr     %r1, %r11
    j       3f
22:
    agr     %r1, %r0
    aghi    %r1, 1
    j       20b
    .endm

    .text
    .machine push
    .machine z13
    .globl konstruct_json_scan
    .p2align 3
konstruct_json_scan:
    stmg    %r6, %r13, 48(%r15)         // r6 to r13 into the caller's register save area
    la      %r3, 0(%r3,%r2)             // r3: the end of the bytes
    lgr     %r1, %r2                    // r1: the next byte
    sgr     %r4, %r2                    // r4: the position of the byte at address a is a + r4
    lgr     %r10, %r6                   // the first position's slot, to count the positions
    lg      %r7, 0(%r5)
    lcgr    %r7, %r7                    // r7: 1 inside a string
    lg      %r8, 8(%r5)                 // r8: 1 when the next byte is escaped
    lg      %r9, 16(%r5)                // r9: 1 when the last byte was in a number or word
    vrepih  %v21, -24384                // 0xa0c0: each range is at least its first byte and
                                        // at most its second
    vrepih  %v20, 0x2020                // v20: whitespace
    vleib   %v20, 0x09, 2
    vleib   %v20, 0x0a, 3
    vleib   %v20, 0x0d, 4
    vleib   %v20, 0x0d, 5
    vrepih  %v22, 0x2222                // v22: what ends a number or word
    vleib   %v22, 0x00, 0
    vleib   %v22, 0x20, 1
    vleib   %v22, 0x2c, 4
    vleib   %v22, 0x2c, 5
    vleib   %v22, 0x3a, 6
    vleib   %v22, 0x3a, 7
    vleib   %v22, 0x5b, 8               // brackets and backslash
    vleib   %v22, 0x5d, 9
    vleib   %v22, 0x7b, 10
    vleib   %v22, 0x7b, 11
    vleib   %v22, 0x7d, 12
    vleib   %v22, 0x7d, 13
    vrepih  %v24, 0x2222                // v24: what matters in a string
    vleib   %v24, 0x00, 0
    vleib   %v24, 0x1f, 1
    vleib   %v24, 0x5c, 2
    vleib   %v24, 0x5c, 3
2:
    clgr    %r1, %r3
    je      8f                          // no bytes left
    ltgr    %r8, %r8
    jne     3f                          // an escaped byte, on its own
    ltgr    %r7, %r7
    jne     4f
    ltgr    %r9, %r9
    jne     5f
    konstruct_json_skip %v20, 8         // whitespace
4:
    konstruct_json_skip %v24, 0         // the rest of a string
5:
    konstruct_json_skip %v22, 0         // the rest of a number or word
3:
    // One byte, r11, at r1.
    llgc    %r11, 0(%r1)
    lgr     %r12, %r8                   // r12: whether it is escaped
    lghi    %r8, 0
    ltgr    %r12, %r12
    jne     6f                          // escaped: no quote, and it escapes nothing
    cghi    %r11, 0x5c
    jne     7f
    lghi    %r8, 1                      // an unescaped backslash escapes the next byte
7:
    cghi    %r11, 0x22
    je      9f                          // a quote
6:
    ltgr    %r7, %r7
    jne     10f
    lgr     %r13, %r11
    oill    %r13, 0x20
    cghi    %r13, 0x7b
    je      11f                         // an opening brace or bracket
    cghi    %r13, 0x7d
    je      11f                         // a closing one
    cghi    %r11, 0x3a
    je      11f                         // colon
    cghi    %r11, 0x2c
    je      11f                         // comma
    cghi    %r11, 0x20
    je      13f                         // space
    jh      12f                         // anything else above it: a number or word
    cghi    %r11, 0x09
    je      13f                         // tab
    cghi    %r11, 0x0a
    je      13f                         // line feed
    cghi    %r11, 0x0d
    je      13f                         // carriage return
    j       14f                         // any other control character
10:
    cghi    %r11, 0x20
    jnl     13f                         // inside a string only control characters matter
    j       14f
9:
    xilf    %r7, 1                      // a quote opens or closes a string
11:
    lgr     %r13, %r1
    agr     %r13, %r4
    st      %r13, 0(%r6)
    aghi    %r6, 4
    j       13f
12:
    ltgr    %r9, %r9
    jne     15f                         // inside a number or word
    lgr     %r13, %r1
    agr     %r13, %r4
    st      %r13, 0(%r6)                // the first byte of one
    aghi    %r6, 4
15:
    lghi    %r9, 1
    j       16f
14:
    lg      %r13, 24(%r5)
    cghi    %r13, -1
    jne     13f                         // one is noted already
    lgr     %r13, %r1
    agr     %r13, %r4
    stg     %r13, 24(%r5)               // the first
13:
    lghi    %r9, 0
16:
    aghi    %r1, 1
    j       2b
8:
    lcgr    %r7, %r7
    stg     %r7, 0(%r5)                 // the state, for the next call: all ones in a string
    stg     %r8, 8(%r5)
    stg     %r9, 16(%r5)
    sgr     %r6, %r10
    srlg    %r2, %r6, 2                 // positions written
    lmg     %r6, %r13, 48(%r15)
    br      %r14
    .machine pop

    .purgem konstruct_json_skip
