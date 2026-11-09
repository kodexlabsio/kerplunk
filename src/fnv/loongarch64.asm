// FNV-1a, 64-bit, over a byte slice: LoongArch64.
//
// What it does
//   Continues an FNV-1a 64 hash over `len` bytes at `data`, starting from `state`, and returns
//   the new state. For each byte: state = (state XOR byte) * 0x100000001b3, wrapping modulo
//   2^64. Starting from the offset basis 0xcbf29ce484222325 gives the published FNV-1a 64 hash
//   of the bytes; passing a previous result continues it, so a hasher can feed a key in parts
//   (a string's bytes, then its 0xff terminator) and get the hash of the whole.
//   The graph index hashes every node id it interns with it (src/fnv/mod.rs, Fnv1a).
//
// Calling convention
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  data   pointer to the first byte (need not be aligned; unused when len is 0)
//     $a1  len    number of bytes
//     $a2  state  the hash so far
//     $a0         the hash after the bytes (returned)
//   Clobbers $a1, $a2, $t0 and $t1, all argument or temporary registers a callee may change.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time with an unsigned byte
//   load, and reads nothing when len is 0; the Rust caller passes a slice's pointer and length,
//   so every byte read is inside one live allocation. It writes no memory and never touches
//   $sp or $ra (it is a leaf that returns through $ra untouched), uses no floating-point or
//   vector registers and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_fnv1a64, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of every
//   LoongArch instruction.
//   On ELF targets src/fnv/mod.rs adds the symbol's `.type` and `.size` after the routine.

    .text
    .globl konstruct_fnv1a64
    .p2align 2
konstruct_fnv1a64:
    ori     $t0, $zero, 0x1b3           // the 64-bit FNV prime, 0x00000100000001b3:
    lu32i.d $t0, 0x100                  // 0x1b3, with 0x100 in bits 32 to 51
    beqz    $a1, 3f                     // nothing to hash
2:
    ld.bu   $t1, $a0, 0                 // next byte, zero-extended
    xor     $a2, $a2, $t1               // FNV-1a mixes the byte in first...
    mul.d   $a2, $a2, $t0               // ...then multiplies, keeping the low 64 bits
    addi.d  $a0, $a0, 1
    addi.d  $a1, $a1, -1
    bnez    $a1, 2b
3:
    move    $a0, $a2                    // return the hash
    ret
