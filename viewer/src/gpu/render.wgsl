// The map in three instanced draws, in the manner of Zed's GPUI: edges as curves tessellated here,
// cards as rounded rectangles shaded by their signed distance, and labels as quads over a
// distance-field atlas. The camera is one uniform; the buffers change only with the layout.

struct View {
    centre: vec2f,
    scale: f32, // CSS pixels per world unit
    dpr: f32,
    viewport: vec2f, // CSS pixels
    head: f32,
    row: f32,
    pad: f32,
    radius: f32,
    nameSize: f32,
    rowSize: f32,
    hover: u32,
    hoverRow: i32,
    focused: u32, // 1 while a card is focused: the rest is dimmed
    labels: u32,
    spread: f32,
    atlasPx: f32,
    nameMinPx: f32, // on-screen sizes below which names and rows are left out
    rowMinPx: f32,
    colours: array<vec4f, 14>,
}

struct Node {
    rect: vec4f,
    kind: u32,
    rows: u32,
    columns: u32,
    spare: u32,
}

const CARD = 0u;
const CARD_HOVER = 1u;
const CARD_BORDER = 2u;
const TEXT = 3u;
const TEXT_3 = 4u;
const ACCENT = 5u;
const EDGE = 6u;
const RUNS = 7u;
const RULE = 8u;
const ACCENT_SOFT = 9u;

// A card's state: its role in the focused lineage in the low bits, and whether a filter hides it.
const UPSTREAM = 1u;
const DOWNSTREAM = 2u;
const FOCUS = 3u;
const HIDDEN = 4u;

const DIM = 0.28;
const SEGMENTS = 20u;
const NOWHERE = vec4f(2.0, 2.0, 2.0, 1.0);

@group(0) @binding(0) var<uniform> view: View;
@group(0) @binding(1) var<storage, read> nodes: array<Node>;
@group(0) @binding(2) var<storage, read> states: array<u32>;
@group(0) @binding(3) var<storage, read> edges: array<vec4u>;
@group(0) @binding(4) var<storage, read> glyphs: array<vec4u>;
@group(0) @binding(5) var<storage, read> table: array<vec4f>;
@group(0) @binding(6) var atlas: texture_2d<f32>;
@group(0) @binding(7) var atlasSampler: sampler;

fn clip(world: vec2f) -> vec4f {
    let s = (world - view.centre) * view.scale / (view.viewport * 0.5);
    return vec4f(s.x, -s.y, 0.0, 1.0);
}

fn corner(v: u32) -> vec2f {
    // Two triangles: (0,0) (1,0) (0,1), (0,1) (1,0) (1,1).
    let x = array<f32, 6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
    let y = array<f32, 6>(0.0, 0.0, 1.0, 1.0, 0.0, 1.0);
    return vec2f(x[v], y[v]);
}

fn role(i: u32) -> u32 {
    return states[i] & 3u;
}

fn hidden(i: u32) -> bool {
    return (states[i] & HIDDEN) != 0u;
}

fn dimmed(i: u32) -> bool {
    return view.focused == 1u && role(i) == 0u;
}

// Straight colour a over premultiplied colour b, giving premultiplied.
fn over(a: vec4f, coverage: f32, b: vec4f) -> vec4f {
    let alpha = a.a * coverage;
    return vec4f(a.rgb * alpha, alpha) + b * (1.0 - alpha);
}

// Cards

struct CardOut {
    @builtin(position) position: vec4f,
    @location(0) local: vec2f, // CSS pixels from the card's centre
    @location(1) @interpolate(flat) node: u32,
}

const RING = 6.0; // CSS pixels around a card for its focus ring

@vertex
fn cardVertex(@builtin(vertex_index) v: u32, @builtin(instance_index) i: u32) -> CardOut {
    var out: CardOut;
    out.node = i;
    if (hidden(i)) {
        out.position = NOWHERE;
        return out;
    }
    let r = nodes[i].rect;
    let margin = RING / view.scale;
    let world = r.xy - margin + corner(v) * (r.zw + 2.0 * margin);
    out.local = (world - (r.xy + r.zw * 0.5)) * view.scale;
    out.position = clip(world);
    return out;
}

