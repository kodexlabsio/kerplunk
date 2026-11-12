"""Discovery's native walk and the file identities a build records, against the standard library's
own answers."""

from __future__ import annotations

import hashlib
import os
import random
import stat
from array import array
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from konstruct._core import Tree, file_digest, file_identity, files_under, folder_digest, folder_files
from konstruct.engine import config as config_module
from konstruct.inputs.dbt import SKIP_DIRS
from konstruct.semantics.definitions import FILE_NAMES

SKIP = frozenset({'.git', 'node_modules', 'target'})


def make(root: Path, names: list[str]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name.encode('utf-8', 'surrogateescape').decode('ascii', 'replace'))


def python_walk(root: Path) -> list[Path]:
    found = []
    for current, dirs, _ in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.'))
        found.append(Path(current))
    return found


def test_the_walk_lists_directories_as_os_walk_does(tmp_path: Path) -> None:
    make(
        tmp_path,
        ['b/2.sql', 'a/z/1.sql', 'a/y/.hid', '.git/x', 'node_modules/y/z', '.dot/q', 'a/target/t', 'c.sql'],
    )
    (tmp_path / 'linked').symlink_to(tmp_path / 'a', target_is_directory=True)
    tree = Tree(str(tmp_path), sorted(SKIP))
    assert [Path(d) for d in tree.directories()] == python_walk(tmp_path)


def test_files_come_in_the_order_the_glob_and_sort_gave(tmp_path: Path) -> None:
    make(tmp_path, ['b/2.sql', 'a/9.sql', 'a/1.sql', 'a/z/3.sql', 'x.sql', 'a/skip.txt', 'B.SQL'])
    (tmp_path / 'a' / 'dir.sql').mkdir()
    (tmp_path / 'a' / 'link.sql').symlink_to(tmp_path / 'x.sql')
    (tmp_path / 'a' / 'dead.sql').symlink_to(tmp_path / 'missing')
    tree = Tree(str(tmp_path), sorted(SKIP))
    expected = []
    for directory in python_walk(tmp_path):
        for path in sorted(directory.glob('*.sql')):
            if path.is_file():
                expected.append((str(path), path.is_symlink()))
    assert [(Path(n), link) for n, link in tree.files(suffixes=['.sql'])] == [
        (Path(n), link) for n, link in expected
    ]


def test_exclude_and_the_skipped_directory_are_applied(tmp_path: Path) -> None:
    make(tmp_path, ['a/x.sql', 'b/x.sql', 'b/c/x.sql', 'keep.sql'])
    tree = Tree(str(tmp_path), sorted(SKIP))
    found = tree.files(suffixes=['.sql'], exclude=['a/*', '*/c/*'], skip='b')
    assert [Path(n).relative_to(tmp_path).as_posix() for n, _ in found] == ['keep.sql']


def test_patterns_and_case_folded_suffixes(tmp_path: Path) -> None:
    make(tmp_path, ['definitions.yml', 'Meltano.YML', 'other.txt', 'd/definitions.yaml'])
    tree = Tree(str(tmp_path), sorted(SKIP))
    names = [Path(n).name for n, _ in tree.files(patterns=['definitions.*'])]
    assert names == ['definitions.yml', 'definitions.yaml']
    folded = [Path(n).name for n, _ in tree.files(suffixes=['.yml'], fold=True)]
    assert folded == ['Meltano.YML', 'definitions.yml']
    with pytest.raises(ValueError, match='not both'):
        tree.files(suffixes=['.a'], patterns=['*.a'])


def test_patterns_match_as_fnmatchcase_does(tmp_path: Path) -> None:
    """The matcher against the standard library's over random patterns, on a folder of random names."""
    rng = random.Random(7)
    alphabet = 'abAB.-_'
    names = {''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))) for _ in range(400)} - {
        '.',
        '..',
    }
    make(tmp_path, sorted(names))
    tree = Tree(str(tmp_path), [])
    pieces = ['*', '?', '[ab]', '[!a]', '[a-b]', '[', ']', '[]a]', '[a-]']
    for _ in range(300):
        pattern = ''.join(
            rng.choice(pieces) if rng.random() < 0.4 else rng.choice(alphabet)
            for _ in range(rng.randint(1, 6))
        )
        expected = sorted(n for n in names if fnmatchcase(n, pattern))
        got = sorted(Path(n).name for n, _ in tree.files(patterns=[pattern]))
        assert got == expected, pattern


