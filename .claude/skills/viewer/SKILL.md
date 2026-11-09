---
name: viewer
description: Work on graph.html, the Konstruct graph viewer (Svelte 5 and TypeScript in viewer/, built into konstruct/outputs/viewer.html). Use for any change to how the graph, panel, search or styling looks or behaves.
---

# Working on the viewer

The viewer is `viewer/` (Svelte 5, strict TypeScript, Vite, pnpm, Node from `.nvmrc`). `pnpm build`
writes one self-contained `konstruct/outputs/viewer.html`; Konstruct fills its `__KONSTRUCT_DATA__`
and `__KONSTRUCT_EXPLAIN__` placeholders when it writes `graph.html`.

1. **Run it with real data:**
   ```bash
   uv run konstruct build examples/jaffle_shop
   cd viewer && pnpm install
   mkdir -p dev && cp ../examples/jaffle_shop/konstruct-out/graph.json dev/
   pnpm dev            # http://localhost:5173/viewer.html
   ```
2. **Where things are.** Pure logic with unit tests: `src/lib/model.ts` (graph indexes, lineage),
   `layout.ts`, `focus.ts`, `search.ts`. State: `src/lib/viewer.svelte.ts` (runes). Interface:
   `src/components/` (Sidebar, Canvas, Panel). Styles: `src/app.css`, following
   `../kodexlabs.io/DESIGN.md` (slate and sky, Manrope and JetBrains Mono, nothing below 14px).
3. **Keep the contract.** Element ids and classes (`#nodes [data-id]`, `.row[data-col]`, `#panel`,
   `#search`, `#isolate`, ...) are what `tests/test_browser.py` drives. Insert text only as text;
   never use `{@html}`. The page must make no network requests.
4. **Check and build:**
   ```bash
   pnpm format && pnpm check && pnpm test && pnpm build
   cd .. && uv run pytest tests/test_browser.py tests/test_html.py
   ```
5. **Commit the rebuilt `konstruct/outputs/viewer.html` with the source.** The pre-commit hook
   rebuilds it and CI fails if it differs from a fresh build.
6. If the change is visible, refresh the screenshots in `docs/images/` (dark theme, 1600 by 900 at
   2x) and the viewer page in `docs/guide/viewer.md`.
