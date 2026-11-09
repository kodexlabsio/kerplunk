"""Check a commit message against the Kodex Labs commit rules.

commitizen (`cz check`) enforces the Conventional Commits structure. This script adds the rules
the Kodex Labs commitlint configuration layers on top: a fixed scope list, sentence-case
subjects, line limits, no AI attribution and no business language. It runs as a pre-commit
`commit-msg` hook and in CI.

It also reads the interface snapshots in tests/golden/interfaces/ (the command line, JSON outputs,
MCP tools, Python API and konstruct.toml keys): a commit that removes or changes an interface there
must be marked breaking. Over a range it compares each commit with its parent, and the range as a
whole from its merge base to its head unless a commit in it carries both marks; given a message
file, as the commit-msg hook, it compares the staged snapshots with HEAD. Konstruct merges by
rebase, so a merge commit, in a range or being made, is refused rather than checked.

Usage:
    python .kodex/scripts/check_commit_message.py <commit-message-file>
    python .kodex/scripts/check_commit_message.py --rev-range origin/main..HEAD
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

TYPES = {'build', 'chore', 'ci', 'docs', 'feat', 'fix', 'perf', 'refactor', 'revert', 'style', 'test'}

SCOPES = {
    # Inputs
    'airflow',
    'dagster',
    'dbt',
    'definitions',
    'iceberg',
    'sql',
    'warehouse',
    # Graph and outputs
    'build',
    'graph',
    'html',
    'lineage',
    'query',
    'report',
    'viewer',
    # Interfaces
    'cli',
    'mcp',
    'plugins',
    'skill',
    # Project
    'examples',
    'readme',
    'tests',
    # Tooling
    'ci',
    'config',
    'deps',
    'deps-dev',
    'release',
}

MAX_LINE = 100

AI_TOOL = re.compile(
    r'\b(claude|anthropic|copilot|openai|chatgpt|gpt-?[\d.]+o?|codex|gemini|cursor|windsurf|codeium'
    r'|devin|aider|codewhisperer|tabnine|cline|deepseek|grok|qwen|opencode|amazon q)\b',
    re.IGNORECASE,
)
# The repository describes what Konstruct does and how to use it; business, market and
# fundraising material belongs elsewhere (AGENTS.md, Rules).
BUSINESS_WORDS = re.compile(
    r'\b(ventures?|investors?|valuations?|fundrais(e|es|ed|er|ers|ing)|pitch decks?|pricing)\b',
    re.IGNORECASE,
)
BUSINESS_ACRONYMS = re.compile(r'\b(VCs?|ARR|TAM)\b')
TRAILER = re.compile(r'^([A-Za-z][\w-]*):\s*(.+)$')
HEADER = re.compile(r'^(?P<type>\w+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<subject>.*)$')
SCISSORS = re.compile(r'^# -+ >8 -+$', re.MULTILINE)
# A breaking change's footer, with its text (prepare_release.py writes it into the changelog).
BREAKING_FOOTER = re.compile(r'^BREAKING[ -]CHANGE: (?P<text>.+(?:\n(?!\n).+)*)', re.MULTILINE)
INTERFACES = 'tests/golden/interfaces'


def is_ai_attribution(line: str) -> bool:
    """Trailers and footer lines that AI coding agents add to attribute themselves."""
    trailer = TRAILER.match(line)
    if trailer:
        key, value = trailer.groups()
        return bool(
            re.match(r'^(ai-)?(assisted|generated)-(by|with)$|^made-with$', key, re.IGNORECASE)
            or AI_TOOL.search(key)
            or (re.match(r'^co-(authored|developed)-by$', key, re.IGNORECASE) and AI_TOOL.search(value))
        )
    return '\U0001f916' in line or bool(
        re.search(r'\b(generated|created|written|assisted)\s+(with|by|using)\b', line, re.IGNORECASE)
        and AI_TOOL.search(line)
    )


def sentence_case(value: str) -> str:
    """commitlint's sentence-case: the first word capitalised and otherwise lower case."""
    word = value.split(' ')[0]
    return value[:1].upper() + word[1:].lower() + value[len(word) :]


