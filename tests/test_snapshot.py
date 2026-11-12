"""A commit's files written as the commit holds them, and the user's private cache beside them."""

from __future__ import annotations

from pathlib import Path

import pytest

from konstruct.analysis.diff import DiffError
from konstruct.engine.snapshot import cache_name, private, user_cache, write_tree
from tests.test_history import commit, git


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / 'repository'
    (root / 'project' / 'models').mkdir(parents=True)
    git(root, 'init', '-q', '-b', 'main')
    (root / 'outside.txt').write_text('a secret\n')
    (root / 'project' / 'models' / 'orders.sql').write_text('select 1 as id\n')
    (root / 'project' / 'hidden.sql').write_text('select 2 as id\n')
    (root / 'project' / 'stamped.sql').write_text('-- $Format:%H$\nselect 3 as id\n')
    (root / 'project' / '.gitattributes').write_text('hidden.sql export-ignore\nstamped.sql export-subst\n')
    (root / 'project' / 'run.sh').write_text('echo hi\n')
    (root / 'project' / 'run.sh').chmod(0o755)
    (root / 'project' / 'alias.sql').symlink_to('models/orders.sql')
    (root / 'project' / 'alias_of_alias.sql').symlink_to('alias.sql')
    (root / 'project' / 'escape.txt').symlink_to('../outside.txt')
    (root / 'project' / 'absolute.txt').symlink_to('/etc/hostname')
    (root / 'project' / 'linked_models').symlink_to('models')
    (root / 'project' / 'loop_a').symlink_to('loop_b')
    (root / 'project' / 'loop_b').symlink_to('loop_a')
    commit(root, 'Add the project', 'Ada', '2026-01-01T10:00:00+00:00')
    return root


def test_the_files_are_the_commits_own(repository: Path, tmp_path: Path) -> None:
    tree = tmp_path / 'tree'
    write_tree(repository, 'HEAD', Path('project'), tree)
    project = tree / 'project'
    # export-ignore and export-subst, which git archive honours, change nothing.
    assert (project / 'hidden.sql').read_text() == 'select 2 as id\n'
    assert (project / 'stamped.sql').read_text() == '-- $Format:%H$\nselect 3 as id\n'
    # A link to a file in the project is that file; one that leads out, to a directory, or
    # nowhere is left out. Nothing written is a link.
    assert (project / 'alias.sql').read_text() == 'select 1 as id\n'
    assert (project / 'alias_of_alias.sql').read_text() == 'select 1 as id\n'
    for name in ('escape.txt', 'absolute.txt', 'linked_models', 'loop_a', 'loop_b'):
        assert not (project / name).exists() and not (project / name).is_symlink(), name
    written = sorted(p.relative_to(tree).as_posix() for p in tree.rglob('*') if p.is_file())
    assert all(not p.is_symlink() for p in tree.rglob('*'))
    assert written == [
        'project/.gitattributes',
        'project/alias.sql',
        'project/alias_of_alias.sql',
        'project/hidden.sql',
        'project/models/orders.sql',
        'project/run.sh',
        'project/stamped.sql',
    ]
    assert not (tree / 'outside.txt').exists()


def test_the_whole_repository_and_errors(repository: Path, tmp_path: Path) -> None:
    write_tree(repository, 'HEAD', Path(), tmp_path / 'all')
    assert (tmp_path / 'all' / 'outside.txt').read_text() == 'a secret\n'
    # A link inside the repository may lead anywhere in it.
    assert (tmp_path / 'all' / 'project' / 'escape.txt').read_text() == 'a secret\n'
    with pytest.raises(DiffError, match='git cannot list'):
        write_tree(repository, 'no-such-commit', Path(), tmp_path / 'none')


