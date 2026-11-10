// Word boundaries in a byte string: LoongArch64 with LSX.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   Sixteen bytes at a time: b - 'a' < 26 and b - '0' < 10, compared unsigned with `vslti.bu`,
//   give 0xff for each word byte, and `vmskltz.b` gathers their top bits into a 16-bit mask. A
//   word starts or ends wherever a byte differs from the one before it, so mask XOR (mask << 1,
//   with the previous block's last byte shifted in) has a bit for each boundary; boundaries
//   alternate between starts and ends. `ctz.d` finds each in turn.
//   The last 1 to 15 bytes are covered by one more block that ends at `len` and overlaps the one
//   before, with the boundaries already written masked out, so nothing past `len` is read; a
//   text shorter than 16 bytes is classified one byte at a time.
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     $a1  len     number of bytes
//     $a2  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     $a0          the number of offsets written (returned)
//   Clobbers $a2, $t0 to $t6 and $vr0 to $vr5, all argument or temporary registers a callee may
//   change. It leaves $vr24 to $vr31, whose low halves ($f24 to $f31) a callee must preserve,
//   alone.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load starts at an offset at most
//   len - 16, and a text shorter than that is read a byte at a time. It writes only bounds[0] to bounds[n - 1],
//   where n, the count it returns, is at most len + 1, as words and separators alternate. The
//   Rust caller passes a slice and a vector with that much spare room. It never touches $sp or
//   $ra (it is a leaf that returns through $ra untouched) and keeps no state between calls, so
//   it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of every
//   LoongArch instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_word_bounds
    .p2align 2
konstruct_word_bounds:
    move    $t0, $a2                    // the first offset's slot, to count the offsets
    move    $t1, $zero                  // $t1: offset of the block or byte being classified
    move    $t2, $zero                  // $t2: 1 while inside a word, else 0
    ori     $t3, $zero, 16
    bltu    $a1, $t3, 6f                // fewer than 16 bytes: classify them one at a time
    vrepli.b $vr4, 0x61                 // 'a'
    vrepli.b $vr5, 0x30                 // '0'
    lu12i.w $t6, 0xf
    ori     $t6, $t6, 0xfff             // $t6: the bytes of the block not yet classified
2:
    vldx    $vr0, $a0, $t1
    vsub.b  $vr1, $vr0, $vr4
    vslti.bu $vr1, $vr1, 26             // 0xff where b - 'a' < 26, unsigned: a letter
    vsub.b  $vr2, $vr0, $vr5
    vslti.bu $vr2, $vr2, 10             // 0xff where b - '0' < 10: a digit
    vor.v   $vr1, $vr1, $vr2
    vmskltz.b $vr1, $vr1
    vpickve2gr.hu $t3, $vr1, 0          // bit k: byte k is a word byte
    slli.d  $t4, $t3, 1
    or      $t4, $t4, $t2               // the mask a byte later, the previous byte shifted in
    xor     $t4, $t4, $t3
    and     $t4, $t4, $t6               // bit k: a word starts or ends at byte k
    srli.d  $t2, $t3, 15                // whether the block ends inside a word
    beqz    $t4, 4f
3:
    ctz.d   $t5, $t4                    // the next boundary in the block
    add.d   $t5, $t5, $t1
    st.d    $t5, $a2, 0
    addi.d  $a2, $a2, 8
    addi.d  $t5, $t4, -1
    and     $t4, $t4, $t5               // clear it
    bnez    $t4, 3b
4:
    addi.d  $t1, $t1, 16
    addi.d  $t3, $t1, 16
    bgeu    $a1, $t3, 2b                // another whole block
    bgeu    $t1, $a1, 8f                // no bytes left
    sub.d   $t3, $t3, $a1               // bytes of the last block already classified
    addi.d  $t1, $a1, -16               // the last block ends with the bytes
    lu12i.w $t6, 0xf
    ori     $t6, $t6, 0xfff
    sll.d   $t6, $t6, $t3
    bstrpick.d $t6, $t6, 15, 0          // keep only the bytes not yet classified
    b       2b
6:
    beqz    $a1, 8f
7:
    ldx.bu  $t3, $a0, $t1
    addi.d  $t4, $t3, -0x61
    sltui   $t4, $t4, 26                // a letter
    addi.d  $t3, $t3, -0x30
    sltui   $t3, $t3, 10                // a digit
    or      $t3, $t3, $t4
    beq     $t3, $t2, 5f
    st.d    $t1, $a2, 0                 // a word starts or ends here
    addi.d  $a2, $a2, 8
    move    $t2, $t3
5:
    addi.d  $t1, $t1, 1
    bltu    $t1, $a1, 7b
8:
    beqz    $t2, 9f
    st.d    $a1, $a2, 0                 // the last word ends with the bytes
    addi.d  $a2, $a2, 8
9:
    sub.d   $a0, $a2, $t0
    srli.d  $a0, $a0, 3                 // offsets written
    ret
