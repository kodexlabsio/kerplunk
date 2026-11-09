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
2. **Where things are.** Pure logic with unit tests in `tests/`: `src/lib/model.ts` (graph indexes,
   lineage), `selector.ts` (dbt selectors), `route.ts` (hash routes), `search.ts` (fuzzy search),
   `layout.ts`, `focus.ts`, `tree.ts`, `stats.ts`, `transform.ts`, `sql.ts`, `export.ts`, `icu.ts` and
   `locale.ts`. State: `src/lib/viewer.svelte.ts` (runes) and `i18n.svelte.ts`. Interface:
   `src/components/` (TopBar, Nav, Palette, Lineage, Inspector, Help) and `src/components/pages/`.
   Messages: `src/locales/` (en-GB is the source; every UI string goes through `t()`). Styles:
   `src/app.css`, following `../kodexlabs.io/DESIGN.md` (slate and sky, Manrope and JetBrains Mono,
   nothing below 14px), with logical properties so right-to-left languages work.
   Loading the graph (inlined, or fetched when a server marks the page with
   `<meta name="konstruct-serve">`) is all in `src/lib/source.ts`. For dev, `dev/explain.json`
   (the second placeholder's data) is read if present. The build adds a hash-based CSP
   (`vite.config.ts`): no inline handlers, no `{@html}`, no eval, no static `style` attributes.
3. **Keep the contract.** Element ids and classes (`#nodes [data-id]`, `.row[data-col]`, `#page h1`,
   `#select`, `#palette-input`, `#tree`, ...) are what `tests/test_browser.py` drives, with an axe-core
   check for WCAG 2.2 AA. Insert text only as text;
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
