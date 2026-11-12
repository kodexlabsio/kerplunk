// JSON string escaping: s390x (z/Architecture, z13 and later), GNU syntax.
//
// What it does
//   Copies the `len` bytes at `src` to `dst` with the escapes a JSON string needs and returns
//   the address just past the last byte written. A quote becomes \" and a backslash \\; the
//   control characters below 0x20 become \b, \t, \n, \f and \r, or \u00xx with lower-case hex
//   for the others; every other byte, DEL and the bytes of non-ASCII characters included, is
//   copied as it is. The output is the one orjson writes, which graph.json has always been.
//   `dst` needs room for 6 * len bytes. The bytes are UTF-8 text, but the routine looks at one
//   byte at a time and changes only ASCII, so it does not need them to be.
//   Konstruct writes every string of graph.json and of its other JSON files with it
//   (src/write/mod.rs).
//
// How
//   Eight bytes at a time, as a 64-bit word. A word with no byte below 0x20 and none equal to
//   0x22 or 0x5c is stored whole. "Some byte of w is below 0x20" is
//   (w - 0x2020...20) & ~w & 0x8080...80, and "some byte of w is equal to c" is the same test
//   (x - 0x0101...01) & ~x & 0x8080...80 on x = w ^ (c in every byte); both are exact for the
//   question whether there is any such byte, and they take no notice of byte order. If there is
//   one, or fewer than eight bytes are left, one byte is looked at on its own and then the word
//   test is made again from the next.
//
// Calling convention
//   The s390x ELF ABI, declared extern "C" in Rust.
//     r2  src  pointer to the first byte (need not be aligned; unused when len is 0)
//     r3  len  number of bytes
//     r4  dst  where the output goes, 6 * len bytes of room
//     r2       the address after the last byte written (returned)
//   Clobbers r0 to r5 and the condition code. It uses r6 to r10, which a callee must keep, and
//   saves them in the register save area the caller's frame gives it at 48(%r15), so it needs
//   no frame of its own.
//
// Why it is safe
//   It reads only src[0] to src[len - 1]: a word load is made only while eight or more bytes
//   are left, and the rest is read a byte at a time. It writes only dst[0] to dst[6 * len - 1]:
//   a word store replaces the eight bytes it read, and no byte writes more than six.
//   It keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_escape: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align function entries on this
//   architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 3
konstruct_json_escape:
    stmg    %r6, %r10, 48(%r15)
    llihf   %r5, 0x80808080
    iilf    %r5, 0x80808080             // 0x8080808080808080
    llihf   %r6, 0x01010101
    iilf    %r6, 0x01010101             // 0x0101010101010101
2:
    clgfi   %r3, 8
    jl      5f
    lg      %r7, 0(%r2)
    sllg    %r9, %r6, 5                 // 0x2020202020202020
    lgr     %r8, %r7
    sgr     %r8, %r9
    lghi    %r10, -1
    xgr     %r10, %r7                   // ~w
    ngr     %r8, %r10                   // some byte is below 0x20
    lgr     %r9, %r6
    mghi    %r9, 0x22
    xgr     %r9, %r7
    lgr     %r10, %r9
    sgr     %r10, %r6
    lghi    %r1, -1
    xgr     %r1, %r9
    ngr     %r10, %r1
    ogr     %r8, %r10                   // or equal to a quote
    lgr     %r9, %r6
    mghi    %r9, 0x5c
    xgr     %r9, %r7
    lgr     %r10, %r9
    sgr     %r10, %r6
    lghi    %r1, -1
    xgr     %r1, %r9
    ngr     %r10, %r1
    ogr     %r8, %r10                   // or equal to a backslash
    ngr     %r8, %r5
    jne     5f
    stg     %r7, 0(%r4)                 // the eight bytes are clean
    la      %r4, 8(%r4)
    la      %r2, 8(%r2)
    aghi    %r3, -8
    j       2b
5:
    ltgr    %r3, %r3
    je      9f
    llgc    %r7, 0(%r2)
    la      %r2, 1(%r2)
    aghi    %r3, -1
    clfi    %r7, 0x22
    je      6f
    clfi    %r7, 0x5c
    je      7f
    clfi    %r7, 0x20
    jl      8f
    stc     %r7, 0(%r4)
    la      %r4, 1(%r4)
    j       2b
6:
    lhi     %r8, 0x5c22                 // \", the backslash at the lower address
    j       10f
7:
    lhi     %r8, 0x5c5c                 // \\
    j       10f
8:
    lhi     %r8, 0x5c62                 // \b
    clfi    %r7, 8
    je      10f
    lhi     %r8, 0x5c74                 // \t
    clfi    %r7, 9
    je      10f
    lhi     %r8, 0x5c6e                 // \n
    clfi    %r7, 10
    je      10f
    lhi     %r8, 0x5c66                 // \f
    clfi    %r7, 12
    je      10f
    lhi     %r8, 0x5c72                 // \r
    clfi    %r7, 13
    je      10f
    iilf    %r8, 0x5c753030             // \u00
    st      %r8, 0(%r4)
    lgr     %r8, %r7
    srl     %r8, 4
    ahi     %r8, 0x30                   // the high digit is 0 or 1
    stc     %r8, 4(%r4)
    lgr     %r8, %r7
    nilf    %r8, 15
    la      %r9, 0x30(%r8)
    la      %r10, 0x57(%r8)             // a to f for 10 to 15
    clfi    %r8, 10
    jl      11f
    lgr     %r9, %r10
11:
    stc     %r9, 5(%r4)
    la      %r4, 6(%r4)
    j       2b
10:
    sth     %r8, 0(%r4)
    la      %r4, 2(%r4)
    j       2b
9:
    lgr     %r2, %r4
    lmg     %r6, %r10, 48(%r15)
    br      %r14