fn roundedBox(p: vec2f, half: vec2f, radius: f32) -> f32 {
    let q = abs(p) - half + radius;
    return length(max(q, vec2f(0.0))) + min(max(q.x, q.y), 0.0) - radius;
}

@fragment
fn cardFragment(in: CardOut) -> @location(0) vec4f {
    let n = nodes[in.node];
    let k = view.scale;
    let half = n.rect.zw * 0.5 * k;
    let radius = min(view.radius * k, min(half.x, half.y));
    let d = roundedBox(in.local, half, radius);
    let px = 1.0 / view.dpr;
    let inside = clamp(0.5 - d * view.dpr, 0.0, 1.0);
    let top = in.local + half; // CSS pixels from the card's top left corner
    let focusRole = role(in.node);
    let isHover = in.node == view.hover;

    var fill = select(view.colours[CARD], view.colours[CARD_HOVER], isHover);
    var colour = vec4f(fill.rgb * fill.a, fill.a);
    // The kind bar down the left of the head.
    if (top.x < 5.0 * k && top.y < min(n.rect.w, view.head) * k) {
        colour = over(view.colours[n.kind], 1.0, colour);
    }
    if (n.rows > 0u) {
        let rule = clamp(0.5 * k + px - abs(top.y - view.head * k), 0.0, 1.0);
        colour = over(view.colours[RULE], rule, colour);
        let r = (top.y / k - view.head - view.pad) / view.row;
        // Too far out to read the column names: each row is a bar, like a minimap.
        let x = top.x / k;
        let bar = view.labels == 1u && view.rowSize * k < view.rowMinPx && r >= 0.0 && r < f32(n.rows);
        if (bar && abs(fract(r) - 0.5) < 0.17 && x > 18.0 && x < n.rect.z * 0.62) {
            colour = over(view.colours[TEXT_3], 0.32, colour);
        }
        if (isHover && view.hoverRow >= 0 && i32(floor(r)) == view.hoverRow && r >= 0.0) {
            colour = over(view.colours[ACCENT_SOFT], 1.0, colour);
        }
    }
    let lit = view.focused == 1u && focusRole != 0u;
    let width = select(1.0, 2.0, lit);
    let border = clamp(0.5 - (abs(d + width * 0.5) - width * 0.5) * view.dpr, 0.0, 1.0);
    let borderColour = select(view.colours[CARD_BORDER], view.colours[ACCENT], lit);
    colour = over(borderColour, border, colour) * inside;
    // A ring outside the focused card, and a fainter one around the card under the pointer.
    let ringWidth = select(0.0, select(1.5, 3.0, focusRole == FOCUS), focusRole == FOCUS || isHover);
    let ring = clamp(0.5 - (abs(d - 2.0 - ringWidth * 0.5) - ringWidth * 0.5) * view.dpr, 0.0, 1.0);
    let ringColour = select(view.colours[TEXT_3], view.colours[ACCENT], focusRole == FOCUS);
    colour = over(ringColour, select(0.0, ring, ringWidth > 0.0), colour);
    if (dimmed(in.node)) {
        colour *= DIM;
    }
    return colour;
}

// Edges

struct EdgeOut {
    @builtin(position) position: vec4f,
    @location(0) side: f32, // CSS pixels from the centre line
    @location(1) @interpolate(flat) half: f32,
    @location(2) @interpolate(flat) colour: vec4f,
}

fn bezier(p0: vec2f, p1: vec2f, p2: vec2f, p3: vec2f, t: f32) -> vec2f {
    let u = 1.0 - t;
    return u * u * u * p0 + 3.0 * u * u * t * p1 + 3.0 * u * t * t * p2 + t * t * t * p3;
}

fn tangent(p0: vec2f, p1: vec2f, p2: vec2f, p3: vec2f, t: f32) -> vec2f {
    let u = 1.0 - t;
    return 3.0 * u * u * (p1 - p0) + 6.0 * u * t * (p2 - p1) + 3.0 * t * t * (p3 - p2);
}

fn onPath(a: u32, b: u32) -> bool {
    let up = (a == UPSTREAM || a == FOCUS) && (b == UPSTREAM || b == FOCUS);
    let down = (a == DOWNSTREAM || a == FOCUS) && (b == DOWNSTREAM || b == FOCUS);
    return up || down;
}

