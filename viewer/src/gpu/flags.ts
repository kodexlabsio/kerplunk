// WebGPU's flag constants, from the specification. TypeScript's DOM library declares the GPU
// interfaces but not these namespaces, and the values are fixed by the standard.

export const BufferUsage = {
    COPY_DST: 0x8,
    UNIFORM: 0x40,
    STORAGE: 0x80,
} as const;

export const ShaderStage = { VERTEX: 0x1, FRAGMENT: 0x2 } as const;

export const TextureUsage = { COPY_DST: 0x2, TEXTURE_BINDING: 0x4 } as const;