def check(raw: str) -> list[str]:
    """Return the rule violations in a commit message, empty when it passes."""
    message = SCISSORS.split(raw)[0]
    lines = [line for line in message.splitlines() if not line.startswith('#')]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ['the commit message is empty']

    header = lines[0]
    if header.startswith(('Merge ', 'Revert ', 'fixup! ', 'squash! ')):
        return []

    errors: list[str] = []
    if len(header) > MAX_LINE:
        errors.append(f'the header is longer than {MAX_LINE} characters')
    match = HEADER.match(header)
    if not match:
        errors.append("the header must look like 'type(scope): Subject'")
    else:
        kind, scope, subject = match['type'], match['scope'], match['subject']
        if kind not in TYPES:
            errors.append(f"type '{kind}' is not one of {', '.join(sorted(TYPES))}")
        if scope is not None:
            if scope != scope.lower():
                errors.append(f"scope '{scope}' must be lower case")
            if scope not in SCOPES:
                errors.append(f"scope '{scope}' is not one of {', '.join(sorted(SCOPES))}")
        if not subject.strip():
            errors.append('the subject is empty')
        else:
            if subject != sentence_case(subject):
                errors.append(f"the subject must be sentence case: '{sentence_case(subject)}'")
            if subject.endswith('.'):
                errors.append('the subject must not end with a full stop')

    if len(lines) > 1 and lines[1].strip():
        errors.append('leave a blank line between the header and the body')
    errors.extend(
        f'line {number} is longer than {MAX_LINE} characters'
        for number, line in enumerate(lines[1:], start=2)
        if len(line) > MAX_LINE and not re.match(r'^\s*(https?://|\[\d+\]:)', line)
    )

    words = sorted(
        {m.group(0) for line in lines for m in BUSINESS_WORDS.finditer(line)}
        | {m.group(0) for line in lines for m in BUSINESS_ACRONYMS.finditer(line)}
    )
    if words:
        errors.append(f'keep business and fundraising language out of the repository: {", ".join(words)}')

    found = [line.strip() for line in lines if line.strip() and is_ai_attribution(line.strip())]
    if found:
        errors.append(f'remove AI attribution from the commit message: {" | ".join(found)}')
    return errors


def breaking_marks(subject: str, body: str) -> tuple[bool, bool]:
    """Whether a commit marks its change breaking with ``!`` after the type, and with a
    ``BREAKING CHANGE:`` footer."""
    match = HEADER.match(subject)
    return bool(match and match['breaking']), bool(BREAKING_FOOTER.search(body))


def is_breaking(subject: str, body: str) -> bool:
    """Whether a commit is breaking for the release: either mark makes it so."""
    return any(breaking_marks(subject, body))


def _types(value: Any) -> set[str]:
    return set(str(value).split('|')) - {'null'}


def uncovered(key: str, after: dict[str, Any]) -> bool:
    """Whether a JSON field is gone because the projects no longer fill what holds it: a list or
    map left empty (``X[]`` gone while ``X`` is still there), or an object or list that is only
    ever ``null``. The snapshot test says so in its message; the commit check counts it as the
    removal it may be."""
    if not key.startswith('json '):
        return False
    start = key.find(' ', len('json ')) + 1
    for at in range(start + 1, len(key)):
        if key[at] not in '.[{' or key[:at] not in after:
            continue
        if not _types(after[key[:at]]) or (key[at] in '[{' and key[: at + 2] not in after):
            return True
    return False


# How many values a positional takes, by its entry in cli.json (``positional <type>, <arity>``):
# one that accepts at least what it did is compatible, so a required positional may become optional.
POSITIONAL = re.compile(r'^positional (?P<type>\S+), (?:(?P<count>\d+) values?, required|(?P<arity>.+))$')
ARITIES = {
    'required': (1, 1),
    'optional': (0, 1),
    'any number': (0, None),
    'one or more, required': (1, None),
}
# A parameter of a Python function or an MCP tool: its position or keyword, its types, and
# whether it is required or what its default is.
PARAMETER = re.compile(
    r'^(?P<place>#\d+ |keyword )?(?P<types>.*?)(?P<given>, required| = .*|, variadic \w+)$'
)


def _union(types: str) -> set[str]:
    """The members of a union annotation or schema type, split at the top-level ``|``."""
    members, depth, member = set(), 0, ''
    for char in types:
        depth += (char in '[(') - (char in '])')
        if char == '|' and depth == 0:
            members.add(member.strip())
            member = ''
        else:
            member += char
    return members | {member.strip()}


