"""Work out the next release's version and changelog section, and write them.

main is protected, so nothing pushes a version bump to it. A release's version arrives one of two
ways, and both use this script:

- A branch that releases runs it as its last commit, after rebasing on main. It reads the releasable
  commits on the branch since the last tag (main's and its own), and sets the next version in
  pyproject.toml, uv.lock and konstruct/__init__.py, with its section at the top of CHANGELOG.md.
  Once the pull request is merged (rebased, so each commit stays on main) and CI passes, the
  release workflow tags that version. With --title, a squash-merged pull request's title is
  counted too, when it is not already one of the commits.
- A releasable change that reaches main without it (a Dependabot fix, say) is released by the
  workflow, which runs this script on a detached commit above main and tags that commit.

While the version is 0.x a release is a patch, unless a change is marked breaking (`!` after the
type, or a `BREAKING CHANGE:` footer), which gives the next minor. The script never moves the
version to 1.0: that is the owner's decision, made by hand. The changelog section follows
commitizen's layout: BREAKING CHANGE (from the footers' text), Feat, Fix, Refactor and Perf.

Usage:
    uv run python .kodex/scripts/prepare_release.py
    uv run python .kodex/scripts/prepare_release.py --dry-run
    uv run python .kodex/scripts/prepare_release.py --title "<pull request title>" --check   # in CI
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

# Beside this script: one definition of a breaking commit for the commit check and the release.
from check_commit_message import BREAKING_FOOTER, HEADER, is_breaking

ROOT = Path(__file__).resolve().parents[2]
RELEASABLE = {'feat': 'Feat', 'fix': 'Fix', 'refactor': 'Refactor', 'perf': 'Perf'}
ORDER = ['BREAKING CHANGE', 'Feat', 'Fix', 'Refactor', 'Perf']
VERSION_LINE = re.compile(r'^version = "(?P<version>[^"]+)"$', re.MULTILINE)
SECTION = re.compile(r'^## v(?P<version>\d+\.\d+\.\d+) \(', re.MULTILINE)

Commit = tuple[str, str]


def git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def parse_version(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in text.split('.'))
    return major, minor, patch


def last_tag() -> str:
    tags = [t for t in git('tag', '--list', 'v*').split() if re.fullmatch(r'v\d+\.\d+\.\d+', t)]
    if not tags:
        sys.exit('no release tag found; fetch tags first (git fetch --tags)')
    return max(tags, key=lambda t: parse_version(t[1:]))


def version_on(ref: str) -> str:
    """The version in pyproject.toml at ``ref``."""
    match = VERSION_LINE.search(git('show', f'{ref}:pyproject.toml'))
    if not match:
        sys.exit(f'no version in pyproject.toml at {ref}')
    return match['version']


def commits_since(tag: str, base: str) -> list[Commit]:
    """Subjects and bodies of the commits on ``base`` since ``tag``, newest first."""
    out = git('log', '--no-merges', '--format=%s%x00%b%x1e', f'{tag}..{base}')
    found = []
    for record in out.split('\x1e'):
        if record.strip():
            subject, _, body = record.strip('\n').partition('\x00')
            found.append((subject, body))
    return found


def releasable(commits: list[Commit]) -> list[Commit]:
    """The commits that call for a release: a feat, fix, refactor or perf, or anything breaking."""
    return [
        (s, b)
        for s, b in commits
        if ((m := HEADER.match(s)) and m['type'] in RELEASABLE) or is_breaking(s, b)
    ]


def next_version(current: str, breaking: bool) -> str:
    major, minor, patch = parse_version(current)
    if major == 0:
        return f'0.{minor + 1}.0' if breaking else f'0.{minor}.{patch + 1}'
    sys.exit('the version is 1.0 or later; this script only prepares 0.x releases')


def section(version: str, commits: list[Commit], date: str | None = None) -> str:
    groups: dict[str, list[str]] = {}
    for subject, body in commits:
        match = HEADER.match(subject)
        if not match:
            continue
        scope = f'**{match["scope"]}**: ' if match['scope'] else ''
        if match['type'] in RELEASABLE:
            groups.setdefault(RELEASABLE[match['type']], []).append(f'- {scope}{match["subject"]}')
        footers = [' '.join(f['text'].split()) for f in BREAKING_FOOTER.finditer(body)]
        # A breaking change says what broke in its footer; without one, its subject says it.
        for text in footers or ([match['subject']] if match['breaking'] else []):
            groups.setdefault('BREAKING CHANGE', []).append(f'- {scope}{text}')
    parts = [f'## v{version} ({date or dt.date.today().isoformat()})']
    for name in ORDER:
        if name in groups:
            parts.append(f'### {name}\n\n' + '\n'.join(groups[name]))
    return '\n\n'.join(parts) + '\n'


def plan(title: str | None, body: str, base: str) -> tuple[str, str] | None:
    """The next version and its changelog section, or None when nothing calls for a release."""
    tag = last_tag()
    on_base = version_on(base)
    if parse_version(on_base) > parse_version(tag[1:]):
        sys.exit(
            f'{base} carries v{on_base}, which is not tagged yet: wait for its release to finish, '
            'then run this again'
        )
    # The commits the release will contain: everything on this branch since the tag, which includes
    # the base it was rebased on (and, in CI, the pull request's own commits). A squash-merged pull
    # request's title stands for its commits when it is not one of them.
    commits = commits_since(tag, 'HEAD')
    if title and all(subject != title for subject, _ in commits):
        commits.insert(0, (title, body))
    changes = releasable(commits)
    if not changes:
        return None
    version = next_version(tag[1:], any(is_breaking(s, b) for s, b in changes))
    if not version.startswith('0.'):
        sys.exit(f'refusing to prepare {version}: Konstruct stays below 1.0 until the owner decides')
    return version, section(version, changes)


def without_section(changelog: str, version: str) -> str:
    """``changelog`` with any section already written for ``version`` removed."""
    matches = list(SECTION.finditer(changelog))
    for index, match in enumerate(matches):
        if match['version'] == version:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
            return changelog[: match.start()] + changelog[end:]
    return changelog


def sections(changelog: str) -> tuple[str, dict[str, str]]:
    """The text before the first section, and each section by version."""
    matches = list(SECTION.finditer(changelog))
    found = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
        found[match['version']] = changelog[match.start() : end].rstrip('\n') + '\n'
    return changelog[: matches[0].start()] if matches else changelog, found


def ancestor(ref: str) -> bool:
    done = subprocess.run(['git', 'merge-base', '--is-ancestor', ref, 'HEAD'], cwd=ROOT, capture_output=True)
    return done.returncode == 0


def above(tag: str) -> bool:
    """Whether ``tag`` is a release the workflow made from a detached commit: the tagged commit is
    not part of this history, but its parent is."""
    return not ancestor(tag) and ancestor(f'{tag}^')


def history(changelog: str) -> str:
    """The changelog's released history, checked against the tags, newest first.

    A release keeps the section written here (later edits to it stand), unless the workflow made it
    from a detached commit above this history: then it takes the section its own tag records, which
    adds a release this changelog has not recorded, and corrects a section a stale pull request
    wrote for a version that was released differently. Sections for versions never released go."""
    preamble, here = sections(changelog)
    tags = sorted(
        (t for t in git('tag', '--list', 'v*').split() if re.fullmatch(r'v\d+\.\d+\.\d+', t)),
        key=lambda t: parse_version(t[1:]),
        reverse=True,
    )
    kept = []
    for tag in tags:
        version = tag[1:]
        if version in here and not above(tag):
            kept.append(here[version])
            continue
        _, recorded = sections(git('show', f'{tag}:CHANGELOG.md'))
        text = recorded.get(version) or here.get(version)
        if text:
            kept.append(text)
    return preamble + '\n'.join(kept)


def write(version: str, changes: str) -> None:
    pyproject = ROOT / 'pyproject.toml'
    pyproject.write_text(
        VERSION_LINE.sub(f'version = "{version}"', pyproject.read_text(encoding='utf-8'), count=1),
        encoding='utf-8',
    )
    # Only konstruct's own entry in the lock changes; relocking would churn the rest of the file.
    lock = ROOT / 'uv.lock'
    lock.write_text(
        re.sub(
            r'(\[\[package\]\]\nname = "konstruct"\nversion = ")[^"]+(")',
            rf'\g<1>{version}\g<2>',
            lock.read_text(encoding='utf-8'),
            count=1,
        ),
        encoding='utf-8',
    )
    init = ROOT / 'konstruct' / '__init__.py'
    init.write_text(
        re.sub(r"^__version__ = '[^']*'$", f"__version__ = '{version}'", init.read_text(), flags=re.M),
        encoding='utf-8',
    )
    changelog = ROOT / 'CHANGELOG.md'
    released = history(without_section(changelog.read_text(encoding='utf-8'), version))
    changelog.write_text(changes + ('\n' + released if released else ''), encoding='utf-8')


def check(title: str | None, body: str, base: str) -> int:
    """For a pull request: pass when it prepares no version, or prepares exactly the version and
    changelog section the script works out against the current base."""
    prepared = VERSION_LINE.search((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    if prepared is None or prepared['version'] == version_on(base):
        print('this pull request prepares no release; main releases its changes after the merge')
        return 0
    expected = plan(title, body, base)
    if expected is None:
        print(f'v{prepared["version"]} is prepared, but nothing in this pull request calls for a release')
        return 1
    version, changes = expected
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    top = SECTION.search(changelog)
    written = changelog[top.start() :] if top else ''
    following = SECTION.search(written, 1)
    written = written[: following.start()] if following else written
    undated = re.compile(r'^(## v\S+) \([^)]*\)', re.MULTILINE)
    if prepared['version'] != version or undated.sub(r'\1', written.strip()) != undated.sub(
        r'\1', changes.strip()
    ):
        print(
            f'the prepared release no longer matches main: run prepare_release.py again ({version} expected)'
        )
        return 1
    print(f'v{version} is prepared and matches main')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--title', help="the pull request's title, as it will be squash-merged")
    parser.add_argument(
        '--body', default='', help="the merge commit's body, to find a BREAKING CHANGE footer"
    )
    parser.add_argument('--base', default='origin/main', help='the commit the release builds on')
    parser.add_argument('--no-fetch', action='store_true', help='use the local tags and base as they are')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='print the version and changelog only')
    mode.add_argument('--check', action='store_true', help='check what a pull request prepares')
    args = parser.parse_args(argv)

    if not args.no_fetch and args.base.startswith('origin/'):
        # --force: the v0 tag moves with every release, and the remote's tags are the ones that count.
        git('fetch', '--quiet', '--tags', '--force', 'origin', args.base.removeprefix('origin/'))
    if args.check:
        return check(args.title, args.body, args.base)
    planned = plan(args.title, args.body, args.base)
    if planned is None:
        print(f'nothing to release since {last_tag()}')
        return 0
    version, changes = planned
    if args.dry_run:
        print(version)
        print(changes)
        return 0
    write(version, changes)
    print(version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
