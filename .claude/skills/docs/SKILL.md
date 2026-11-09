---
name: docs
description: Update the Konstruct documentation (Sphinx with MyST Markdown in docs/, published to GitHub Pages) and the README. Use when a command, option, input, output, limitation or number changes, or when writing a new guide page.
---

# Writing the documentation

- **Where things go.** `README.md` is the overview: header, feature grid, quick start, evidence and
  links. Detail lives in `docs/`: `docs/guide/` (how to use each capability), `docs/reference/`
  (CLI, Python API, MCP, configuration, outputs, limitations), and the project pages. Images are in
  `docs/images/`.
- **Generated parts.** The command reference is rendered from `konstruct.interfaces.cli.parser()` by
  sphinx-argparse, and the Python API by autodoc from docstrings. The MCP table in
  `docs/reference/mcp.md` is written by hand; `tests/test_docs.py` fails if it misses a tool, or if
  `docs/reference/cli.md` misses a command.
- **A new page** goes in a `toctree` in `docs/index.md`; Sphinx warns about orphans.
- **Build exactly as CI does** (warnings are errors):
  ```bash
  uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
  open docs/_build/html/index.html
  ```
- **No peripheral information.** Only what Konstruct does and how to use it: no notes on how a
  feature was tested or what it was not tested against, no dates facts were checked, no remarks
  about the work itself.
- **Style.** British English, neutral and concise, no emojis, no em or en dashes. Konstruct in
  prose, `konstruct` for anything typed. Every number states its source or the test behind it.
- Pushing to main publishes the site through `.github/workflows/docs.yml`.