def test_the_cache_is_the_users_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg'))
    assert user_cache('timeline') == tmp_path / 'xdg' / 'konstruct' / 'timeline'
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative')
    assert user_cache('timeline') == Path.home() / '.cache' / 'konstruct' / 'timeline'
    monkeypatch.setenv('CI', 'true')
    assert user_cache('timeline') is None
    mine = tmp_path / 'mine'
    mine.mkdir(mode=0o700)
    assert private(mine)
    shared = tmp_path / 'shared'
    shared.mkdir()
    shared.chmod(0o777)
    assert not private(shared)
    (tmp_path / 'link').symlink_to(mine)
    assert not private(tmp_path / 'link')
    assert not private(tmp_path / 'missing')


def test_the_cache_name_covers_what_a_build_depends_on(repository: Path, tmp_path: Path) -> None:
    first = cache_name(repository / 'project', 'HEAD')
    assert first is not None
    top, sha, prefix, name = first
    assert top == repository and prefix == Path('project') and len(sha) == 40
    assert cache_name(repository, 'HEAD')[3] != name  # type: ignore[index]
    assert cache_name(repository / 'project', 'HEAD', 'more')[3] != name  # type: ignore[index]
    other = tmp_path / 'clone'
    git(tmp_path, 'clone', '-q', str(repository), str(other))
    assert cache_name(other / 'project', 'HEAD')[3] != name  # type: ignore[index]
    assert cache_name(repository / 'project', 'no-such-ref') is None
    assert cache_name(tmp_path / 'not-a-repository', 'HEAD') is None


