#!/usr/bin/env bash
# Rebuild the viewer and fail if the page konstruct ships differs from what is staged, so the
# committed konstruct/outputs/viewer.html always matches viewer/src. Run by pre-commit and CI.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
pnpm build > /dev/null
if ! git diff --quiet -- konstruct/outputs/viewer.html; then
    echo "konstruct/outputs/viewer.html was rebuilt from viewer/src; stage it (git add konstruct/outputs/viewer.html)."
    exit 1
fi