def identity_by_python(path: Path) -> list[int] | None:
    found = path.stat()
    if not stat.S_ISREG(found.st_mode):
        return None
    return [found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_dev, found.st_ino]


def test_identity_is_the_stat_fields_of_a_regular_file(tmp_path: Path) -> None:
    make(tmp_path, ['a.sql'])
    (tmp_path / 'link').symlink_to(tmp_path / 'a.sql')
    assert file_identity(str(tmp_path / 'a.sql')) == identity_by_python(tmp_path / 'a.sql')
    assert file_identity(str(tmp_path / 'link')) == identity_by_python(tmp_path / 'a.sql')
    assert file_identity(str(tmp_path)) is None
    assert file_identity(str(tmp_path / 'missing')) is None


def test_digest_is_blake2b_of_the_decimal_fields() -> None:
    found = [12, 1_700_000_000_123_456_789, -5, 64768, 2**63 + 5]
    expected = hashlib.blake2b(' '.join(map(str, found)).encode(), digest_size=8).hexdigest()
    assert file_digest(found) == expected


def python_listing(folder: Path, extension: str) -> tuple[str, list[str]]:
    """The listing digest as the Python it replaced made it."""
    bits = (1 << 64) - 1
    names: list[str] = []
    fields = array('Q')
    with os.scandir(folder) as entries:
        for item in entries:
            if not item.name.endswith(extension):
                continue
            try:
                found = item.stat()
            except OSError:
                continue
            if stat.S_ISREG(found.st_mode):
                names.append(item.name)
                fields.extend(
                    (
                        found.st_size,
                        found.st_mtime_ns & bits,
                        found.st_ctime_ns & bits,
                        found.st_dev,
                        found.st_ino & bits,
                    )
                )
    hashed = hashlib.blake2b('\0'.join(names).encode('utf-8', 'surrogateescape'), digest_size=8)
    hashed.update(fields)
    return hashed.hexdigest(), names


def test_a_listing_is_the_digest_the_python_gave(tmp_path: Path) -> None:
    make(tmp_path, [f'f{n}.sql' for n in range(30)] + ['x.txt', 'sub/y.sql'])
    (tmp_path / 'link.sql').symlink_to(tmp_path / 'f1.sql')
    (tmp_path / 'dead.sql').symlink_to(tmp_path / 'missing')
    whole, entries = folder_files(str(tmp_path), '.sql')
    expected, names = python_listing(tmp_path, '.sql')
    assert whole == expected == folder_digest(str(tmp_path), '.sql')
    assert [name for name, _, _ in entries] == names
    for name, found, hashed in entries:
        assert found == identity_by_python(tmp_path / name)
        assert hashed == file_digest(found)
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert folder_digest(str(empty), '.sql') == hashlib.blake2b(digest_size=8).hexdigest()
    with pytest.raises(OSError):
        folder_files(str(tmp_path / 'missing'), '.sql')


def test_files_under_takes_every_depth_and_hidden_files(tmp_path: Path) -> None:
    make(tmp_path, ['a.sql', 'x/b.sql', 'x/y/.c'])
    (tmp_path / 'dirlink').symlink_to(tmp_path / 'x', target_is_directory=True)
    got = dict(files_under(str(tmp_path)))
    assert set(got) == {'a.sql', str(Path('x', 'b.sql')), str(Path('x', 'y', '.c'))}
    assert got['a.sql'] == identity_by_python(tmp_path / 'a.sql')


def test_files_under_a_directory_whose_name_is_not_text_are_found(tmp_path: Path) -> None:
    odd = os.fsdecode(b'd\xff')
    odd_file = os.fsdecode(b'\xfe')
    make(tmp_path, [f'{odd}/x.sql', f'{odd}/inner/y.sql', f'plain/{odd_file}.sql', 'ok.sql'])
    tree = Tree(str(tmp_path), sorted(SKIP))
    expected: list[str] = []
    for directory in python_walk(tmp_path):
        expected.extend(str(p) for p in sorted(directory.glob('*.sql')) if p.is_file())
    found = [n for n, _ in tree.files(suffixes=['.sql'])]
    assert found == expected
    assert len(found) == 4
    assert [Path(d) for d in tree.directories()] == python_walk(tmp_path)