def _arity(entry: str) -> tuple[str, int, int | None] | None:
    match = POSITIONAL.match(entry)
    if not match:
        return None
    if match['count']:
        return match['type'], int(match['count']), int(match['count'])
    if match['arity'] not in ARITIES:
        return None
    return match['type'], *ARITIES[match['arity']]


def compatible(key: str, before: str, after: str) -> bool:
    """Whether an entry that changed still accepts everything it did: a required option, input,
    positional, parameter or argument made optional, or a parameter or argument that takes more
    types. A changed default, a narrowed type and a moved parameter break callers."""
    if key.startswith('konstruct '):
        old_arity, new_arity = _arity(before), _arity(after)
        if old_arity and new_arity:
            (kind, low, high), (new_kind, new_low, new_high) = old_arity, new_arity
            wider = new_high is None or (high is not None and high <= new_high)
            return kind == new_kind and new_low <= low and wider
        return before == f'{after}, required'
    if key.startswith('action input '):
        return before == 'required' and after.startswith('= ')
    parameter = key.startswith('mcp ') and key.count(' ') == 2
    parameter |= key.startswith('python ') and not key.endswith(' ->')
    old, new = PARAMETER.match(before), PARAMETER.match(after)
    if not parameter or not old or not new or old['place'] != new['place']:
        return False
    if old['given'] != new['given'] and not (old['given'] == ', required' and new['given'].startswith(' = ')):
        return False
    return _union(old['types']) <= _union(new['types'])


def _json_compatible(before: Any, after: Any) -> bool:
    """A JSON field's types are inferred from example data, so it may hold fewer of them than it
    did (``null`` aside, which overlaps any type) or gain one where it only ever held ``null``.
    Emptied to only ``null`` it breaks, as does a field that gains a type readers did not see."""
    old, new = _types(before), _types(after)
    return not old or (bool(new) and new <= old)


def _breaks_by_key(before: dict[str, Any], after: dict[str, Any]) -> dict[str, str]:
    breaks = {}
    for key, value in before.items():
        if key not in after:
            breaks[key] = f'{key} is removed'
        elif after[key] != value:
            if key.startswith('json ') and _json_compatible(value, after[key]):
                continue
            if not key.startswith('json ') and compatible(key, str(value), str(after[key])):
                continue
            breaks[key] = f'{key} changes from {value!r} to {after[key]!r}'
    for key, value in after.items():
        if key not in before and 'required' in str(value) and key.rsplit(' ', 1)[0] in before:
            breaks[key] = f'{key} is added as {value!r}'
    return breaks


