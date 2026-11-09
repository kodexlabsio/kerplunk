// FNV-1a, 64-bit, over a byte slice: x86_64, Intel syntax.
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
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  data   pointer to the first byte (need not be aligned; unused when len is 0)
//     rsi  len    number of bytes
//     rdx  state  the hash so far
//     rax         the hash after the bytes (returned)
//   Clobbers rcx, rdx, rsi, rdi and r8, which System V lets a callee change, and the flags.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time, and reads nothing when
//   len is 0; the Rust caller passes a slice's pointer and length, so every byte read is inside
//   one live allocation. It writes no memory, touches no stack (it is a leaf: no pushes, no
//   frame, so Windows needs no unwind data for it), uses no vector registers and keeps no state
//   between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_fnv1a64 on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 4` starts the function on a 16-byte boundary, as compilers align
//   function entries.
//   Local labels are numbered 2 and 3: in Intel syntax a label made only of 0 and 1 reads as a
//   binary number.
//   On ELF targets src/fnv/mod.rs adds the symbol's `.type` and `.size` after the routine.

    .text
    .globl konstruct_fnv1a64
    .p2align 4
konstruct_fnv1a64:
    mov     rax, rdx                    // the hash so far
    test    rsi, rsi
    jz      3f                          // nothing to hash
    movabs  r8, 0x100000001b3           // the 64-bit FNV prime
2:
    movzx   ecx, byte ptr [rdi]         // next byte, zero-extended into rcx
    xor     rax, rcx                    // FNV-1a mixes the byte in first...
    imul    rax, r8                     // ...then multiplies, keeping the low 64 bits
    inc     rdi
    dec     rsi
    jnz     2b
3:
    ret
