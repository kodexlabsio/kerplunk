// The WebGPU side of the map: one device, three pipelines sharing one bind group, and buffers that
// persist across frames. A frame is drawn only when something asks for one, and it is one render
// pass with three instanced draws.

import { HEAD, PAD, ROW } from '../lib/layout';
import { TOKENS } from './colour';
import { BufferUsage, ShaderStage, TextureUsage } from './flags';
import { glyphsAt, NAME_MIN_PX, NAME_SIZE, ROW_MIN_PX, ROW_SIZE, type Glyphs } from './pack';
import shader from './render.wgsl';
import { ATLAS_PX, SPREAD, type Atlas } from './text';

/** Segments of each edge curve, as in render.wgsl. */
const SEGMENTS = 20;
const VIEW_FLOATS = 20 + TOKENS.length * 4;
const RADIUS = 12;

/** The page has no WebGPU, or no adapter: the map says so, and nothing else draws it. */
export class Unsupported extends Error {}

/** A buffer or texture the map needs is larger than the device allows. */
export class TooLarge extends Error {
    readonly what: string;
    readonly needed: number;
    readonly limit: number;

    constructor(what: string, needed: number, limit: number) {
        super(`The map's ${what} needs ${needed} bytes, and this device allows ${limit}.`);
        this.what = what;
        this.needed = needed;
        this.limit = limit;
    }
}

/** What the device reported in an error scope: a validation error, or running out of memory. */
export class DeviceError extends Error {}

/**
 * Runs ``work`` inside validation and out-of-memory error scopes, and throws what either caught:
 * otherwise WebGPU reports such errors only to the console, and the map would stay blank.
 */
export async function checked<T>(device: GPUDevice, work: () => T | Promise<T>): Promise<T> {
    device.pushErrorScope('out-of-memory');
    device.pushErrorScope('validation');
    let result: T;
    try {
        result = await work();
    } catch (error) {
        // The error thrown is the one to report; the scopes are popped all the same.
        await device.popErrorScope();
        await device.popErrorScope();
        throw error;
    }
    const validation = await device.popErrorScope();
    const memory = await device.popErrorScope();
    const error = validation ?? memory;
    if (error) throw new DeviceError(error.message);
    return result;
}

/** Bytes of the buffer that holds ``bytes``: whole words, and never empty. */
function bufferSize(bytes: number): number {
    return Math.max(16, Math.ceil(bytes / 4) * 4);
}

/** The limits the map asks for: the adapter's largest buffers, which the defaults may be below. */
export function limitsOf(adapter: GPUAdapter): Record<string, number> {
    return {
        maxBufferSize: adapter.limits.maxBufferSize,
        maxStorageBufferBindingSize: adapter.limits.maxStorageBufferBindingSize,
    };
}

let devicePromise: Promise<GPUDevice> | null = null;

/** The one device the map uses, requested once per page. */
export function gpuDevice(): Promise<GPUDevice> {
    devicePromise ??= (async () => {
        const gpu = typeof navigator === 'undefined' ? undefined : navigator.gpu;
        if (!gpu) throw new Unsupported('navigator.gpu is not available');
        const adapter = await gpu.requestAdapter();
        if (!adapter) throw new Unsupported('no WebGPU adapter');
        const device = await adapter.requestDevice({ requiredLimits: limitsOf(adapter) });
        void device.lost.then(() => (devicePromise = null));
        return device;
    })();
    devicePromise.catch(() => (devicePromise = null));
    return devicePromise;
}

export interface ViewState {
    x: number;
    y: number;
    k: number;
    dpr: number;
    w: number;
    h: number;
    hover: number;
    hoverRow: number;
    focused: boolean;
    labels: boolean;
}

export interface Content {
    nodes: ArrayBuffer;
    states: Uint32Array;
    edges: Uint32Array;
    glyphs: Glyphs;
    atlas: Atlas;
}

export class MapRenderer {
    readonly device: GPUDevice;
    private readonly format: GPUTextureFormat;
    private readonly pipelines: GPURenderPipeline[];
    private readonly layout: GPUBindGroupLayout;
    private readonly viewBuffer: GPUBuffer;
    private readonly viewData = new ArrayBuffer(VIEW_FLOATS * 4);
    private readonly sampler: GPUSampler;
    private resources: GPUBuffer[] = [];
    private atlasTexture: GPUTexture | null = null;
    private group: GPUBindGroup | null = null;
    private context: GPUCanvasContext | null = null;
    private nodeBuffer: GPUBuffer | null = null;
    private stateBuffer: GPUBuffer | null = null;
    private counts = { nodes: 0, edges: 0, glyphs: 0, names: 0 };
    private ground: GPUColor = { r: 0, g: 0, b: 0, a: 1 };
    /** Bytes of GPU memory the map holds: buffers and the atlas. */
    gpuBytes = 0;

