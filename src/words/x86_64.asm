// Word boundaries in a byte string: x86_64 with SSE2, Intel syntax.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   Sixteen bytes at a time: b - 'a' and b - '0', compared unsigned with 25 and 9 (as
//   min(x, 25) == x, since SSE2 has no unsigned byte comparison), give a mask with bit k set when
//   byte k is a word byte. A word starts or ends wherever a byte differs from the one before it,
//   so mask XOR (mask << 1, with the last byte of the previous block shifted in) has a bit for
//   each boundary, and boundaries alternate between starts and ends. `bsf` finds each in turn.
//   The last 1 to 15 bytes are covered by one more block that ends at `len` and overlaps the one
//   before, with the boundaries already written masked out, so nothing past `len` is read; a
//   text shorter than 16 bytes is classified one byte at a time.
//
// Calling convention
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     rsi  len     number of bytes
//     rdx  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     rax          the number of offsets written (returned)
//   Clobbers rcx, rdx, r8 to r11, xmm0 to xmm7 and the flags, all of which System V lets a
//   callee change; under System V every vector register is caller-saved, so none is preserved.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load starts at an offset at most
//   len - 16, and a text shorter than that is read a byte at a time. It writes only bounds[0] to bounds[n - 1],
//   where n, the count it returns, is at most len + 1, as words and separators alternate. The
//   Rust caller passes a slice and a vector with that much spare room. It touches no stack (it is
//   a leaf: no pushes, no frame, so Windows needs no unwind data for it) and keeps no state
//   between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds on ELF, Mach-O and COFF alike: Rust links
//   to it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 4` starts the function and its loops on 16-byte boundaries.
//   Local labels are numbered from 2: in Intel syntax a label made only of 0 and 1 reads as a
//   binary number.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_word_bounds
    .p2align 4
konstruct_word_bounds:
    mov     r8, rdx                     // the first offset's slot, to count the offsets
    xor     r9d, r9d                    // r9: offset of the block or byte being classified
    xor     r10d, r10d                  // r10: 1 while inside a word, else 0
    cmp     rsi, 16
    jb      6f                          // fewer than 16 bytes: classify them one at a time
    mov     eax, 0x9f9f9f9f             // -'a' in each byte
    movd    xmm4, eax
    pshufd  xmm4, xmm4, 0
    mov     eax, 0x19191919             // 25 = 'z' - 'a'
    movd    xmm5, eax
    pshufd  xmm5, xmm5, 0
    mov     eax, 0xd0d0d0d0             // -'0'
    movd    xmm6, eax
    pshufd  xmm6, xmm6, 0
    mov     eax, 0x09090909             // 9 = '9' - '0'
    movd    xmm7, eax
    pshufd  xmm7, xmm7, 0
    mov     r11d, 0xffff                // r11: the bytes of the block not yet classified
    .p2align 4
2:
    movdqu  xmm0, xmmword ptr [rdi + r9]
    movdqa  xmm1, xmm0
    paddb   xmm0, xmm4                  // b - 'a'
    paddb   xmm1, xmm6                  // b - '0'
    movdqa  xmm2, xmm0
    pminub  xmm2, xmm5
    pcmpeqb xmm2, xmm0                  // 0xff where b - 'a' <= 25, unsigned: a letter
    movdqa  xmm3, xmm1
    pminub  xmm3, xmm7
    pcmpeqb xmm3, xmm1                  // 0xff where b - '0' <= 9: a digit
    por     xmm2, xmm3
    pmovmskb eax, xmm2                  // bit k: byte k is a word byte
    lea     ecx, [rax + rax]
    or      ecx, r10d                   // the mask a byte later, the previous byte shifted in
    xor     ecx, eax
    shr     eax, 15
    mov     r10d, eax                   // whether the block ends inside a word
    and     ecx, r11d                   // bit k: a word starts or ends at byte k
    jz      4f
3:
    bsf     eax, ecx                    // the next boundary in the block
    add     rax, r9
    mov     qword ptr [rdx], rax
    add     rdx, 8
    lea     eax, [rcx - 1]
    and     ecx, eax                    // clear it
    jnz     3b
4:
    add     r9, 16
    lea     rax, [r9 + 16]
    cmp     rax, rsi
    jbe     2b                          // another whole block
    cmp     r9, rsi
    jae     8f                          // no bytes left
    lea     rcx, [r9 + 16]
    sub     rcx, rsi                    // bytes of the last block already classified
    lea     r9, [rsi - 16]              // the last block ends with the bytes
    mov     r11d, 0xffff
    shl     r11d, cl
    movzx   r11d, r11w                  // keep only the bytes not yet classified
    jmp     2b
6:
    test    rsi, rsi
    jz      8f
7:
    movzx   eax, byte ptr [rdi + r9]
    lea     ecx, [rax - 0x61]
    cmp     ecx, 26
    setb    cl                          // a letter
    sub     eax, 0x30
    cmp     eax, 10
    setb    al                          // a digit
    or      al, cl
    movzx   eax, al
    cmp     eax, r10d
    je      5f
    mov     qword ptr [rdx], r9         // a word starts or ends here
    add     rdx, 8
    mov     r10d, eax
5:
    inc     r9
    cmp     r9, rsi
    jb      7b
8:
    test    r10d, r10d
    jz      9f
    mov     qword ptr [rdx], rsi        // the last word ends with the bytes
    add     rdx, 8
9:
    mov     rax, rdx
    sub     rax, r8
    shr     rax, 3                      // offsets written
    ret
