// JSON string escaping: x86_64, Intel syntax.
//
// What it does
//   Copies the `len` bytes at `src` to `dst` with the escapes a JSON string needs and returns
//   the address just past the last byte written. A quote becomes \" and a backslash \\; the
//   control characters below 0x20 become \b, \t, \n, \f and \r, or \u00xx with lower-case hex
//   for the others; every other byte, DEL and the bytes of non-ASCII characters included, is
//   copied as it is. The output is the one orjson writes, which graph.json has always been.
//   `dst` needs room for 6 * len bytes. The bytes are UTF-8 text, but the routine looks at one
//   byte at a time and changes only ASCII, so it does not need them to be.
//   Konstruct writes every string of graph.json and of its other JSON files with it
//   (src/write/mod.rs).
//
// How
//   Eight bytes at a time, as a 64-bit word. A word with no byte below 0x20 and none equal to
//   0x22 or 0x5c is stored whole. "Some byte of w is below 0x20" is
//   (w - 0x2020...20) & ~w & 0x8080...80, and "some byte of w is equal to c" is the same test
//   (x - 0x0101...01) & ~x & 0x8080...80 on x = w ^ (c in every byte); both are exact for the
//   question whether there is any such byte. If there is one, or fewer than eight bytes are
//   left, one byte is looked at on its own and then the word test is made again from the next.
//
// Calling convention
//   System V AMD64, declared extern "sysv64" in Rust, so one routine serves Windows as well.
//     rdi  src  pointer to the first byte (need not be aligned; unused when len is 0)
//     rsi  len  number of bytes
//     rdx  dst  where the output goes, 6 * len bytes of room
//     rax       the address after the last byte written (returned)
//   Clobbers rax, rcx, rdx, rsi, rdi, r8 to r11 and the flags, all of which the convention
//   lets a callee change. It does not use the stack.
//
// Why it is safe
//   It reads only src[0] to src[len - 1]: a word load is made only while eight or more bytes
//   are left, and the rest is read a byte at a time. It writes only dst[0] to dst[6 * len - 1]:
//   a word store replaces the eight bytes it read, and no byte writes more than six.
//   It keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_escape: Rust links to it by that exact name, as
//   a leading \x01 byte in its link_name tells LLVM not to add the underscore Mach-O symbols
//   get. `.text` names the code section in all object formats; `.p2align 4` starts the
//   function on a 16-byte boundary. Local labels are numbered 2 and up: in Intel syntax a label
//   made only of 0 and 1 reads as a binary number.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 4
konstruct_json_escape:
    movabs  r9, 0x8080808080808080
    movabs  r10, 0x0101010101010101
2:
    cmp     rsi, 8
    jb      5f
    mov     rax, qword ptr [rdi]
    mov     rcx, rax
    mov     r8, r10
    shl     r8, 5                       // 0x2020202020202020
    sub     rcx, r8
    mov     r8, rax
    not     r8
    and     rcx, r8                     // some byte is below 0x20
    imul    r8, r10, 0x22
    xor     r8, rax
    mov     r11, r8
    sub     r11, r10
    not     r8
    and     r11, r8
    or      rcx, r11                    // or equal to a quote
    imul    r8, r10, 0x5c
    xor     r8, rax
    mov     r11, r8
    sub     r11, r10
    not     r8
    and     r11, r8
    or      rcx, r11                    // or equal to a backslash
    test    rcx, r9
    jnz     5f
    mov     qword ptr [rdx], rax        // the eight bytes are clean
    add     rdx, 8
    add     rdi, 8
    sub     rsi, 8
    jmp     2b
5:
    test    rsi, rsi
    jz      9f
    movzx   eax, byte ptr [rdi]
    inc     rdi
    dec     rsi
    cmp     al, 0x22
    je      6f
    cmp     al, 0x5c
    je      7f
    cmp     al, 0x20
    jb      8f
    mov     byte ptr [rdx], al
    inc     rdx
    jmp     2b
6:
    mov     word ptr [rdx], 0x225c      // \"
    add     rdx, 2
    jmp     2b
7:
    mov     word ptr [rdx], 0x5c5c      // \\
    add     rdx, 2
    jmp     2b
8:
    mov     ecx, 0x625c                 // \b
    cmp     al, 8
    je      10f
    mov     ecx, 0x745c                 // \t
    cmp     al, 9
    je      10f
    mov     ecx, 0x6e5c                 // \n
    cmp     al, 10
    je      10f
    mov     ecx, 0x665c                 // \f
    cmp     al, 12
    je      10f
    mov     ecx, 0x725c                 // \r
    cmp     al, 13
    je      10f
    mov     dword ptr [rdx], 0x3030755c // \u00
    mov     ecx, eax
    shr     ecx, 4
    add     cl, 0x30                    // the high digit is 0 or 1
    mov     byte ptr [rdx + 4], cl
    and     eax, 15
    lea     ecx, [rax + 0x30]
    lea     r8d, [rax + 0x57]
    cmp     al, 10
    cmovae  ecx, r8d                    // a to f for 10 to 15
    mov     byte ptr [rdx + 5], cl
    add     rdx, 6
    jmp     2b
10:
    mov     word ptr [rdx], cx
    add     rdx, 2
    jmp     2b
9:
    mov     rax, rdx
    ret