    private constructor(device: GPUDevice, pipelines: GPURenderPipeline[], layout: GPUBindGroupLayout) {
        this.device = device;
        this.format = navigator.gpu.getPreferredCanvasFormat();
        this.pipelines = pipelines;
        this.layout = layout;
        this.viewBuffer = device.createBuffer({
            size: VIEW_FLOATS * 4,
            usage: BufferUsage.UNIFORM | BufferUsage.COPY_DST,
        });
        this.sampler = device.createSampler({ magFilter: 'linear', minFilter: 'linear' });
        const f = new Float32Array(this.viewData);
        f.set([HEAD, ROW, PAD, RADIUS, NAME_SIZE, ROW_SIZE], 6);
        f.set([SPREAD, ATLAS_PX, NAME_MIN_PX, ROW_MIN_PX], 16);
    }

    static create(device: GPUDevice): Promise<MapRenderer> {
        return checked(device, () => MapRenderer.build(device));
    }

    private static async build(device: GPUDevice): Promise<MapRenderer> {
        const format = navigator.gpu.getPreferredCanvasFormat();
        const module = device.createShaderModule({ code: shader, label: 'map' });
        const visibility = ShaderStage.VERTEX | ShaderStage.FRAGMENT;
        const layout = device.createBindGroupLayout({
            entries: [
                { binding: 0, visibility, buffer: { type: 'uniform' } },
                ...[1, 2, 3, 4, 5].map(binding => ({
                    binding,
                    visibility,
                    buffer: { type: 'read-only-storage' as const },
                })),
                { binding: 6, visibility: ShaderStage.FRAGMENT, texture: { sampleType: 'float' } },
                { binding: 7, visibility: ShaderStage.FRAGMENT, sampler: { type: 'filtering' } },
            ],
        });
        const pipelineLayout = device.createPipelineLayout({ bindGroupLayouts: [layout] });
        const blend: GPUBlendState = {
            color: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha' },
            alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha' },
        };
        const pipeline = (name: string, topology: GPUPrimitiveTopology): Promise<GPURenderPipeline> =>
            device.createRenderPipelineAsync({
                label: name,
                layout: pipelineLayout,
                vertex: { module, entryPoint: `${name}Vertex` },
                fragment: { module, entryPoint: `${name}Fragment`, targets: [{ format, blend }] },
                primitive: { topology },
            });
        const pipelines = await Promise.all([
            pipeline('edge', 'triangle-strip'),
            pipeline('card', 'triangle-list'),
            pipeline('glyph', 'triangle-list'),
        ]);
        return new MapRenderer(device, pipelines, layout);
    }

    private buffer(data: ArrayBuffer | Uint32Array, usage: number): GPUBuffer {
        const size = bufferSize(data.byteLength);
        const buffer = this.device.createBuffer({
            size,
            usage: usage | BufferUsage.COPY_DST,
            mappedAtCreation: true,
        });
        new Uint8Array(buffer.getMappedRange()).set(
            data instanceof ArrayBuffer
                ? new Uint8Array(data)
                : new Uint8Array(data.buffer, data.byteOffset, data.byteLength),
        );
        buffer.unmap();
        this.resources.push(buffer);
        this.gpuBytes += size;
        return buffer;
    }

    /** Throws TooLarge unless every buffer and the atlas fit within the device's limits. */
    private fits(content: Content): void {
        const limits = this.device.limits;
        const buffer = Math.min(limits.maxBufferSize, limits.maxStorageBufferBindingSize);
        const sizes: [string, number][] = [
            ['node buffer', content.nodes.byteLength],
            ['state buffer', content.states.byteLength],
            ['edge buffer', content.edges.byteLength],
            ['glyph buffer', content.glyphs.data.byteLength],
            ['glyph table', content.atlas.table.byteLength],
        ];
        for (const [what, bytes] of sizes) {
            const size = bufferSize(bytes);
            if (size > buffer) throw new TooLarge(what, size, buffer);
        }
        // The atlas is at most 2,048 by 8,192 pixels, which every device's textures allow.
    }

    /**
     * Uploads everything the map draws, or throws: TooLarge when something exceeds the device's
     * limits, DeviceError when the device refuses it. Later changes go through setNodes and setStates.
     */
    async upload(content: Content): Promise<void> {
        this.fits(content);
        this.release();
        try {
            await checked(this.device, () => this.write(content));
        } catch (error) {
            this.release();
            throw error;
        }
    }

