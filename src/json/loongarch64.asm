// The JSON scan: LoongArch64 with LSX.
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
//   bracket), the whitespace bytes, and 0x20 (`vslt.bu`, a control character below it).
//   `vmskltz.b` gathers each comparison into 16 bits, and `vextrins.h` puts the four of a class
//   side by side, a 64-bit mask with bit k for byte k. Then, in the general registers:
//   - Escaped bytes: with p the backslashes not themselves escaped by the last block, the bytes
//     each odd run of backslashes escapes are ((p << 1 | ODD) - p) ^ ODD ^ (backslashes |
//     carry), where ODD has the odd bits set: the subtraction runs a borrow along each run, so
//     whether a run is odd or even decides the bit after it.
//   - Strings: the prefix XOR of the unescaped quotes (x ^= x << 1, << 2, ... << 32) has a bit
//     set from each opening quote up to its closing quote, and its top bit carries over.
//   - Every other token: the bytes that are not whitespace, structural, quotes or control
//     characters outside strings, wherever the byte before is not one too.
//   `ctz.d` then finds each position to write. The last 1 to 63 bytes of a call are copied into
//   the 64 bytes at the end of `scan`, after spaces, which change nothing that is reported, and
//   scanned there, so nothing past `len` is read.
//
// Calling convention
//   The LoongArch64 ELF psABI (LP64D), declared extern "C" in Rust.
//     $a0  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     $a1  len        number of bytes
//     $a2  base       the position of the first byte in the document
//     $a3  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     $a4  positions  where the positions go: room for len of them, 4-byte aligned
//     $a0             the number of positions written (returned)
//   Clobbers $a1 to $a7, $t0 to $t8, $vr0 to $vr23 and nothing else, all of which the psABI
//   lets a callee change. It uses $s0, which a callee must preserve, by saving it in `scan` and
//   restoring it before it returns, and leaves $vr24 to $vr31, whose low halves ($f24 to $f31)
//   a callee must preserve, alone.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load is of a block that ends at most
//   at len, and the last bytes are copied a byte at a time. It writes only `scan` and
//   positions[0] to positions[n - 1], where n, the count it returns, is at most len, as no byte
//   is reported twice. The Rust caller passes a slice, the state and a vector with that much
//   spare room. It never touches the stack pointer or $ra (it is a leaf that returns through
//   $ra untouched) and keeps its state only in `scan`, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of every
//   instruction. The macros konstruct_json_mask and konstruct_json_classes are named so that
//   they cannot meet another routine's in the crate's assembly, and removed after use.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    // The 64 comparisons in $vr4 to $vr7, one bit each, into the general register \dst.
    .macro konstruct_json_mask dst
    vmskltz.b $vr4, $vr4
    vmskltz.b $vr5, $vr5
    vmskltz.b $vr6, $vr6
    vmskltz.b $vr7, $vr7
    vextrins.h $vr4, $vr5, 0x10
    vextrins.h $vr4, $vr6, 0x20
    vextrins.h $vr4, $vr7, 0x30
    vpickve2gr.d \dst, $vr4, 0
    .endm

    // The structural characters and the whitespace of the 16 bytes in \x, into \s and \w.
    .macro konstruct_json_classes x, s, w
    vor.v   $vr8, \x, $vr18             // b | 0x20
    vseq.b  $vr9, $vr8, $vr19           // an opening brace or bracket
    vseq.b  $vr8, $vr8, $vr20           // a closing brace or bracket
    vseq.b  $vr10, \x, $vr21            // colon
    vseq.b  $vr11, \x, $vr22            // comma
    vor.v   $vr8, $vr8, $vr9
    vor.v   $vr10, $vr10, $vr11
    vor.v   \s, $vr8, $vr10
    vseq.b  $vr8, \x, $vr18             // space
    vseq.b  $vr9, \x, $vr23             // tab
    vseq.b  $vr10, \x, $vr14            // line feed
    vseq.b  $vr11, \x, $vr15            // carriage return
    vor.v   $vr8, $vr8, $vr9
    vor.v   $vr10, $vr10, $vr11
    vor.v   \w, $vr8, $vr10
    .endm

    .text
    .globl konstruct_json_scan
    .p2align 2
