// The JSON scan: x86_64 with SSE2, Intel syntax.
//
// What it does
//   Scans the `len` bytes at `data`, which start at position `base` of a document, and writes
//   as 32-bit integers from `positions`, in order, the position of every structural character
//   (braces, brackets, colon and comma) outside strings, every quote that is not escaped, and the first byte of
//   every other run of bytes outside strings that are not whitespace (src/json/mod.rs defines
//   the scan exactly). Returns how many positions it wrote. The state in `scan` carries over
//   from one call to the next: whether the last byte was inside a string, whether the next one
//   is escaped, whether the last byte continued a number or word, and the position of the first
//   control character JSON does not allow. Every call but a document's last must be given a
//   multiple of 64 bytes. Konstruct reads graph.json with it (src/json/mod.rs).
//
// How
//   Sixty-four bytes at a time, as simdjson does. Each 16 are compared with the quote, the
//   backslash, the structural characters (b | 0x20 is 0x7b or 0x7d for either brace or bracket), the
//   whitespace bytes and min(b, 0x1f) == b (a control character, as SSE2 has no unsigned byte
//   comparison), and `pmovmskb` turns each comparison into 16 bits of a 64-bit mask, one per
//   class. Then, in the general registers:
//   - Escaped bytes: with p the backslashes not themselves escaped by the last block, the bytes
//     each odd run of backslashes escapes are ((p << 1 | ODD) - p) ^ ODD ^ (backslashes |
//     carry), where ODD has the odd bits set: the subtraction runs a borrow along each run, so
//     whether a run is odd or even decides the bit after it.
//   - Strings: the prefix XOR of the unescaped quotes (x ^= x << 1, << 2, ... << 32) has a bit
//     set from each opening quote up to its closing quote, and its top bit carries over.
//   - Every other token: the bytes that are not whitespace, structural, quotes or control
//     characters outside strings, wherever the byte before is not one too.
//   `bsf` then finds each position to write. The last 1 to 63 bytes of a call are copied into
//   the 64 bytes at the end of `scan`, after spaces, which change nothing that is reported, and
//   scanned there, so nothing past `len` is read.
//
// Calling convention
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     rsi  len        number of bytes
//     rdx  base       the position of the first byte in the document
//     rcx  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     r8   positions  where the positions go: room for len of them, 4-byte aligned
//     rax             the number of positions written (returned)
//   Clobbers rcx, rdx, rsi, rdi, r8 to r11, xmm0 to xmm15 and the flags, all of which System V
//   lets a callee change; under System V every vector register is caller-saved. It uses rbx,
//   rbp and r12 to r15, which a callee must preserve, by saving them in `scan` and restoring
//   them before it returns.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load is of a block that ends at most
//   at len, and the last bytes are copied a byte at a time. It writes only `scan` and
//   positions[0] to positions[n - 1], where n, the count it returns, is at most len, as no byte
//   is reported twice. The Rust caller passes a slice, the state and a vector with that much
//   spare room. It touches no stack (it is a leaf: no pushes, no frame, so Windows needs no
//   unwind data for it) and keeps its state only in `scan`, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 4` starts the function and its loops on 16-byte boundaries.
//   Local labels are numbered from 2: in Intel syntax a label made only of 0 and 1 reads as a
//   binary number. The classification of 16 bytes is the macro konstruct_json_sixteen, named so
//   that it cannot meet another routine's in the crate's assembly, and removed after use.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    // Classifies the 16 bytes at rdi + \offset into bits \offset to \offset + 15 of rbx
    // (backslashes), r12 (quotes), r13 (structural), r14 (control) and r15 (whitespace).
    .macro konstruct_json_sixteen offset
    movdqu  xmm0, xmmword ptr [rdi + \offset]
    movdqa  xmm1, xmm0
    pcmpeqb xmm1, xmm8                  // quotes
    pmovmskb eax, xmm1
    shl     rax, \offset
    or      r12, rax
    movdqa  xmm1, xmm0
    pcmpeqb xmm1, xmm9                  // backslashes
    pmovmskb eax, xmm1
    shl     rax, \offset
    or      rbx, rax
    movdqa  xmm1, xmm0
    por     xmm1, xmm10                 // b | 0x20
    movdqa  xmm2, xmm1
    pcmpeqb xmm1, xmm11                 // an opening brace or bracket
    pcmpeqb xmm2, xmm12                 // a closing brace or bracket
    por     xmm1, xmm2
    movdqa  xmm2, xmm0
    pcmpeqb xmm2, xmm13                 // `:`
    movdqa  xmm3, xmm0
    pcmpeqb xmm3, xmm14                 // `,`
    por     xmm1, xmm2
    por     xmm1, xmm3
    pmovmskb eax, xmm1
    shl     rax, \offset
    or      r13, rax
    movdqa  xmm1, xmm0
    pminub  xmm1, xmm15
    pcmpeqb xmm1, xmm0                  // b <= 0x1f, unsigned: a control character
    pmovmskb eax, xmm1
    shl     rax, \offset
    or      r14, rax
    movdqa  xmm1, xmm0
    pcmpeqb xmm1, xmm10                 // space
    movdqa  xmm2, xmm0
    pcmpeqb xmm2, xmm5                  // tab
    movdqa  xmm3, xmm0
    pcmpeqb xmm3, xmm6                  // line feed
    pcmpeqb xmm0, xmm7                  // carriage return
    por     xmm1, xmm2
    por     xmm1, xmm3
    por     xmm1, xmm0
    pmovmskb eax, xmm1
    shl     rax, \offset
    or      r15, rax
    .endm

    .text
    .globl konstruct_json_scan
    .p2align 4
