"""Write graph.html: one self-contained, offline, interactive viewer of the graph.

The viewer is a Svelte and TypeScript app in ``viewer/`` at the root of the repository. Its build
(``pnpm build``) writes ``konstruct/outputs/viewer.html``: one file with the script, the styles and the
Manrope and JetBrains Mono fonts (SIL Open Font License; the licences are in ``licenses/``)
inlined. Konstruct fills its two placeholders with
``Graph.to_dict()`` and each column's formula and each relation's conditions from
:mod:`konstruct.analysis.explain`. The page loads nothing from the network.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from konstruct.core import jsonio
from konstruct.core.graph import Graph

_DATA_PLACEHOLDER = '__KONSTRUCT_DATA__'
_EXPLAIN_PLACEHOLDER = '__KONSTRUCT_EXPLAIN__'

# Inside <script type="application/json"> the HTML parser only looks for "</script"; escaping
# "<", ">" and "&" rules that out entirely, and U+2028/U+2029 are escaped for older JS parsers.
_JSON_ESCAPES = (
    ('<', '\\u003c'),
    ('>', '\\u003e'),
    ('&', '\\u0026'),
    ('\u2028', '\\u2028'),
    ('\u2029', '\\u2029'),
)

VIEWER_FORMULA_LIMIT = 800


@cache
def template() -> str:
    """The built viewer page, with the data placeholders still open."""
    page = resources.files('konstruct.outputs').joinpath('viewer.html').read_text(encoding='utf-8')
    if _DATA_PLACEHOLDER not in page or _EXPLAIN_PLACEHOLDER not in page:
        raise RuntimeError('konstruct/outputs/viewer.html is not a viewer build; run `pnpm build` in viewer/')
    return page


def _embed_json(data: Any) -> str:
    # A pass of str.replace per character runs in C; str.translate with a mapping calls back
    # into Python for every character, over 1 s for a 2,000-model graph. No replacement holds a
    # character another one replaces, so the order does not matter.
    text = jsonio.dumps(data)
    for character, escape in _JSON_ESCAPES:
        text = text.replace(character, escape)
    return text


def explanations(graph: Graph, cache: Any = None) -> dict[str, Any]:
    """Column formulas and relation conditions for the viewer, or empty if they cannot be made.
    ``cache`` is the build cache, so an update recomputes only the formulas that changed."""
    from konstruct.analysis.explain import Explainer

    try:
        # A formula longer than this is not readable at a glance; the panel points to
        # `konstruct explain` for the full composition.
        return Explainer(graph, max_composed=VIEWER_FORMULA_LIMIT).summaries(cache=cache)
    except RecursionError:
        return {}


def write_html(graph: Graph, path: Path, cache: Any = None) -> Path:
    """Write the interactive viewer for ``graph`` to ``path`` and return ``path``."""
    page = (
        template()
        .replace(_EXPLAIN_PLACEHOLDER, _embed_json(explanations(graph, cache)), 1)
        .replace(_DATA_PLACEHOLDER, _embed_json(graph.to_dict()), 1)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, page)
    return path
