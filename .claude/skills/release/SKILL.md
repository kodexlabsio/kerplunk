---
name: release
description: Understand or change how Konstruct is versioned and released (prepare_release.py, the release workflow, GitHub releases, the v0 action tag, the gated PyPI job). Use when asked about versions, a release did not happen or was the wrong size, or publishing.
---

# Releases

- main is protected: nothing pushes to it, and a release is a tag plus a GitHub release.
- Changes accumulate on a branch as separate commits and merge in batches, about once an hour, by
  rebase, so each commit stays on main and in the changelog. Give the batch's pull request a plain
  title (not `feat(...): ...`): CI's Release preparation check counts a Conventional title as a
  commit, as it would be after a squash merge. The batch's last commit prepares the
  release: rebase on main, then run `uv run python .kodex/scripts/prepare_release.py`. It fetches
  main and the tags, reads every releasable commit on the branch since the last tag, and sets the
  next version in `pyproject.toml`, `uv.lock` (konstruct's own line only) and
  `konstruct/__init__.py`, with its section at the top of `CHANGELOG.md`. Running it again replaces
  that section. `--dry-run` prints the version and section. For a squash-merged pull request,
  `--title` counts its title when it is not one of the commits.
- The Release preparation job in Build and Test runs `--check` on every pull request: it passes when
  the pull request prepares no release, or prepares exactly what main now calls for, and fails when
  main has moved on (run the script again).
- While the version is 0.x a release is a patch, unless a change has `!` after its type or a
  `BREAKING CHANGE:` footer, which gives the next minor. The changelog follows commitizen's layout
  (BREAKING CHANGE from the footers' text, then Feat, Fix, Refactor and Perf). The script refuses to
  prepare 1.0 and the workflow refuses to release it: the owner decides that.
- `.github/workflows/release.yml` runs after Build and Test passes on main. It finishes a release
  left half done (tag pushed, no GitHub release); otherwise it tags a tested commit whose version
  has no tag; otherwise, when main has releasable commits since the last tag (a Dependabot fix, or a
  pull request merged without preparing), it runs the script on a detached commit above main and
  tags that. Tags are pushed only after the build succeeds. Each release moves `v0` and publishes
  the changelog section, wheel and sdist. A release made from a detached commit reaches main's
  `CHANGELOG.md` with the next prepared pull request, which copies its section from the tag.
- A run by hand (workflow_dispatch) only finishes a half-done release or tags a prepared version; it
  never releases from main's head. A run with nothing to release re-points `v0` at the latest
  release if it lags.
- **PyPI** is a separate job, off until the `PUBLISH_TO_PYPI` repository variable is `true` and a
  `pypi` environment with trusted publishing exists. Never turn it on without Dom's go-ahead.
- Never delete or move a published release tag other than `v0`.