@vertex
fn edgeVertex(@builtin(vertex_index) v: u32, @builtin(instance_index) i: u32) -> EdgeOut {
    var out: EdgeOut;
    let e = edges[i];
    if (hidden(e.x) || hidden(e.y)) {
        out.position = NOWHERE;
        return out;
    }
    let a = nodes[e.x].rect;
    let b = nodes[e.y].rect;
    let p0 = vec2f(a.x + a.z, a.y + view.head * 0.5);
    let p3 = vec2f(b.x, b.y + view.head * 0.5);
    var p1: vec2f;
    var p2: vec2f;
    // As on the lineage page: forwards edges bow out horizontally, backwards ones loop beneath.
    if (p3.x <= p0.x) {
        p1 = p0 + vec2f(60.0, 40.0);
        p2 = p3 + vec2f(-60.0, 40.0);
    } else {
        let dx = max(40.0, (p3.x - p0.x) * 0.5);
        p1 = p0 + vec2f(dx, 0.0);
        p2 = p3 - vec2f(dx, 0.0);
    }
    let t = f32(v / 2u) / f32(SEGMENTS);
    let side = select(-1.0, 1.0, (v & 1u) == 1u);
    let point = bezier(p0, p1, p2, p3, t);
    let dir = normalize(tangent(p0, p1, p2, p3, t));
    let lit = view.focused == 1u && onPath(role(e.x), role(e.y));
    out.half = select(0.7, 1.2, lit);
    let reach = out.half + 1.0 / view.dpr;
    out.side = side * reach;
    out.position = clip(point + vec2f(-dir.y, dir.x) * side * reach / view.scale);
    var colour = select(view.colours[EDGE], view.colours[RUNS], e.z == 1u);
    if (lit) {
        colour = view.colours[ACCENT];
    } else if (view.focused == 1u) {
        colour.a *= DIM;
    }
    out.colour = colour;
    return out;
}

@fragment
fn edgeFragment(in: EdgeOut) -> @location(0) vec4f {
    let coverage = clamp((in.half - abs(in.side)) * view.dpr + 0.5, 0.0, 1.0);
    let alpha = in.colour.a * coverage;
    return vec4f(in.colour.rgb * alpha, alpha);
}

// Labels

struct GlyphOut {
    @builtin(position) position: vec4f,
    @location(0) uv: vec2f,
    @location(1) @interpolate(flat) ratio: f32, // device pixels per atlas pixel
    @location(2) @interpolate(flat) colour: vec4f,
}

@vertex
fn glyphVertex(@builtin(vertex_index) v: u32, @builtin(instance_index) i: u32) -> GlyphOut {
    var out: GlyphOut;
    let g = glyphs[i];
    let node = g.w;
    let face = g.z >> 24u;
    let size = select(view.rowSize, view.nameSize, face == 0u);
    let px = size * view.scale;
    // Text too small to read is left out; rows go before names.
    if (view.labels == 0u || hidden(node) || px < select(view.rowMinPx, view.nameMinPx, face == 0u)) {
        out.position = NOWHERE;
        return out;
    }
    let index = g.z & 0xffffffu;
    let uv = table[index * 2u];
    let plane = table[index * 2u + 1u];
    let c = corner(v);
    let pen = nodes[node].rect.xy + vec2f(bitcast<f32>(g.x), bitcast<f32>(g.y));
    out.position = clip(pen + mix(plane.xy, plane.zw, c) * size);
    out.uv = mix(uv.xy, uv.zw, c);
    out.ratio = px * view.dpr / view.atlasPx;
    var colour = select(view.colours[TEXT_3], view.colours[TEXT], face == 0u);
    if (dimmed(node)) {
        colour.a *= DIM;
    }
    out.colour = colour;
    return out;
}

@fragment
fn glyphFragment(in: GlyphOut) -> @location(0) vec4f {
    let field = textureSample(atlas, atlasSampler, in.uv).r;
    let d = (0.5 - field) * 2.0 * view.spread * in.ratio;
    let alpha = in.colour.a * clamp(0.5 - d, 0.0, 1.0);
    return vec4f(in.colour.rgb * alpha, alpha);
}
