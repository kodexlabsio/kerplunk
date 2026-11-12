// An integer's decimal digits: little-endian PowerPC64 (POWER8 and later), GNU syntax.
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
//   The 64-bit ELFv2 ABI, declared extern "C" in Rust.
//     r3  value
//     r4  buf    20 bytes
//     r3         the number of digits (returned)
//   Clobbers r3 to r9 and cr0, all volatile. It uses no TOC and no stack.
//
// Why it is safe
//   It writes only buf[0] to buf[19] and reads no memory. It keeps no state, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_digits: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 4` aligns the entry to 16 bytes, as compilers align function entries for POWER.
//   As it uses no TOC, its global and local entry points are the same address and it needs no
//   `.localentry`.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_digits
    .p2align 4
konstruct_json_digits:
    addi    5, 4, 20                    // the next digit goes before r5
    lis     6, 0xcccc
    ori     6, 6, 0xcccc
    sldi    6, 6, 32
    oris    6, 6, 0xcccc
    ori     6, 6, 0xcccd                // 0xcccccccccccccccd
    li      9, 10
2:
    mulhdu  7, 3, 6
    srdi    7, 7, 3                     // value / 10
    mulld   8, 7, 9
    subf    8, 8, 3                     // value % 10
    addi    8, 8, 0x30
    addi    5, 5, -1
    stb     8, 0(5)
    mr.     3, 7
    bne     0, 2b
    addi    3, 4, 20
    subf    3, 5, 3
    blr