konstruct_json_scan:
    mov     qword ptr [rcx + 56], rbx   // the registers a callee preserves, into scan
    mov     qword ptr [rcx + 64], rbp
    mov     qword ptr [rcx + 72], r12
    mov     qword ptr [rcx + 80], r13
    mov     qword ptr [rcx + 88], r14
    mov     qword ptr [rcx + 96], r15
    mov     qword ptr [rcx + 48], r8    // the first position's slot, to count the positions
    sub     rdx, rdi
    mov     qword ptr [rcx + 32], rdx   // the position of the byte at address a is a + this
    add     rsi, rdi
    mov     qword ptr [rcx + 40], rsi   // the end of the bytes
    mov     r10, qword ptr [rcx]        // r10: all ones inside a string
    mov     r11, qword ptr [rcx + 8]    // r11: 1 when the next byte is escaped
    mov     rbp, qword ptr [rcx + 16]   // rbp: 1 when the last byte was in a number or word
    mov     eax, 0x09090909
    movd    xmm5, eax
    pshufd  xmm5, xmm5, 0               // tab in each byte
    mov     eax, 0x0a0a0a0a
    movd    xmm6, eax
    pshufd  xmm6, xmm6, 0               // line feed
    mov     eax, 0x0d0d0d0d
    movd    xmm7, eax
    pshufd  xmm7, xmm7, 0               // carriage return
    mov     eax, 0x22222222
    movd    xmm8, eax
    pshufd  xmm8, xmm8, 0               // `"`
    mov     eax, 0x5c5c5c5c
    movd    xmm9, eax
    pshufd  xmm9, xmm9, 0               // `\`
    mov     eax, 0x20202020
    movd    xmm10, eax
    pshufd  xmm10, xmm10, 0             // space, and the bit that folds brackets onto braces
    mov     eax, 0x7b7b7b7b
    movd    xmm11, eax
    pshufd  xmm11, xmm11, 0             // opening brace
    mov     eax, 0x7d7d7d7d
    movd    xmm12, eax
    pshufd  xmm12, xmm12, 0             // closing brace
    mov     eax, 0x3a3a3a3a
    movd    xmm13, eax
    pshufd  xmm13, xmm13, 0             // `:`
    mov     eax, 0x2c2c2c2c
    movd    xmm14, eax
    pshufd  xmm14, xmm14, 0             // `,`
    mov     eax, 0x1f1f1f1f
    movd    xmm15, eax
    pshufd  xmm15, xmm15, 0             // the last control character
    .p2align 4
