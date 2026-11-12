// An integer's decimal digits: AArch64 (ARMv8-A), GNU syntax.
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
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  value
//     x1  buf    20 bytes
//     x0         the number of digits (returned)
//   Clobbers x1 to x7 and the flags. It does not touch x18 and does not use the stack.
//
// Why it is safe
//   It writes only buf[0] to buf[19] and reads no memory. It keeps no state, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_digits: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_digits
    .p2align 2
konstruct_json_digits:
    add     x2, x1, #20                 // the next digit goes before x2
    mov     x3, #0xcccd
    movk    x3, #0xcccc, lsl #16
    movk    x3, #0xcccc, lsl #32
    movk    x3, #0xcccc, lsl #48        // 0xcccccccccccccccd
    mov     x7, #10
2:
    umulh   x4, x0, x3
    lsr     x4, x4, #3                  // value / 10
    msub    x5, x4, x7, x0              // value % 10
    add     w5, w5, #0x30
    strb    w5, [x2, #-1]!
    mov     x0, x4
    cbnz    x0, 2b
    add     x0, x1, #20
    sub     x0, x0, x2
    ret