def _git_in(root: Path, text: str, *args: str) -> str:
    import subprocess

    found = subprocess.run(
        ['git', '-C', str(root), '-c', 'user.name=Ada', '-c', 'user.email=ada@example.com', *args],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip()


def test_names_git_could_hold_but_no_tree_should(tmp_path: Path) -> None:
    """A tree object can name a file with a backslash or hold a submodule; neither is written."""
    root = tmp_path / 'odd'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    blob = _git_in(root, 'select 1\n', 'hash-object', '-w', '--stdin')
    listing = (
        f'100644 blob {blob}\tok.sql\n100644 blob {blob}\tback\\slash.sql\n160000 commit {"1" * 40}\tsub\n'
    )
    tree = _git_in(root, listing, 'mktree')
    sha = _git_in(root, '', 'commit-tree', tree, '-m', 'Odd names')
    write_tree(root, sha, Path(), tmp_path / 'out')
    assert sorted(p.name for p in (tmp_path / 'out').iterdir()) == ['ok.sql']
    with pytest.raises(DiffError, match='not a commit'):
        write_tree(root, '--output=x', Path(), tmp_path / 'never')
    assert cache_name(root, '--all') is None


def test_the_cache_directory_on_each_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ('CI', 'GITHUB_ACTIONS', 'XDG_CACHE_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('sys.platform', 'darwin')
    assert user_cache('x') == Path.home() / 'Library' / 'Caches' / 'konstruct' / 'x'
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert user_cache('x') == tmp_path / 'konstruct' / 'x'
    from konstruct.engine.snapshot import private_directory, repository_key

    (tmp_path / 'file').write_text('')
    assert not private_directory(tmp_path / 'file' / 'below')
    assert repository_key(tmp_path) is None


def test_a_tree_written_again_for_another_commit_changes_only_what_differs(tmp_path: Path) -> None:
    root = tmp_path / 'r'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    (root / 'a.sql').write_text('select 1\n')
    (root / 'b.sql').write_text('select 2\n')
    commit(root, 'First', 'Ada', '2026-01-01T10:00:00+00:00')
    (root / 'b.sql').unlink()
    (root / 'a.sql').write_text('select 3\n')
    (root / 'c' / 'd.sql').parent.mkdir()
    (root / 'c' / 'd.sql').write_text('select 4\n')
    commit(root, 'Second', 'Ada', '2026-01-02T10:00:00+00:00')
    tree = tmp_path / 'tree'
    tree.mkdir()
    present: dict[str, bytes] = {}
    write_tree(root, 'HEAD~1', Path(), tree, present)
    assert sorted(present) == ['a.sql', 'b.sql']
    untouched = (tree / 'a.sql').stat().st_ino
    write_tree(root, 'HEAD~1', Path(), tree, present)
    assert (tree / 'a.sql').stat().st_ino == untouched  # the same blob is not written again
    write_tree(root, 'HEAD', Path(), tree, present)
    assert sorted(present) == ['a.sql', 'c/d.sql'] and not (tree / 'b.sql').exists()
    assert (tree / 'a.sql').read_text() == 'select 3\n' and (tree / 'c' / 'd.sql').read_text() == 'select 4\n'


def test_a_worktree_is_its_own_repository(repository: Path, tmp_path: Path) -> None:
    from konstruct.engine.snapshot import repository_key

    worktree = tmp_path / 'worktree'
    git(repository, 'worktree', 'add', '-q', '--detach', str(worktree))
    assert repository_key(worktree / 'project') not in (None, repository_key(repository / 'project'))
    found = cache_name(worktree / 'project', 'HEAD')
    assert found is not None and found[0] == worktree.resolve() and found[2] == Path('project')
    (tmp_path / 'odd').mkdir()
    (tmp_path / 'odd' / '.git').write_text('not a pointer\n')
    assert repository_key(tmp_path / 'odd') is None


def test_a_git_file_names_a_repository_only_where_it_names_this_one_back(
    repository: Path, tmp_path: Path
) -> None:
    """A .git file or link a repository commits cannot take another repository's place in the
    cache: git's own back-link must agree, and a link, a pipe or junk names none."""
    import os
    import subprocess

    from konstruct.engine.snapshot import repository_key

    forged = tmp_path / 'forged'
    forged.mkdir()
    (forged / '.git').write_text(f'gitdir: {repository / ".git"}\n')
    named = cache_name(forged, 'HEAD')
    assert repository_key(forged) is None and named is not None and named[3] is None
    linked = tmp_path / 'linked'
    linked.mkdir()
    (linked / '.git').symlink_to(repository / '.git', target_is_directory=True)
    assert repository_key(linked) is None
    piped = tmp_path / 'piped'
    piped.mkdir()
    os.mkfifo(piped / '.git')
    assert repository_key(piped) is None
    # A submodule's .git file is named back by core.worktree in its git directory.
    parent = tmp_path / 'parent'
    parent.mkdir()
    git(parent, 'init', '-q', '-b', 'main')
    (parent / 'README').write_text('parent\n')
    commit(parent, 'Start', 'Ada', '2026-01-01T10:00:00+00:00')
    subprocess.run(
        [
            'git',
            '-C',
            str(parent),
            '-c',
            'protocol.file.allow=always',
            'submodule',
            'add',
            '-q',
            str(repository),
            'sub',
        ],
        check=True,
        capture_output=True,
    )
    assert (parent / 'sub' / '.git').is_file()
    sub = repository_key(parent / 'sub' / 'project')
    assert sub is not None and sub not in (repository_key(parent), repository_key(repository / 'project'))
    # A bare repository has no work tree, so nothing is built from it to cache.
    bare = tmp_path / 'bare.git'
    git(tmp_path, 'clone', '-q', '--bare', str(repository), str(bare))
    assert repository_key(bare) is None


def test_the_gates_base_tree_is_the_commit_as_it_holds_it(tmp_path: Path) -> None:
    """As the gate writes the tree of its base: every regular file of the commit, whatever
    .gitattributes says, and no link that leads outside the tree."""
    (tmp_path / 'dir').mkdir()
    (tmp_path / 'dir' / 'kept.sql').write_text('select 1')
    (tmp_path / 'stamp.txt').write_text('$Format:%H$')
    (tmp_path / '.gitattributes').write_text('dir/** export-ignore\nstamp.txt export-subst\n')
    (tmp_path / 'link').symlink_to('/etc/hostname')
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / '.gitignore').write_text('konstruct-out/\n')
    commit(tmp_path, 'Start', 'Ada', '2026-01-01T10:00:00+00:00')
    tree = tmp_path / 'konstruct-out' / 'tree'
    write_tree(tmp_path, 'HEAD', Path(), tree)
    assert (tree / 'dir' / 'kept.sql').read_text() == 'select 1'
    assert (tree / 'stamp.txt').read_text() == '$Format:%H$'
    assert not (tree / 'link').exists() and not (tree / 'link').is_symlink()
    with pytest.raises(DiffError):
        write_tree(tmp_path, 'no-such-ref', Path(), tmp_path / 'konstruct-out' / 'other')


def test_a_directory_that_becomes_a_file_and_back(tmp_path: Path) -> None:
    import shutil

    root = tmp_path / 'r'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    (root / 'a').mkdir()
    (root / 'a' / 'b.sql').write_text('select 1\n')
    (root / 'gone' / 'deep').mkdir(parents=True)
    (root / 'gone' / 'deep' / 'c.sql').write_text('select 2\n')
    commit(root, 'A directory', 'Ada', '2026-01-01T10:00:00+00:00')
    shutil.rmtree(root / 'a')
    shutil.rmtree(root / 'gone')
    (root / 'a').write_text('now a file\n')
    commit(root, 'A file', 'Ada', '2026-01-02T10:00:00+00:00')
    (root / 'a').unlink()
    (root / 'a').mkdir()
    (root / 'a' / 'b.sql').write_text('select 3\n')
    commit(root, 'A directory again', 'Ada', '2026-01-03T10:00:00+00:00')
    tree = tmp_path / 'tree'
    tree.mkdir()
    present: dict[str, bytes] = {}
    write_tree(root, 'HEAD~2', Path(), tree, present)
    write_tree(root, 'HEAD~1', Path(), tree, present)
    assert (tree / 'a').read_text() == 'now a file\n' and not (tree / 'gone').exists()
    assert sorted(present) == ['a']
    write_tree(root, 'HEAD', Path(), tree, present)
    assert (tree / 'a' / 'b.sql').read_text() == 'select 3\n' and sorted(present) == ['a/b.sql']


def _gate_project(root: Path) -> None:
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (root / 'orders.sql').write_text('create table main.orders as select id, amount from raw.orders\n')
    commit(root, 'Start', 'Ada', '2026-01-01T10:00:00+00:00')


def test_the_gates_base_builds_where_git_keeps_its_directory_elsewhere(tmp_path: Path) -> None:
    """git init --separate-git-dir leaves a .git file that no git directory names back: git still
    gives the top and the commit, so the base is built, only not kept in the cache."""
    from konstruct.analysis.sensitivity import base_graph

    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main', f'--separate-git-dir={tmp_path / "elsewhere.git"}')
    assert (root / '.git').is_file()
    _gate_project(root)
    named = cache_name(root, 'HEAD')
    assert named is not None and named[0].resolve() == root.resolve() and named[3] is None
    graph = base_graph(root, 'HEAD', cache=tmp_path / 'cache')
    assert graph is not None and any(n.name == 'orders' for n in graph.relations())


def test_a_worktree_with_relative_paths_is_its_own_repository(tmp_path: Path) -> None:
    """git 2.48's worktree.useRelativePaths writes both links relative: the worktree's .git file
    relative to the worktree, and its gitdir file relative to the git directory that holds it."""
    from konstruct.analysis.sensitivity import base_graph
    from konstruct.engine.snapshot import repository_key

    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '-q', '-b', 'main')
    _gate_project(main)
    git(main, 'worktree', 'add', '-q', str(tmp_path / 'wt'), '-b', 'side')
    wt = tmp_path / 'wt'
    pointed = main / '.git' / 'worktrees' / 'wt'
    (wt / '.git').write_text('gitdir: ../main/.git/worktrees/wt\n')
    (pointed / 'gitdir').write_text('../../../../wt/.git\n')
    named, other = cache_name(wt, 'HEAD'), cache_name(main, 'HEAD')
    assert named is not None and other is not None and named[3] is not None and named[3] != other[3]
    assert repository_key(wt) not in (None, repository_key(main))
    assert base_graph(wt, 'HEAD') is not None