konstruct_json_scan:
    st.d    $s0, $a3, 56                // the register a callee preserves, into scan
    sub.d   $a2, $a2, $a0               // $a2: the position of the byte at address a is a + $a2
    add.d   $a1, $a0, $a1               // $a1: the end of the bytes
    move    $a5, $a4                    // the first position's slot, to count the positions
    ld.d    $a6, $a3, 0                 // $a6: all ones inside a string
    ld.d    $a7, $a3, 8                 // $a7: 1 when the next byte is escaped
    ld.d    $t8, $a3, 16                // $t8: 1 when the last byte was in a number or word
    lu12i.w $s0, -349526                // 0xaaaaa, as the signed 20 bits it takes
    ori     $s0, $s0, 0xaaa
    lu32i.d $s0, -349526
    lu52i.d $s0, $s0, -1366             // 0xaaa as 12 signed bits; $s0: the odd bits
    vrepli.b $vr16, 0x22                // quote
    vrepli.b $vr17, 0x5c                // backslash
    vrepli.b $vr18, 0x20                // space, and the bit that folds brackets onto braces
    vrepli.b $vr19, 0x7b                // opening brace
    vrepli.b $vr20, 0x7d                // closing brace
    vrepli.b $vr21, 0x3a                // colon
    vrepli.b $vr22, 0x2c                // comma
    vrepli.b $vr23, 0x09                // tab
    vrepli.b $vr14, 0x0a                // line feed
    vrepli.b $vr15, 0x0d                // carriage return
2:
    sub.d   $t5, $a1, $a0               // bytes left
    ori     $t6, $zero, 64
    bltu    $t5, $t6, 5f
    vld     $vr0, $a0, 0
    vld     $vr1, $a0, 16
    vld     $vr2, $a0, 32
    vld     $vr3, $a0, 48
    vseq.b  $vr4, $vr0, $vr16
    vseq.b  $vr5, $vr1, $vr16
    vseq.b  $vr6, $vr2, $vr16
    vseq.b  $vr7, $vr3, $vr16
    konstruct_json_mask $t0             // $t0: quotes
    vseq.b  $vr4, $vr0, $vr17
    vseq.b  $vr5, $vr1, $vr17
    vseq.b  $vr6, $vr2, $vr17
    vseq.b  $vr7, $vr3, $vr17
    konstruct_json_mask $t1             // $t1: backslashes
    vslt.bu $vr4, $vr0, $vr18
    vslt.bu $vr5, $vr1, $vr18
    vslt.bu $vr6, $vr2, $vr18
    vslt.bu $vr7, $vr3, $vr18
    konstruct_json_mask $t3             // $t3: control characters, below 0x20
    konstruct_json_classes $vr0, $vr4, $vr12
    konstruct_json_classes $vr1, $vr5, $vr13
    konstruct_json_classes $vr2, $vr6, $vr0
    konstruct_json_classes $vr3, $vr7, $vr1
    konstruct_json_mask $t2             // $t2: structural characters
    vori.b  $vr4, $vr12, 0
    vori.b  $vr5, $vr13, 0
    vori.b  $vr6, $vr0, 0
    vori.b  $vr7, $vr1, 0
    konstruct_json_mask $t4             // $t4: whitespace
    // The bytes escaped: each odd run of backslashes escapes the byte after it.
    andn    $t5, $t1, $a7               // backslashes not escaped by the last block
    slli.d  $t6, $t5, 1
    or      $t6, $t6, $s0
    sub.d   $t6, $t6, $t5
    xor     $t6, $t6, $s0               // the escaped bytes and the backslashes that escape
    or      $t5, $t1, $a7
    xor     $t5, $t5, $t6               // the escaped bytes
    and     $t6, $t6, $t1
    srli.d  $a7, $t6, 63                // whether the block's last byte escapes the next
    andn    $t0, $t0, $t5               // $t0: the quotes that are not escaped
    // Inside strings: from each opening quote up to its closing quote.
    slli.d  $t6, $t0, 1
    xor     $t5, $t0, $t6
    slli.d  $t6, $t5, 2
    xor     $t5, $t5, $t6
    slli.d  $t6, $t5, 4
    xor     $t5, $t5, $t6
    slli.d  $t6, $t5, 8
    xor     $t5, $t5, $t6
    slli.d  $t6, $t5, 16
    xor     $t5, $t5, $t6
    slli.d  $t6, $t5, 32
    xor     $t5, $t5, $t6
    xor     $t5, $t5, $a6               // $t5: inside a string
    srai.d  $a6, $t5, 63                // whether the block ends inside one
    // Control characters inside strings, or outside them and not whitespace.
    orn     $t6, $t5, $t4
    and     $t6, $t6, $t3
    bnez    $t6, 7f
