"""A commit's files, written as the commit holds them, and the user's own cache of what is built
from them.

Building the project as it was at a commit (the timeline, the gate's base) needs the commit's files
on disk. They are read from git's objects with ``git ls-tree`` and ``git cat-file``, never through
``git archive``, whose ``export-ignore`` and ``export-subst`` attributes the commit itself sets, and
never by checking anything out. Only regular files are written, each at a path inside the tree: a
symbolic link is written as the file it names when that file is in the same part of the commit,
and left out otherwise, and a submodule is another repository.

What is built from a commit is kept in the user's cache directory, never in the repository or a
path its configuration names, so a change cannot commit what a later run reads back; a directory or
file that is a link, or that someone else can write, is neither read nor written, and in CI there
is no cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, cast

from konstruct import __version__

_REGULAR = (b'100644', b'100755')
_LINK = b'120000'
_HOPS = 40
"""The most links followed to reach a file, as the kernel allows."""


def write_tree(
    top: Path, ref: str, prefix: Path, tree: Path, present: dict[str, bytes] | None = None
) -> None:
    """Write the files under ``prefix`` of the commit ``ref`` names in the repository at ``top``
    into ``tree``, at the same paths: every regular file, and each symbolic link whose target,
    followed within the commit, is a regular file under ``prefix``.

    ``tree`` is a new directory, unless ``present`` is given: then it is the tree this function
    wrote before, with the blob of each file it holds, and only what differs is written or
    removed, so writing the commits of a range one after another costs what changed between them.
    ``present`` is brought up to date.

    Raises:
        konstruct.analysis.diff.DiffError: git cannot list or read the commit.
    """
    from konstruct.analysis.diff import DiffError

    if ref.startswith('-'):
        raise DiffError(f'git cannot list {ref}: not a commit')
    git = ['git', '--literal-pathspecs', '-C', str(top)]
    where = [] if str(prefix) == '.' else ['--', prefix.as_posix()]
    listing = subprocess.run(
        [*git, 'ls-tree', '-r', '-z', '--full-tree', f'{ref}^{{commit}}', *where], capture_output=True
    )
    if listing.returncode != 0:
        raise DiffError(f'git cannot list {ref}: {listing.stderr.decode(errors="replace").strip()}')
    scope = '' if str(prefix) == '.' else prefix.as_posix().rstrip('/') + '/'
    files: dict[str, bytes] = {}
    links: dict[str, bytes] = {}
    for record in listing.stdout.split(b'\0'):
        info, _, raw = record.partition(b'\t')
        fields = info.split()
        if len(fields) != 3 or fields[1] != b'blob':
            continue
        name = raw.decode('utf-8', 'surrogateescape')
        if not _safe(name) or not name.startswith(scope):
            continue
        if fields[0] in _REGULAR:
            files[name] = fields[2]
        elif fields[0] == _LINK:
            links[name] = fields[2]
    read = _read(git, list(links.values()))
    targets = {name: read[blob].decode('utf-8', 'surrogateescape') for name, blob in links.items()}
    for name in links:
        found = _follow(name, targets, files, scope)
        if found is not None:
            files[name] = files[found]
    if present is None:
        # A new directory, so nothing in it is a link that could lead elsewhere.
        tree.mkdir(parents=True)
        present = {}
    for name in sorted(set(present) - set(files)):
        gone = tree.joinpath(*name.split('/'))
        gone.unlink(missing_ok=True)
        del present[name]
        # A directory the commit no longer has is removed, so a file can take its place.
        for folder in gone.parents:
            if folder == tree or not folder.is_relative_to(tree) or any(folder.iterdir()):
                break
            folder.rmdir()
    wanted = {name: blob for name, blob in files.items() if present.get(name) != blob}
    contents = _read(git, sorted(set(wanted.values())))
    for name, blob in sorted(wanted.items()):
        target = tree.joinpath(*name.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        # A new file, never one already there that could be a link to elsewhere.
        with target.open('xb') as handle:
            handle.write(contents[blob])
        present[name] = blob


def _safe(name: str) -> bool:
    return bool(name) and not any(part in ('', '.', '..') or '\\' in part for part in name.split('/'))


def _follow(name: str, targets: dict[str, str], files: dict[str, bytes], scope: str) -> str | None:
    """The regular file under ``scope`` that the link ``name`` leads to through the commit's own
    links, or None when it leads outside, to a directory, nowhere, or round in a loop."""
    for _ in range(_HOPS):
        target = targets[name]
        if target.startswith('/'):
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if not _safe(resolved) or not resolved.startswith(scope):
            return None
        if resolved in files:
            return resolved
        if resolved not in targets:
            return None
        name = resolved
    return None


def _read(git: list[str], blobs: list[bytes]) -> dict[bytes, bytes]:
    """The content of each blob, from one ``git cat-file --batch``, fed from another thread so
    that neither side waits on the other."""
    from konstruct.analysis.diff import DiffError

    if not blobs:
        return {}
    process = subprocess.Popen([*git, 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdin, stdout = cast(IO[bytes], process.stdin), cast(IO[bytes], process.stdout)

    def feed() -> None:
        with contextlib.suppress(BrokenPipeError):
            stdin.write(b''.join(blob + b'\n' for blob in blobs))
        with contextlib.suppress(BrokenPipeError):
            stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    found: dict[bytes, bytes] = {}
    try:
        for blob in blobs:
            header = stdout.readline().split()
            if len(header) != 3 or header[0] != blob or header[1] != b'blob':
                raise DiffError(f'git cannot read {blob.decode()}')
            found[blob] = stdout.read(int(header[2]))
            stdout.read(1)
    finally:
        writer.join()
        stdout.close()
        process.wait()
    return found


def user_cache(name: str) -> Path | None:
    """The user's cache directory for ``name``: ``$XDG_CACHE_HOME/konstruct/<name>``, otherwise in
    the platform's user cache directory (``~/.cache``, ``~/Library/Caches`` or ``%LOCALAPPDATA%``).
    None in CI (``CI`` or ``GITHUB_ACTIONS`` set): a fresh runner gains nothing from a cache, and
    one a workflow restores could hold what a change wrote."""
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        return None
    xdg = os.environ.get('XDG_CACHE_HOME')
    if xdg and Path(xdg).is_absolute():
        base = Path(xdg)
    elif sys.platform == 'win32' and os.environ.get('LOCALAPPDATA'):
        base = Path(os.environ['LOCALAPPDATA'])
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    else:
        base = Path.home() / '.cache'
    return base / 'konstruct' / name


def private(path: Path) -> bool:
    """Whether ``path`` is the user's own: not a link, owned by the user where the system has
    owners, and writable by no one else. A cache that is not is neither read nor written."""
    try:
        found = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(found.st_mode) or found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    owner = getattr(os, 'getuid', None)
    return owner is None or found.st_uid == owner()


def private_directory(path: Path) -> bool:
    """Make ``path`` (and the directories above it in the cache) if need be, readable by the user
    alone, and say whether it is :func:`private`."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return False
    return private(path)


