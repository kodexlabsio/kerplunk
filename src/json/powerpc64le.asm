// The JSON scan: 64-bit little-endian Power, POWER8 vector instructions.
//
// What it does
//   Scans the `len` bytes at `data`, which start at position `base` of a document, and writes
//   as 32-bit integers from `positions`, in order, the position of every structural character
//   (braces, brackets, colon and comma) outside strings, every quote that is not escaped, and
//   the first byte of every other run of bytes outside strings that are not whitespace
//   (src/json/mod.rs defines the scan exactly). Returns how many positions it wrote. The state
//   in `scan` carries over from one call to the next: whether the last byte was inside a
//   string, whether the next one is escaped, whether the last byte continued a number or word,
//   and the position of the first control character JSON does not allow. Every call but a
//   document's last must be given a multiple of 64 bytes. Konstruct reads graph.json with it
//   (src/json/mod.rs).
//
// How
//   Sixty-four bytes at a time, as simdjson does. Each 16 are compared with the quote, the
//   backslash, the structural characters (b | 0x20 is 0x7b or 0x7d for either brace or
//   bracket), the whitespace bytes, and 0x20 (`vcmpgtub`, unsigned: a control character below
//   it), and `vbpermq` (POWER8) gathers each comparison into 16 bits. `lxvd2x` loads each
//   doubleword in little-endian order, so the bytes sit in the register as two reversed halves;
//   the bit indices `vbpermq` takes are arranged to match, so bit k of a mask is byte k in
//   memory. `rldimi` puts the four masks of a class side by side, a 64-bit mask with bit k for
//   byte k. Then, in the general registers:
//   - Escaped bytes: with p the backslashes not themselves escaped by the last block, the bytes
//     each odd run of backslashes escapes are ((p << 1 | ODD) - p) ^ ODD ^ (backslashes |
//     carry), where ODD has the odd bits set: the subtraction runs a borrow along each run, so
//     whether a run is odd or even decides the bit after it.
//   - Strings: the prefix XOR of the unescaped quotes (x ^= x << 1, << 2, ... << 32) has a bit
//     set from each opening quote up to its closing quote, and its top bit carries over.
//   - Every other token: the bytes that are not whitespace, structural, quotes or control
//     characters outside strings, wherever the byte before is not one too.
//   POWER8 has no count of trailing zeros (`cnttzd` is POWER9), so each position is isolated
//   with x AND -x and located with `cntlzd`. The last 1 to 63 bytes of a call are copied into
//   the 64 bytes at the end of `scan`, after spaces, which change nothing that is reported, and
//   scanned there, so nothing past `len` is read.
//
// Calling convention
//   The 64-bit ELF ABI version 2 (ELFv2), which every little-endian Power Linux uses, declared
//   extern "C" in Rust.
//     r3  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     r4  len        number of bytes
//     r5  base       the position of the first byte in the document
//     r6  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     r7  positions  where the positions go: room for len of them, 4-byte aligned
//     r3             the number of positions written (returned)
//   Clobbers r0, r4 to r12, v0 to v19 (VSX registers 32 to 51) and cr0, all volatile under
//   ELFv2. It uses r14 to r22, which a callee must preserve, by saving them in `scan` and
//   restoring them before it returns; it leaves v20 to v31, which a callee must preserve,
//   alone, and does not use VRSAVE, which ELFv2 does not require a callee to maintain.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load is of a block that ends at most
//   at len, and the last bytes are copied a byte at a time. It writes only `scan` and
//   positions[0] to positions[n - 1], where n, the count it returns, is at most len, as no byte
//   is reported twice. The Rust caller passes a slice, the state and a vector with that much
//   spare room. It has no stack frame and never touches r1, r2 (the TOC pointer, which it does
//   not need) or the link register, through which it returns, and keeps its state only in
//   `scan`, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). As it uses
//   no TOC, its global and local entry points are the same address and it needs no
//   `.localentry`. `.text` is the ELF code section; `.p2align 4` aligns the entry to 16 bytes,
//   as compilers align function entries for POWER's instruction fetch. The macro
//   konstruct_json_sixteen is named so that it cannot meet another routine's in the crate's
//   assembly, and removed after use.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    // Classifies the 16 bytes at r3 + \offset into bits \offset to \offset + 15 of r18
    // (quotes), r19 (backslashes), r20 (structural), r21 (control) and r22 (whitespace);
    // \mb is the first bit (counting from the top, as rldimi does) of those bits.
    .macro konstruct_json_sixteen offset, mb
    li      9, \offset
    lxvd2x  32, 3, 9                    // v0: the bytes
    vcmpequb 1, 0, 10                   // quotes
    vcmpequb 2, 0, 11                   // backslashes
    vor     7, 0, 12                    // b | 0x20
    vcmpequb 8, 7, 13                   // an opening brace or bracket
    vcmpequb 7, 7, 14                   // a closing brace or bracket
    vcmpequb 9, 0, 15                   // colon
    vor     7, 7, 8
    vcmpequb 8, 0, 16                   // comma
    vor     7, 7, 9
    vor     3, 7, 8                     // structural
    vcmpgtub 4, 12, 0                   // 0x20 > b, unsigned: a control character
    vcmpequb 5, 0, 12                   // space
    vcmpequb 7, 0, 17                   // tab
    vcmpequb 8, 0, 18                   // line feed
    vcmpequb 9, 0, 19                   // carriage return
    vor     5, 5, 7
    vor     8, 8, 9
    vor     5, 5, 8                     // whitespace
    vbpermq 1, 1, 6
    vbpermq 2, 2, 6
    vbpermq 3, 3, 6
    vbpermq 4, 4, 6
    vbpermq 5, 5, 6
    mfvsrd  9, 33
    mfvsrd  10, 34
    mfvsrd  11, 35
    mfvsrd  12, 36
    mfvsrd  0, 37
    rldimi  18, 9, \offset, \mb
    rldimi  19, 10, \offset, \mb
    rldimi  20, 11, \offset, \mb
    rldimi  21, 12, \offset, \mb
    rldimi  22, 0, \offset, \mb
    .endm

    .text
    .globl konstruct_json_scan
    .p2align 4