def interface_breaks(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What a change to an interface snapshot breaks: an entry removed or changed incompatibly
    (see :func:`compatible`), or a required parameter added to a command, tool or method that
    already existed.

    A JSON field breaks when it is removed, emptied to only ``null`` or given a type it did not
    hold (``null`` aside: widening ``str`` to ``null|str`` passes). A field gone because the
    projects stop filling what holds it is counted as removed too, as a change to the code can do
    the same: restore the coverage, or mark the change breaking."""
    return list(_breaks_by_key(before, after).values())


def _snapshot(revision: str, path: str) -> dict[str, Any]:
    """A snapshot at ``revision``, the index when it is empty (``git show :path``)."""
    shown = subprocess.run(['git', 'show', f'{revision}:{path}'], capture_output=True, text=True, check=False)
    return dict(json.loads(shown.stdout)) if shown.returncode == 0 else {}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', *args], capture_output=True, text=True, check=False)


Breaks = dict[tuple[str, str], str]


def _breaks(before: str | None, after: str) -> Breaks:
    """What ``after`` (a commit, or ``''`` for the index) breaks against ``before`` (nothing when
    None), by snapshot and entry. Renames are listed as a removal and an addition, so renaming a
    snapshot cannot hide a change to it."""
    if before is None:
        listed = _git(
            'diff-tree',
            '--root',
            '--no-renames',
            '--no-commit-id',
            '--name-only',
            '-r',
            after,
            '--',
            INTERFACES,
        )
    elif after:
        listed = _git('diff', '--no-renames', '--name-only', before, after, '--', INTERFACES)
    else:
        listed = _git('diff', '--no-renames', '--cached', '--name-only', before, '--', INTERFACES)
    if listed.returncode != 0:
        raise SystemExit(
            f'git could not list the files {after or "the index"} changes: {listed.stderr.strip()}'
        )
    found: Breaks = {}
    for path in listed.stdout.split():
        old = _snapshot(before, path) if before else {}
        for key, item in _breaks_by_key(old, _snapshot(after, path)).items():
            found[Path(path).stem, key] = f'{Path(path).stem}: {item}'
    return found


MERGES = 'merge commits are not checked; rebase instead'
BREAKS = (
    "it breaks an interface; restore it, or mark the change breaking with both '!' after the type "
    "and a 'BREAKING CHANGE:' footer that says what users must change: "
)


def _merging() -> bool:
    return Path(_git('rev-parse', '--git-path', 'MERGE_HEAD').stdout.strip()).is_file()


def is_merge(sha: str) -> bool:
    parents = _git('rev-list', '--parents', '-n', '1', sha)
    if parents.returncode != 0:
        raise SystemExit(f'git could not read the parents of {sha}: {parents.stderr.strip()}')
    return len(parents.stdout.split()) > 2


def broken_interfaces(sha: str) -> list[str]:
    """The interfaces commit ``sha`` breaks, from the snapshots it changes, compared with its
    parent (with nothing for a root commit). Konstruct merges by rebase only, so a merge commit is
    not compared at all: :func:`main` refuses it."""
    parent = _git('rev-parse', '--verify', '--quiet', f'{sha}^1')
    return list(_breaks(parent.stdout.strip() if parent.returncode == 0 else None, sha).values())


def net_breaks(rev_range: str) -> list[str]:
    """What a range ``A..B`` breaks as a whole, from the merge base of A and B to B, so a change
    split across commits, or brought in by a merge, is still seen. Nothing for a single revision."""
    if '..' not in rev_range:
        return []
    start, end = (part.strip('.') or 'HEAD' for part in rev_range.split('..', 1))
    base = _git('merge-base', start, end)
    if base.returncode != 0 or not base.stdout.strip():
        raise SystemExit(f'git found no merge base for {rev_range}: {base.stderr.strip()}')
    return list(_breaks(base.stdout.strip(), end).values())


def staged_breaks() -> list[str]:
    """The interfaces the commit being made breaks, from the staged snapshots compared with HEAD,
    so the commit-msg hook catches what CI catches. Nothing outside a git repository."""
    if _git('rev-parse', '--verify', '--quiet', 'HEAD').returncode != 0:
        return []  # outside a repository, or a root commit, which only adds
    return list(_breaks('HEAD', '').values())


def messages_in_range(rev_range: str) -> list[tuple[str, str]]:
    output = subprocess.run(
        ['git', 'log', '--format=%H%x00%B%x1e', rev_range],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    commits = []
    for record in output.split('\x1e'):
        if record.strip():
            sha, body = record.strip('\n').split('\x00', 1)
            commits.append((sha, body))
    return commits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('message_file', nargs='?', type=Path)
    group.add_argument('--rev-range')
    args = parser.parse_args(argv)

    if args.rev_range:
        commits = messages_in_range(args.rev_range)
    else:
        commits = [('message', args.message_file.read_text(encoding='utf-8'))]

    failed = False
    marked = False

    def report(name: str, header: str, errors: list[str]) -> None:
        nonlocal failed
        failed = True
        print(f'{name}: {header}', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)

    for name, raw in commits:
        errors = check(raw)
        subject, _, body = raw.strip().partition('\n')
        both = all(breaking_marks(subject, body))
        marked |= both
        merge = is_merge(name) if args.rev_range else _merging()
        if merge:
            errors.append(MERGES)
        elif not both:
            broken = broken_interfaces(name) if args.rev_range else staged_breaks()
            if broken:
                errors.append(BREAKS + '; '.join(broken))
        if errors:
            report(name[:7], raw.splitlines()[0] if raw.strip() else '', errors)
    if args.rev_range and not marked:
        broken = net_breaks(args.rev_range)
        if broken:
            report(args.rev_range, 'the range as a whole', [BREAKS + '; '.join(broken)])
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
