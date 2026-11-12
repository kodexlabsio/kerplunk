// An integer's decimal digits: LoongArch64, GNU syntax.
//
// What it does
//   Writes the decimal digits of the unsigned 64-bit `value` to the end of the 20 bytes at
//   `buf`, with no leading zeros (0 is "0"), and returns how many digits there are; they are
//   buf[20 - n] to buf[19]. 20 bytes hold the largest value, 18446744073709551615.
//   Konstruct writes every integer of its JSON files with it (src/write/mod.rs).
//
// How
//   Each step divides by ten as a multiplication: the high half of value * 0xcccccccccccccccd,
//   shifted right by 3, is value / 10 for every 64-bit value; the remainder is value - 10 * q.
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  value
//     $a1  buf    20 bytes
//     $a0         the number of digits (returned)
//   Clobbers $a0 to $a5 and $t0, all caller-saved. It does not use the stack.
//
// Why it is safe
//   It writes only buf[0] to buf[19] and reads no memory. It keeps no state, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_digits: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of every LoongArch instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_digits
    .p2align 2
konstruct_json_digits:
    addi.d  $a2, $a1, 20                // the next digit goes before $a2
    li.d    $a3, 0xcccccccccccccccd
    li.w    $a5, 10
2:
    mulh.du $a4, $a0, $a3
    srli.d  $a4, $a4, 3                 // value / 10
    mul.d   $t0, $a4, $a5
    sub.d   $t0, $a0, $t0               // value % 10
    addi.d  $t0, $t0, 0x30
    addi.d  $a2, $a2, -1
    st.b    $t0, $a2, 0
    move    $a0, $a4
    bnez    $a0, 2b
    addi.d  $a0, $a1, 20
    sub.d   $a0, $a0, $a2
    ret