6:
    // The bytes of numbers and words: none of the above, outside strings.
    or      $t3, $t3, $t4
    or      $t3, $t3, $t2
    or      $t3, $t3, $t0
    nor     $t3, $t3, $t5
    andn    $t2, $t2, $t5               // $t2: structural characters outside strings
    slli.d  $t6, $t3, 1
    or      $t6, $t6, $t8               // the bytes after a byte of a number or word
    srli.d  $t8, $t3, 63
    andn    $t6, $t3, $t6               // the first byte of each number or word
    or      $t6, $t6, $t2
    or      $t6, $t6, $t0               // every position to write
    beqz    $t6, 4f
    add.d   $t7, $a0, $a2               // the position of the block's first byte
3:
    ctz.d   $t5, $t6
    add.d   $t5, $t5, $t7
    st.w    $t5, $a4, 0
    addi.d  $a4, $a4, 4
    addi.d  $t5, $t6, -1
    and     $t6, $t6, $t5               // clear it
    bnez    $t6, 3b
4:
    addi.d  $a0, $a0, 64
    b       2b
7:
    ld.d    $t7, $a3, 24
    addi.d  $t7, $t7, 1
    bnez    $t7, 6b                     // one is noted already
    ctz.d   $t6, $t6
    add.d   $t6, $t6, $a0
    add.d   $t6, $t6, $a2
    st.d    $t6, $a3, 24                // the first
    b       6b
5:
    beqz    $t5, 8f                     // no bytes left
    addi.d  $t6, $a3, 104               // the last bytes go to the end of scan
    add.d   $a2, $a2, $a0
    sub.d   $a2, $a2, $t6               // positions as they were, from the copy
    lu12i.w $t7, 0x20202
    ori     $t7, $t7, 0x020
    lu32i.d $t7, 0x02020
    lu52i.d $t7, $t7, 0x202
    st.d    $t7, $t6, 0                 // spaces after them
    st.d    $t7, $t6, 8
    st.d    $t7, $t6, 16
    st.d    $t7, $t6, 24
    st.d    $t7, $t6, 32
    st.d    $t7, $t6, 40
    st.d    $t7, $t6, 48
    st.d    $t7, $t6, 56
    move    $t4, $zero
9:
    ldx.bu  $t7, $a0, $t4
    stx.b   $t7, $t6, $t4
    addi.d  $t4, $t4, 1
    bltu    $t4, $t5, 9b
    move    $a0, $t6
    addi.d  $a1, $t6, 64                // one block left: the copy
    b       2b
8:
    st.d    $a6, $a3, 0                 // the state, for the next call
    st.d    $a7, $a3, 8
    st.d    $t8, $a3, 16
    sub.d   $a0, $a4, $a5
    srli.d  $a0, $a0, 2                 // positions written
    ld.d    $s0, $a3, 56
    ret

    .purgem konstruct_json_mask
    .purgem konstruct_json_classes