def test_one_rule_for_names_in_a_walk_and_on_a_file_alone() -> None:
    from konstruct._core import name_accepts
    from konstruct.engine import config

    assert name_accepts('Meltano.YML', suffixes=['.yml'], fold=True)
    assert not name_accepts('Meltano.YML', suffixes=['.yml'])
    assert name_accepts('dags.yml', patterns=['dags.*'])
    assert not name_accepts('Dags.yml', patterns=['dags.*'])
    with pytest.raises(ValueError, match='not both'):
        name_accepts('a', suffixes=['.a'], patterns=['*.a'])
    for name in ['definitions.yml', 'definitions.yaml', 'x.definitions.yml', 'a.ossie.yaml', 'b.ossie.yml']:
        assert config.FINDS['definitions'](Path(name), name, ()) == (name in FILE_NAMES)
        assert config.FINDS['ossie'](Path(name), name, ()) == name.endswith(('.ossie.yaml', '.ossie.yml'))
    assert config.FINDS['names'](Path('x/a.lkml'), 'x/a.lkml', ('*.lkml',))


def configured(root: Path, table: str, key: str, directory: str) -> config_module.Config:
    (root / 'konstruct.toml').write_text(f'[{table}]\n{key} = ["{directory}"]\n')
    return config_module.load_config(root)


def test_a_configured_directory_is_walked_as_rglob_walked_it(tmp_path: Path) -> None:
    layout = [
        'bi/z.tmdl',
        'bi/a.tmdl',
        'bi/m/c.tmdl',
        'bi/m/n/d.tmdl',
        'bi/.hid/h.tmdl',
        'bi/node_modules/n.tmdl',
        'bi/target/t.tmdl',
        'bi/definitions.yml',
        'bi/sub/definitions.yaml',
        'bi/.hid/definitions.yml',
        'bi/node_modules/definitions.yml',
        'bi/x.ossie.yaml',
        'bi/.hid/y.ossie.yml',
        'bi/node_modules/z.ossie.yml',
        'bi/q.lkml',
        'bi/.hid/r.lkml',
        'bi/node_modules/s.lkml',
    ]
    make(tmp_path, layout)
    bi = tmp_path / 'bi'
    skip = SKIP_DIRS

    def kept(paths: Iterable[Path]) -> list[Path]:
        return sorted(p for p in paths if not set(p.parts) & skip)

    cfg = configured(tmp_path, 'powerbi', 'paths', 'bi')
    assert config_module.discover_powerbi_files(cfg) == kept(bi.rglob('*.tmdl'))
    assert [p.name for p in config_module.discover_powerbi_files(cfg)] == [
        'h.tmdl',
        'a.tmdl',
        'c.tmdl',
        'd.tmdl',
        'z.tmdl',
    ]
    cfg = configured(tmp_path, 'definitions', 'paths', 'bi')
    assert config_module.discover_definitions_files(cfg) == sorted(
        p for name in FILE_NAMES for p in bi.rglob(name)
    )
    cfg = configured(tmp_path, 'ossie', 'paths', 'bi')
    assert config_module.discover_ossie_files(cfg) == sorted(
        p for p in bi.rglob('*') if p.is_file() and p.name.endswith(('.ossie.yaml', '.ossie.yml'))
    )
    cfg = configured(tmp_path, 'looker', 'paths', 'bi')
    assert config_module.discover_looker_files(cfg) == kept(bi.rglob('*.lkml'))


def test_a_reversed_range_matches_nothing_where_fnmatch_merges_chunks() -> None:
    """The one place the matcher differs from ``fnmatch``: a range written backwards (``[z-a]``)
    followed by more of the set (``[z-a!]``, ``[z-a![]``). ``fnmatch`` joins the chunks around the
    bad range in a way that turns the set into something else (``[z-a!]`` then matches ``b``);
    here the range matches nothing and the rest of the set means what it says. No pattern a person
    writes for a file name means the former, so the difference is left as it is."""
    from konstruct._core import name_accepts

    assert not name_accepts('b', patterns=['[z-a!]'])
    assert name_accepts('!', patterns=['[z-a!]'])
    assert name_accepts('[', patterns=['[z-a![]'])
    assert not name_accepts('z', patterns=['[z-a![]'])