2:
    mov     rax, qword ptr [rcx + 40]
    sub     rax, rdi                    // bytes left
    cmp     rax, 64
    jb      5f
    xor     ebx, ebx
    xor     r12d, r12d
    xor     r13d, r13d
    xor     r14d, r14d
    xor     r15d, r15d
    konstruct_json_sixteen 0
    konstruct_json_sixteen 16
    konstruct_json_sixteen 32
    konstruct_json_sixteen 48
    // The bytes escaped: each odd run of backslashes escapes the byte after it.
    mov     rax, r11
    not     rax
    and     rax, rbx                    // backslashes not escaped by the last block
    lea     rdx, [rax + rax]
    movabs  rsi, 0xaaaaaaaaaaaaaaaa     // the odd bits
    or      rdx, rsi
    sub     rdx, rax
    xor     rdx, rsi                    // the escaped bytes and the backslashes that escape
    mov     rax, rbx
    or      rax, r11
    xor     rax, rdx                    // the escaped bytes
    and     rdx, rbx
    shr     rdx, 63
    mov     r11, rdx                    // whether the block's last byte escapes the next
    not     rax
    and     r12, rax                    // r12: the quotes that are not escaped
    // Inside strings: from each opening quote up to its closing quote.
    mov     rax, r12
    mov     rdx, rax
    shl     rdx, 1
    xor     rax, rdx
    mov     rdx, rax
    shl     rdx, 2
    xor     rax, rdx
    mov     rdx, rax
    shl     rdx, 4
    xor     rax, rdx
    mov     rdx, rax
    shl     rdx, 8
    xor     rax, rdx
    mov     rdx, rax
    shl     rdx, 16
    xor     rax, rdx
    mov     rdx, rax
    shl     rdx, 32
    xor     rax, rdx
    xor     rax, r10                    // rax: inside a string
    mov     r10, rax
    sar     r10, 63                     // whether the block ends inside one
    // Control characters inside strings, or outside them and not whitespace.
    mov     rdx, r15
    not     rdx
    or      rdx, rax
    and     rdx, r14
    jnz     7f
6:
    // The bytes of numbers and words: none of the above, outside strings.
    or      r14, r15
    or      r14, r13
    or      r14, r12
    or      r14, rax
    not     r14
    mov     rdx, rax
    not     rdx
    and     r13, rdx                    // r13: structural characters outside strings
    lea     rdx, [r14 + r14]
    or      rdx, rbp                    // the bytes after a byte of a number or word
    mov     rbp, r14
    shr     rbp, 63
    not     rdx
    and     rdx, r14                    // the first byte of each number or word
    or      rdx, r13
    or      rdx, r12                    // every position to write
    jz      4f
    mov     rsi, qword ptr [rcx + 32]
    add     rsi, rdi                    // the position of the block's first byte
3:
    bsf     rax, rdx
    add     rax, rsi
    mov     dword ptr [r8], eax
    add     r8, 4
    lea     rax, [rdx - 1]
    and     rdx, rax                    // clear it
    jnz     3b
4:
    add     rdi, 64
    jmp     2b
7:
    cmp     qword ptr [rcx + 24], -1
    jne     6b                          // one is noted already
    bsf     rdx, rdx
    add     rdx, rdi
    add     rdx, qword ptr [rcx + 32]
    mov     qword ptr [rcx + 24], rdx   // the first
    jmp     6b
5:
    test    rax, rax
    jz      8f                          // no bytes left
    lea     rsi, [rcx + 104]            // the last bytes go to the end of scan
    mov     rdx, qword ptr [rcx + 32]
    add     rdx, rdi
    sub     rdx, rsi
    mov     qword ptr [rcx + 32], rdx   // positions as they were, from the copy
    movabs  rdx, 0x2020202020202020
    mov     qword ptr [rsi], rdx        // spaces after them
    mov     qword ptr [rsi + 8], rdx
    mov     qword ptr [rsi + 16], rdx
    mov     qword ptr [rsi + 24], rdx
    mov     qword ptr [rsi + 32], rdx
    mov     qword ptr [rsi + 40], rdx
    mov     qword ptr [rsi + 48], rdx
    mov     qword ptr [rsi + 56], rdx
    xor     edx, edx
9:
    movzx   r9d, byte ptr [rdi + rdx]
    mov     byte ptr [rsi + rdx], r9b
    inc     rdx
    cmp     rdx, rax
    jb      9b
    mov     rdi, rsi
    lea     rax, [rsi + 64]
    mov     qword ptr [rcx + 40], rax   // one block left: the copy
    jmp     2b
8:
    mov     qword ptr [rcx], r10        // the state, for the next call
    mov     qword ptr [rcx + 8], r11
    mov     qword ptr [rcx + 16], rbp
    mov     rax, r8
    sub     rax, qword ptr [rcx + 48]
    shr     rax, 2                      // positions written
    mov     rbx, qword ptr [rcx + 56]
    mov     rbp, qword ptr [rcx + 64]
    mov     r12, qword ptr [rcx + 72]
    mov     r13, qword ptr [rcx + 80]
    mov     r14, qword ptr [rcx + 88]
    mov     r15, qword ptr [rcx + 96]
    ret

    .purgem konstruct_json_sixteen
