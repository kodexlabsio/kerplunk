#!/usr/bin/env bash
# Work out what the release workflow releases, and leave the commit to tag checked out.
#
# Writes released=true|false and tag=vX.Y.Z to $GITHUB_OUTPUT, and the release notes to
# $RUNNER_TEMP/RELEASE_NOTES.md. In order:
#   1. the latest tag has no GitHub release: finish it (check it out);
#   2. main carries a version with no tag that is newer than the latest tag: it was prepared by a
#      pull request; tag main;
#   3. main has releasable commits since the latest tag: write the next version on a detached
#      commit above main and tag that (not for a run by hand, which may be on an untested commit).
# Any version outside 0.x is refused. KONSTRUCT_RELEASED_TAGS, a space-separated list, stands in
# for asking GitHub which tags have releases (the tests use it).
set -euo pipefail

has_release() {
  if [ -n "${KONSTRUCT_RELEASED_TAGS+set}" ]; then
    case " ${KONSTRUCT_RELEASED_TAGS} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
  fi
  gh release view "$1" > /dev/null 2>&1
}

git config --local user.email "action@github.com"
git config --local user.name "GitHub Action"
latest="$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
if [ -n "$latest" ] && ! has_release "$latest"; then
  echo "${latest} is tagged but has no GitHub release; finishing it"
  tag="$latest"
  git checkout --quiet "$tag"
elif ! git rev-parse -q --verify "refs/tags/v${version}" > /dev/null \
    && [ "$(printf '%s\n' "${latest#v}" "$version" | sort -V | tail -1)" = "$version" ]; then
  tag="v${version}"
  echo "main carries ${tag}, prepared by its pull request"
elif [ "${GITHUB_EVENT_NAME:-}" = workflow_dispatch ]; then
  # A run by hand only finishes or tags what was prepared; it never releases from main's
  # head, which may not have been tested.
  echo "Nothing prepared to release"
  echo "released=false" >> "$GITHUB_OUTPUT"
  exit 0
else
  if ! git rev-parse -q --verify "refs/tags/v${version}" > /dev/null; then
    # Prepared against an older main: a later release overtook it. Release from above main
    # instead, where the script rebuilds the changelog from the tags.
    echo "::warning::main carries v${version}, which is older than ${latest}; releasing the next version instead"
  fi
  next="$(python3 .kodex/scripts/prepare_release.py --base HEAD --no-fetch --dry-run | sed -n 1p)"
  if [ "${next#nothing}" != "$next" ]; then
    echo "Nothing to release since ${latest}"
    # A release whose v0 move failed is put right here.
    if [ "$(git rev-parse -q --verify 'refs/tags/v0^{commit}')" != "$(git rev-parse "${latest}^{commit}")" ]; then
      git tag -f v0 "$latest"
      git push -f origin refs/tags/v0
    fi
    echo "released=false" >> "$GITHUB_OUTPUT"
    exit 0
  fi
  tag="v${next}"
  echo "main has releasable changes since ${latest}; releasing ${tag} from a commit above main"
  git checkout --quiet --detach
  python3 .kodex/scripts/prepare_release.py --base HEAD --no-fetch --above-main
  git commit --quiet -am "chore(release): Release ${next}"
fi
case "$tag" in
  v0.*) ;;
  *) echo "::error::refusing to release ${tag}: Konstruct stays below 1.0 until the owner decides"; exit 1 ;;
esac
# The changelog section for this version, up to the next heading.
awk -v heading="## ${tag} " 'index($0, heading) == 1 {found = 1; print; next}
  found && /^## v/ {exit} found {print}' CHANGELOG.md > "$RUNNER_TEMP/RELEASE_NOTES.md"
if [ ! -s "$RUNNER_TEMP/RELEASE_NOTES.md" ]; then
  echo "::error::CHANGELOG.md has no section for ${tag}"
  exit 1
fi
echo "released=true" >> "$GITHUB_OUTPUT"
echo "tag=${tag}" >> "$GITHUB_OUTPUT"