    private write(content: Content): void {
        const storage = BufferUsage.STORAGE;
        this.nodeBuffer = this.buffer(content.nodes, storage);
        this.stateBuffer = this.buffer(content.states, storage);
        const edges = this.buffer(content.edges, storage);
        const glyphs = this.buffer(content.glyphs.data, storage);
        const table = this.buffer(content.atlas.table.buffer as ArrayBuffer, storage);
        const { width, height, pixels } = content.atlas;
        this.atlasTexture = this.device.createTexture({
            size: [width, height],
            format: 'r8unorm',
            usage: TextureUsage.TEXTURE_BINDING | TextureUsage.COPY_DST,
        });
        // Rows of a texture upload are padded to 256 bytes.
        const stride = Math.ceil(width / 256) * 256;
        const padded = new Uint8Array(stride * height);
        for (let y = 0; y < height; y++) padded.set(pixels.subarray(y * width, (y + 1) * width), y * stride);
        this.device.queue.writeTexture({ texture: this.atlasTexture }, padded, { bytesPerRow: stride }, [
            width,
            height,
        ]);
        this.gpuBytes += width * height + VIEW_FLOATS * 4;
        this.counts = {
            nodes: content.states.length,
            edges: content.edges.length / 4,
            glyphs: content.glyphs.data.length / 4,
            names: content.glyphs.names,
        };
        this.group = this.device.createBindGroup({
            layout: this.layout,
            entries: [
                { binding: 0, resource: { buffer: this.viewBuffer } },
                { binding: 1, resource: { buffer: this.nodeBuffer } },
                { binding: 2, resource: { buffer: this.stateBuffer } },
                { binding: 3, resource: { buffer: edges } },
                { binding: 4, resource: { buffer: glyphs } },
                { binding: 5, resource: { buffer: table } },
                { binding: 6, resource: this.atlasTexture.createView() },
                { binding: 7, resource: this.sampler },
            ],
        });
    }

    /** Card positions, written over the buffer in place. */
    setNodes(nodes: ArrayBuffer): void {
        if (this.nodeBuffer) this.device.queue.writeBuffer(this.nodeBuffer, 0, nodes);
    }

    setStates(states: Uint32Array): void {
        if (this.stateBuffer && states.length) this.device.queue.writeBuffer(this.stateBuffer, 0, states);
    }

    setTheme(colours: Float32Array, ground: [number, number, number, number]): void {
        new Float32Array(this.viewData).set(colours, 20);
        this.ground = { r: ground[0], g: ground[1], b: ground[2], a: 1 };
    }

    attach(canvas: HTMLCanvasElement): void {
        this.context = canvas.getContext('webgpu') as GPUCanvasContext | null;
        if (!this.context) throw new Unsupported('no WebGPU canvas context');
        this.context.configure({ device: this.device, format: this.format, alphaMode: 'opaque' });
    }

    detach(): void {
        this.context?.unconfigure();
        this.context = null;
    }

    /** Draws one frame; resolves with the milliseconds until the GPU finished it. */
    draw(state: ViewState): Promise<number> {
        const started = performance.now();
        if (!this.context || !this.group) return Promise.resolve(0);
        const f = new Float32Array(this.viewData);
        const u = new Uint32Array(this.viewData);
        const s = new Int32Array(this.viewData);
        f.set([state.x, state.y, state.k, state.dpr, state.w, state.h], 0);
        u[12] = state.hover < 0 ? 0xffffffff : state.hover;
        s[13] = state.hoverRow;
        u[14] = state.focused ? 1 : 0;
        u[15] = state.labels ? 1 : 0;
        this.device.queue.writeBuffer(this.viewBuffer, 0, this.viewData);
        const encoder = this.device.createCommandEncoder({ label: 'map frame' });
        const pass = encoder.beginRenderPass({
            colorAttachments: [
                {
                    view: this.context.getCurrentTexture().createView(),
                    clearValue: this.ground,
                    loadOp: 'clear',
                    storeOp: 'store',
                },
            ],
        });
        pass.setBindGroup(0, this.group);
        const [edges, cards, glyphs] = this.pipelines;
        if (this.counts.edges) {
            pass.setPipeline(edges!);
            pass.draw((SEGMENTS + 1) * 2, this.counts.edges);
        }
        if (this.counts.nodes) {
            pass.setPipeline(cards!);
            pass.draw(6, this.counts.nodes);
        }
        // Glyphs too small to read are not drawn at all, rather than drawn and discarded.
        const shown = glyphsAt(
            state.k,
            { total: this.counts.glyphs, names: this.counts.names },
            state.labels,
        );
        if (shown) {
            pass.setPipeline(glyphs!);
            pass.draw(6, shown);
        }
        pass.end();
        this.device.queue.submit([encoder.finish()]);
        return this.device.queue.onSubmittedWorkDone().then(() => performance.now() - started);
    }

    private release(): void {
        for (const b of this.resources) b.destroy();
        this.atlasTexture?.destroy();
        this.resources = [];
        this.atlasTexture = null;
        this.group = null;
        this.nodeBuffer = null;
        this.stateBuffer = null;
        this.counts = { nodes: 0, edges: 0, glyphs: 0, names: 0 };
        this.gpuBytes = 0;
    }

    destroy(): void {
        this.detach();
        this.release();
        this.viewBuffer.destroy();
    }
}