konstruct_json_scan:
    std     14, 32(6)                   // the registers a callee preserves, into scan
    std     15, 40(6)
    std     16, 48(6)
    std     17, 56(6)
    std     18, 64(6)
    std     19, 72(6)
    std     20, 80(6)
    std     21, 88(6)
    std     22, 96(6)
    mr      8, 7                        // the first position's slot, to count the positions
    subf    5, 3, 5                     // r5: the position of the byte at address a is a + r5
    add     4, 3, 4                     // r4: the end of the bytes
    ld      14, 0(6)                    // r14: all ones inside a string
    ld      15, 8(6)                    // r15: 1 when the next byte is escaped
    ld      16, 16(6)                   // r16: 1 when the last byte was in a number or word
    lis     17, -21846
    ori     17, 17, 0xaaaa
    rldimi  17, 17, 32, 0               // r17: the odd bits
    li      9, 0
    lvsl    6, 0, 9                     // v6: bytes 0 to 15
    vspltisb 7, 3
    vslb    6, 6, 7                     // the index of the first bit of each byte, 0 to 120,
    xxswapd 38, 38                      // with the halves swapped as lxvd2x leaves the data
    li      9, 0x22
    mtvsrwz 42, 9
    vspltb  10, 10, 7                   // v10: quote in each byte
    li      9, 0x5c
    mtvsrwz 43, 9
    vspltb  11, 11, 7                   // v11: backslash
    li      9, 0x20
    mtvsrwz 44, 9
    vspltb  12, 12, 7                   // v12: space, and the bit that folds brackets onto braces
    li      9, 0x7b
    mtvsrwz 45, 9
    vspltb  13, 13, 7                   // v13: opening brace
    li      9, 0x7d
    mtvsrwz 46, 9
    vspltb  14, 14, 7                   // v14: closing brace
    li      9, 0x3a
    mtvsrwz 47, 9
    vspltb  15, 15, 7                   // v15: colon
    li      9, 0x2c
    mtvsrwz 48, 9
    vspltb  16, 16, 7                   // v16: comma
    vspltisb 17, 9                      // v17: tab
    vspltisb 18, 10                     // v18: line feed
    vspltisb 19, 13                     // v19: carriage return
