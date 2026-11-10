"""Trust in a project's own connection settings, recorded outside the repository.

A repository's ``konstruct.toml`` and ``profiles.yml`` can name a warehouse, read environment
variables into its connection and point Konstruct at paths outside the checkout, and its dbt
project runs its own macros and SQL when dbt runs. Konstruct uses those settings, and runs dbt,
only once the user has trusted the project: ``konstruct trust`` records a SHA-256 of the
connection and path settings in a file of the user's own (``trusted.json`` in the user's
configuration directory), and any change to them no longer matches the record, so the project
is untrusted again until it is reviewed and trusted once more. Until then the build skips those
connections, never expands the environment for them, refuses paths outside the project and
does not run dbt. The record does not cover the dbt project's models and macros.

Settings that do not come from the repository are always trusted: ``~/.dbt/profiles.yml``, a
``DBT_PROFILES_DIR`` outside the project, and build options passed by the caller.
``KONSTRUCT_TRUST_CONFIG=1`` (or ``--trust-config``) trusts the repository's settings too, for
CI on branches whose content is already trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from konstruct.engine.config import Config
    from konstruct.interfaces.layout import Style

ENVIRONMENT = 'KONSTRUCT_TRUST_CONFIG'
"""The environment variable that trusts every project's own settings when it is ``1``."""

SETTINGS = ('warehouse', 'usage')
"""The keys of ``konstruct.toml`` that carry connections or read through them, recorded as written. Path settings
(:data:`konstruct.engine.config.PATH_SETTINGS`) are recorded with where each leads when that is
outside the project."""

_UNTRUSTED = 'the project is not trusted'
_ADVICE = 'run `konstruct trust` after reviewing it'
WARNING = f'{_UNTRUSTED}; {_ADVICE}'

_TOML_VARIABLE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)')
_PROFILE_VARIABLE = re.compile(r"env_var\(\s*['\"]([^'\"]+)['\"]")


def trust_file() -> Path:
    """Where trust is recorded: ``$XDG_CONFIG_HOME/konstruct/trusted.json``, otherwise the
    platform's user configuration directory (``~/.config``, ``~/Library/Application Support``
    or ``%APPDATA%``)."""
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if xdg and Path(xdg).is_absolute():
        base = Path(xdg)
    elif sys.platform == 'win32' and os.environ.get('APPDATA'):
        base = Path(os.environ['APPDATA'])
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path.home() / '.config'
    return base / 'konstruct' / 'trusted.json'


def trusted_by_environment() -> bool:
    """Whether ``KONSTRUCT_TRUST_CONFIG`` trusts every project's own settings."""
    return os.environ.get(ENVIRONMENT, '').strip().lower() in ('1', 'true', 'yes')