def cache_name(root: Path, ref: str, *extra: str) -> tuple[Path, str, Path, str | None] | None:
    """The repository's top, the commit ``ref`` names and the project's directory in the
    repository, as git gives them, and the name in the cache of what is built from that commit;
    None when git cannot tell. The name covers everything a build depends on beyond the commit's
    files: which repository and work tree (two clones, or two worktrees, keep apart), the
    project's directory in it, the versions of Konstruct, sqlglot and every installed plugin,
    whether plugins are turned off, and ``extra``. It is None, so nothing is cached, where the
    files do not name this repository as git does (see :func:`repository_key`), as for a .git
    file that no git directory names back."""
    from konstruct.analysis.diff import DiffError, run_git

    if ref.startswith('-'):
        return None
    try:
        top = Path(run_git(root, 'rev-parse', '--show-toplevel').strip())
        repository = Path(run_git(root, 'rev-parse', '--absolute-git-dir').strip())
        commit = run_git(root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}').strip()
        prefix = Path(root).resolve().relative_to(top.resolve())
    except (DiffError, OSError, ValueError):
        return None
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        return None
    located = _located(root)
    if located is None or located[0] != top.resolve() or located[1] != str(repository.resolve()):
        return top, commit, prefix, None
    return (
        top,
        commit,
        prefix,
        _digest(located[1], str(located[0]), commit, prefix.as_posix(), *_versions(), *extra),
    )


def repository_key(root: Path, *extra: str) -> str | None:
    """A name in the cache for what is kept for the project at ``root`` across its commits, such
    as the analyses its builds share: by repository, work tree, project directory and the versions
    :func:`cache_name` covers. None when git cannot tell."""
    located = _located(root)
    if located is None:
        return None
    top, repository, prefix = located
    return _digest(repository, str(top), prefix.as_posix(), *_versions(), *extra)


def _located(root: Path) -> tuple[Path, str, Path] | None:
    """The top of the work tree holding ``root``, its git directory and ``root``'s directory in
    it, found from the files alone (a build starts no process to name its cache), or None. The
    nearest ``.git`` above decides: a directory is the git directory; a ``.git`` file (a worktree
    or a submodule) names one only where that git directory names this work tree back, as git
    records it (a worktree's ``gitdir`` file, a submodule's ``core.worktree``), so a file a
    repository commits cannot borrow another repository's name; and a link, a pipe or anything
    else names none."""
    try:
        start = root.resolve()
    except OSError:
        return None
    for top in (start, *start.parents):
        marker = top / '.git'
        try:
            found = marker.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(found.st_mode):
            repository = marker
        elif stat.S_ISREG(found.st_mode):
            pointed = _pointed(top, marker)
            if pointed is None:
                return None
            repository = pointed
        else:
            return None
        return top, str(repository.resolve()), Path(os.path.relpath(start, top))
    return None


def _pointed(top: Path, marker: Path) -> Path | None:
    """The git directory a ``.git`` file names, when that git directory names this work tree."""
    try:
        line = marker.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith('gitdir:'):
        return None
    pointed = (top / line.removeprefix('gitdir:').strip()).resolve()
    if not pointed.is_dir():
        return None
    try:
        back = (pointed / 'gitdir').read_text(encoding='utf-8').strip()
        # Relative to the git directory that holds it, as worktree.useRelativePaths writes it.
        if (pointed / back).resolve() == marker.resolve():
            return pointed
    except (OSError, UnicodeDecodeError):
        pass
    try:
        config = (pointed / 'config').read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    for match in re.finditer(r'^\s*worktree\s*=\s*(.+?)\s*$', config, re.MULTILINE):
        if (pointed / match[1]).resolve() == top.resolve():
            return pointed
    return None


def _versions() -> list[str]:
    from importlib.metadata import entry_points, version

    from konstruct.plugins.registry import GROUPS

    every = entry_points()
    plugins = sorted(
        {
            f'{e.dist.name}=={e.dist.version}' if e.dist is not None else e.value
            for group in GROUPS
            for e in every.select(group=f'konstruct.{group}')
        }
    )
    return [__version__, version('sqlglot'), os.environ.get('KONSTRUCT_NO_PLUGINS', ''), *plugins]


def _digest(*parts: str) -> str:
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()
