"""Check a commit message against the Kodex Labs commit rules.

commitizen (`cz check`) enforces the Conventional Commits structure. This script adds the rules
the Kodex Labs commitlint configuration layers on top: a fixed scope list, sentence-case
subjects, line limits, no AI attribution and no business language. It runs as a pre-commit `commit-msg` hook and in CI.

Usage:
    python .kodex/scripts/check_commit_message.py <commit-message-file>
    python .kodex/scripts/check_commit_message.py --rev-range origin/main..HEAD
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

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
    # Interfaces
    'cli',
    'mcp',
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
    r'\b(venture|investors?|valuations?|fundrais(e|es|ed|ing)|pitch deck|pricing)\b', re.IGNORECASE
)
BUSINESS_ACRONYMS = re.compile(r'\b(VCs?|ARR|TAM)\b')
TRAILER = re.compile(r'^([A-Za-z][\w-]*):\s*(.+)$')
HEADER = re.compile(r'^(?P<type>\w+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<subject>.*)$')
SCISSORS = re.compile(r'^# -+ >8 -+$', re.MULTILINE)


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
            commits.append((sha[:7], body))
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
    for name, raw in commits:
        errors = check(raw)
        if errors:
            failed = True
            print(f'{name}: {raw.splitlines()[0] if raw.strip() else ""}', file=sys.stderr)
            for error in errors:
                print(f'  - {error}', file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