2:
    subf    9, 3, 4                     // bytes left
    cmpldi  9, 64
    blt     0, 5f
    li      18, 0
    li      19, 0
    li      20, 0
    li      21, 0
    li      22, 0
    konstruct_json_sixteen 0, 48
    konstruct_json_sixteen 16, 32
    konstruct_json_sixteen 32, 16
    konstruct_json_sixteen 48, 0
    // The bytes escaped: each odd run of backslashes escapes the byte after it.
    andc    9, 19, 15                   // backslashes not escaped by the last block
    sldi    10, 9, 1
    or      10, 10, 17
    subf    10, 9, 10
    xor     10, 10, 17                  // the escaped bytes and the backslashes that escape
    or      9, 19, 15
    xor     9, 9, 10                    // the escaped bytes
    and     10, 10, 19
    srdi    15, 10, 63                  // whether the block's last byte escapes the next
    andc    18, 18, 9                   // r18: the quotes that are not escaped
    // Inside strings: from each opening quote up to its closing quote.
    sldi    10, 18, 1
    xor     9, 18, 10
    sldi    10, 9, 2
    xor     9, 9, 10
    sldi    10, 9, 4
    xor     9, 9, 10
    sldi    10, 9, 8
    xor     9, 9, 10
    sldi    10, 9, 16
    xor     9, 9, 10
    sldi    10, 9, 32
    xor     9, 9, 10
    xor     9, 9, 14                    // r9: inside a string
    sradi   14, 9, 63                   // whether the block ends inside one
    // Control characters inside strings, or outside them and not whitespace.
    orc     10, 9, 22
    and.    10, 10, 21
    bne     0, 7f
6:
    // The bytes of numbers and words: none of the above, outside strings.
    or      21, 21, 22
    or      21, 21, 20
    or      21, 21, 18
    nor     21, 21, 9
    andc    20, 20, 9                   // r20: structural characters outside strings
    sldi    10, 21, 1
    or      10, 10, 16                  // the bytes after a byte of a number or word
    srdi    16, 21, 63
    andc    10, 21, 10                  // the first byte of each number or word
    or      10, 10, 20
    or.     10, 10, 18                  // every position to write
    beq     0, 4f
    add     11, 3, 5                    // the position of the block's first byte
3:
    neg     12, 10
    and     12, 12, 10                  // the lowest position alone
    cntlzd  12, 12
    subfic  12, 12, 63                  // its place in the block
    add     12, 12, 11
    stw     12, 0(7)
    addi    7, 7, 4
    addi    12, 10, -1
    and.    10, 10, 12                  // clear it
    bne     0, 3b
4:
    addi    3, 3, 64
    b       2b
7:
    ld      11, 24(6)
    cmpdi   11, -1
    bne     0, 6b                       // one is noted already
    neg     12, 10
    and     12, 12, 10
    cntlzd  12, 12
    subfic  12, 12, 63
    add     12, 12, 3
    add     12, 12, 5
    std     12, 24(6)                   // the first
    b       6b
5:
    cmpldi  9, 0
    beq     0, 8f                       // no bytes left
    addi    10, 6, 104                  // the last bytes go to the end of scan
    add     5, 5, 3
    subf    5, 10, 5                    // positions as they were, from the copy
    lis     11, 0x2020
    ori     11, 11, 0x2020
    rldimi  11, 11, 32, 0
    std     11, 0(10)                   // spaces after them
    std     11, 8(10)
    std     11, 16(10)
    std     11, 24(10)
    std     11, 32(10)
    std     11, 40(10)
    std     11, 48(10)
    std     11, 56(10)
    li      12, 0
9:
    lbzx    0, 3, 12
    stbx    0, 10, 12
    addi    12, 12, 1
    cmpld   12, 9
    blt     0, 9b
    mr      3, 10
    addi    4, 10, 64                   // one block left: the copy
    b       2b
8:
    std     14, 0(6)                    // the state, for the next call
    std     15, 8(6)
    std     16, 16(6)
    subf    3, 8, 7
    srdi    3, 3, 2                     // positions written
    ld      14, 32(6)
    ld      15, 40(6)
    ld      16, 48(6)
    ld      17, 56(6)
    ld      18, 64(6)
    ld      19, 72(6)
    ld      20, 80(6)
    ld      21, 88(6)
    ld      22, 96(6)
    blr

    .purgem konstruct_json_sixteen