@dataclass
class Trust:
    """Whether a project's own connection settings are trusted, and what that covers."""

    root: Path
    """The project."""
    state: str
    """``trusted`` (recorded and unchanged), ``environment`` (``KONSTRUCT_TRUST_CONFIG``),
    ``changed`` (recorded, but the content has changed since) or ``untrusted``."""
    sha256: str
    """The digest of the content that carries connections, as ``konstruct trust`` records it."""
    files: list[str] = field(default_factory=list)
    """The files that content comes from, relative to the project."""
    variables: list[str] = field(default_factory=list)
    """The environment variables that content reads once it is trusted."""
    dbt_projects: list[str] = field(default_factory=list)
    """The dbt projects Konstruct runs dbt in only once the project is trusted. The record does
    not cover their models and macros, which dbt runs with the user's environment."""
    record: Path = field(default_factory=trust_file)
    """The user's file that records trust."""
    action: str = ''
    """What the call did: ``recorded``, ``unchanged`` (the same record was already there),
    ``revoked``, or empty for a check."""

    @property
    def trusted(self) -> bool:
        """Whether the build uses the project's own connections and paths."""
        return self.state in ('trusted', 'environment')

    def to_dict(self) -> dict[str, Any]:
        return {
            'root': str(self.root),
            'state': self.state,
            'trusted': self.trusted,
            'sha256': self.sha256,
            'files': self.files,
            'variables': self.variables,
            'dbt_projects': self.dbt_projects,
            'record': str(self.record),
            'action': self.action,
        }

    @property
    def environment_variable(self) -> str:
        """The environment variable that trusts every project."""
        return ENVIRONMENT

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, trust_lines

        return show(trust_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def check(root: Path) -> Trust:
    """Whether the project at ``root`` has its own connection settings trusted.

    Raises:
        konstruct.engine.config.ConfigError: The configuration cannot be read.
    """
    from konstruct.engine.config import read_config

    return _check(read_config(root))


def trust(root: Path) -> Trust:
    """Trust the project's own connection settings as they are now, until they change.

    Raises:
        konstruct.engine.config.ConfigError: The configuration cannot be read.
    """
    from konstruct.engine.config import read_config

    config = read_config(root)
    digest, files, variables, projects = _fingerprint(config)
    records = _records()
    previous = records.get(str(config.root))
    action = 'unchanged' if isinstance(previous, dict) and previous.get('sha256') == digest else 'recorded'
    records[str(config.root)] = {
        'sha256': digest,
        'trusted_at': datetime.now(UTC).isoformat(timespec='seconds'),
    }
    _write(records)
    state = 'environment' if trusted_by_environment() else 'trusted'
    return Trust(config.root, state, digest, files, variables, projects, action=action)


def revoke(root: Path) -> Trust:
    """Remove the record that trusts the project's own connection settings.

    Raises:
        konstruct.engine.config.ConfigError: The configuration cannot be read.
    """
    from konstruct.engine.config import read_config

    config = read_config(root)
    records = _records()
    removed = records.pop(str(config.root), None) is not None
    if removed:
        _write(records)
    result = _check(config)
    result.action = 'revoked' if removed else 'unchanged'
    return result


def is_trusted(config: Config) -> bool:
    """Whether ``config``'s own connections and paths may be used. Only a project with a record
    is fingerprinted, so an untrusted build costs no more than reading the record."""
    if trusted_by_environment():
        return True
    record = _records().get(str(config.root))
    return isinstance(record, dict) and record.get('sha256') == _fingerprint(config)[0]


def untrusted_warning(config: Config, skipped: list[str]) -> str:
    """The build's one warning about what it skipped for want of trust."""
    changed = ' (its settings changed after `konstruct trust`)' if str(config.root) in _records() else ''
    return f'{_UNTRUSTED}{changed}; {_ADVICE}; skipped {", ".join(skipped)}'


@contextmanager
def trusting(enabled: bool) -> Iterator[None]:
    """Trust every project's own settings while the block runs, as ``KONSTRUCT_TRUST_CONFIG=1``
    does (for ``--trust-config``), then restore the environment."""
    if not enabled:
        yield
        return
    previous = os.environ.get(ENVIRONMENT)
    os.environ[ENVIRONMENT] = '1'
    try:
        yield
    finally:
        if previous is None:
            del os.environ[ENVIRONMENT]
        else:
            os.environ[ENVIRONMENT] = previous


def _check(config: Config) -> Trust:
    digest, files, variables, projects = _fingerprint(config)
    if trusted_by_environment():
        state = 'environment'
    else:
        record = _records().get(str(config.root))
        if not isinstance(record, dict):
            state = 'untrusted'
        else:
            state = 'trusted' if record.get('sha256') == digest else 'changed'
    return Trust(config.root, state, digest, files, variables, projects)


def _fingerprint(config: Config) -> tuple[str, list[str], list[str], list[str]]:
    """The digest of everything in the project that carries a connection or a path: the
    connection settings of its configuration file, each path setting that leads outside the
    project with where it leads, the repository's own profiles.yml files, and the same for each
    repository it lists. Values are hashed as written, before any environment variable is read.
    Also returns the files that content comes from, the variables it reads and the dbt projects."""
    files: list[str] = []
    variables: set[str] = set()
    projects: list[str] = []
    content = _content(config, config.root, files, variables, projects, set())
    blob = json.dumps(content, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(blob).hexdigest(), files, sorted(variables), projects


def _content(
    config: Config, top: Path, files: list[str], variables: set[str], projects: list[str], seen: set[Path]
) -> dict[str, Any]:
    from konstruct.engine.config import (
        dbt_projects,
        duckdb_paths_outside,
        paths_outside,
        read_config,
        read_settings,
        repository_profiles,
    )

    seen.add(config.root)
    content: dict[str, Any] = {}
    if config.config_file is not None:
        data = read_settings(config.config_file)
        settings: dict[str, Any] = {k: v for k, v in data.items() if k in SETTINGS}
        outside = [*paths_outside(config.root, data), *duckdb_paths_outside(config.root, data)]
        if outside:
            settings['paths outside the project'] = [[setting, str(where)] for _, setting, where in outside]
        if settings:
            name = _name(config.config_file, top)
            content[name] = settings
            files.append(name)
            variables.update(_TOML_VARIABLE.findall(json.dumps(settings, default=str)))
    found = dbt_projects(config)
    projects.extend(project.name for project in found)
    for path in repository_profiles(config, found):
        name = _name(path, top)
        data_bytes = path.read_bytes()
        content[name] = hashlib.sha256(data_bytes).hexdigest()
        files.append(name)
        variables.update(_PROFILE_VARIABLE.findall(data_bytes.decode('utf-8', errors='replace')))
    for repository in config.repositories:
        if repository.is_dir() and repository not in seen:
            other = read_config(repository)
            content[f'repository {_name(repository, top)}'] = _content(
                other, top, files, variables, projects, seen
            )
    return content


def _name(path: Path, root: Path) -> str:
    return os.path.relpath(path, root) if path.is_relative_to(root) else str(path)


def _records() -> dict[str, Any]:
    try:
        data = json.loads(trust_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    projects = data.get('projects') if isinstance(data, dict) else None
    return dict(projects) if isinstance(projects, dict) else {}


def _write(records: dict[str, Any]) -> None:
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}')
    temporary.write_text(json.dumps({'version': 1, 'projects': records}, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
