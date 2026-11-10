"""Trust in a project's own connections: an unreviewed checkout connects nowhere it names, reads
no environment variable for it and writes nowhere outside itself until `konstruct trust`."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.engine import config as config_module
from konstruct.engine import trust
from konstruct.engine.build import BuildResult, build
from konstruct.engine.config import (
    PATH_SETTINGS,
    Config,
    ConfigError,
    UntrustedConfig,
    WarehouseConfig,
    load_config,
)
from konstruct.inputs.dbt import read_project
from konstruct.interfaces.cli import main
from konstruct.interfaces.project import Project
from konstruct.interfaces.status import inspect

SECRET = 'not-a-real-secret-4f1c'


class Listener:
    """A plain TCP socket on 127.0.0.1 that records every connection and what it was sent."""

    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(8)
        self.socket.settimeout(0.2)
        self.port: int = self.socket.getsockname()[1]
        self.received: list[bytes] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                data = b''
                try:
                    while chunk := connection.recv(4096):
                        data += chunk
                        # Decline TLS and GSS encryption, so the client sends its startup in clear.
                        if data[-4:] in (b'\x04\xd2\x16/', b'\x04\xd2\x160'):
                            connection.sendall(b'N')
                        elif len(data) > 8:
                            break
                except OSError:
                    pass
                self.received.append(data)

    def close(self) -> None:
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=5)

    @property
    def leaked(self) -> bool:
        return any(SECRET.encode() in data for data in self.received)


@pytest.fixture
def listener() -> Iterator[Listener]:
    server = Listener()
    yield server
    server.close()


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv('FAKE_SECRET', SECRET)
    return SECRET


def postgres_project(root: Path, port: int) -> Path:
    """A repository whose konstruct.toml sends an environment variable to a host it chooses."""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "postgres"\n'
        f'dsn = "postgresql://${{FAKE_SECRET}}:x@127.0.0.1:{port}/db?sslmode=disable&gssencmode=disable'
        '&connect_timeout=2"\n'
    )
    (root / 'report.sql').write_text('create table marts.big as select id from raw.orders')
    return root


def dbt_project(root: Path, profile: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'dbt_project.yml').write_text("name: shop\nprofile: shop\nmodel-paths: ['models']\n")
    (root / 'models').mkdir(exist_ok=True)
    (root / 'models' / 'orders.sql').write_text('select 1 as id\n')
    (root / 'profiles.yml').write_text(profile)
    return root


def files_only(root: Path) -> BuildResult:
    """Build without running dbt, which would wait on the listener as a real server."""
    config = load_config(root)
    config.override(run_dbt=False)
    return build(config)


def postgres_profile(port: int) -> str:
    return (
        'shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n      host: 127.0.0.1\n'
        f'      port: {port}\n      user: "{{{{ env_var(\'FAKE_SECRET\') }}}}"\n      password: x\n'
        '      dbname: db\n      schema: public\n      sslmode: disable\n'
    )


# Connections


def test_an_untrusted_project_connects_nowhere(tmp_path: Path, listener: Listener, secret: str) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    result = build(load_config(root))
    assert listener.received == []
    warning = next(w for w in result.graph.warnings if trust.WARNING in w)
    assert 'postgres (konstruct.toml)' in warning
    skipped = next(s for s in result.graph.meta['sources'] if s.get('status') == 'skipped')
    assert skipped['input'] == 'postgres'
    assert result.graph.node_for_relation('marts.big') is not None  # static analysis still runs


def test_untrusted_settings_never_read_the_environment(
    tmp_path: Path, listener: Listener, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    dbt_project(root / 'transform', postgres_profile(listener.port))

    def refuse(value: Any) -> Any:
        raise AssertionError('expand_env was called for untrusted content')

    monkeypatch.setattr(config_module, 'expand_env', refuse)
    read: list[str] = []
    environ = os.environ.get

    def recording(key: str, default: str | None = None) -> str | None:
        read.append(key)
        return environ(key, default)

    monkeypatch.setattr(os.environ, 'get', recording)
    files_only(root)
    assert 'FAKE_SECRET' not in read
    assert listener.received == []


def test_an_untrusted_profile_never_reads_the_environment(
    tmp_path: Path, listener: Listener, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No konstruct.toml, so the warehouse comes from the repository's own profiles.yml.
    root = dbt_project(tmp_path / 'repo', postgres_profile(listener.port))
    read: list[str] = []
    environ = os.environ.get

    def recording(key: str, default: str | None = None) -> str | None:
        read.append(key)
        return environ(key, default)

    monkeypatch.setattr(os.environ, 'get', recording)
    files_only(root)
    assert 'FAKE_SECRET' not in read
    assert listener.received == []


def test_a_trusted_project_connects(tmp_path: Path, listener: Listener, secret: str, capsys: Any) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    assert main(['trust', str(root)]) == 0
    shown = capsys.readouterr().out
    assert 'trusted' in shown and 'konstruct.toml' in shown and 'FAKE_SECRET' in shown
    result = build(load_config(root))
    assert listener.leaked
    assert not any(trust.WARNING in w for w in result.graph.warnings)


def test_a_change_after_trust_withdraws_it(tmp_path: Path, listener: Listener, secret: str) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    Project(root).trust()
    toml = root / 'konstruct.toml'
    toml.write_text(toml.read_text() + 'schemas = ["raw"]\n')
    result = build(load_config(root))
    assert listener.received == []
    assert any('changed after `konstruct trust`' in w for w in result.graph.warnings)
    assert trust.check(root).state == 'changed'


def test_a_changed_profile_withdraws_trust(tmp_path: Path, listener: Listener, secret: str) -> None:
    root = dbt_project(tmp_path / 'repo', 'shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n')
    Project(root).trust()
    (root / 'profiles.yml').write_text(postgres_profile(listener.port))
    files_only(root)
    assert listener.received == []
    Project(root).trust()
    files_only(root)
    assert listener.leaked


def test_trust_by_environment(
    tmp_path: Path, listener: Listener, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    assert trust.check(root).state == 'environment'
    build(load_config(root))
    assert listener.leaked


def test_trust_config_option_lasts_for_the_command(tmp_path: Path, listener: Listener, secret: str) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    assert main(['build', str(root), '--trust-config', '-q']) == 0
    assert listener.leaked
    assert 'KONSTRUCT_TRUST_CONFIG' not in os.environ


def test_the_users_own_profiles_stay_trusted(
    tmp_path: Path, listener: Listener, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = dbt_project(tmp_path / 'repo', 'other:\n  target: dev\n')
    (root / 'profiles.yml').unlink()
    profiles = tmp_path / 'home-dbt'
    profiles.mkdir()
    (profiles / 'profiles.yml').write_text(postgres_profile(listener.port))
    monkeypatch.setenv('DBT_PROFILES_DIR', str(profiles))
    project = read_project(root)
    assert project is not None
    [warehouse] = config_module.discover_warehouses(load_config(root), [project])
    assert warehouse.trusted
    files_only(root)
    assert listener.leaked


def test_a_profiles_dir_inside_the_project_is_the_repositorys(
    tmp_path: Path, listener: Listener, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = dbt_project(tmp_path / 'repo', 'other:\n  target: dev\n')
    (root / 'ci').mkdir()
    (root / 'ci' / 'profiles.yml').write_text(postgres_profile(listener.port))
    monkeypatch.setenv('DBT_PROFILES_DIR', str(root / 'ci'))
    files_only(root)
    assert listener.received == []
    assert trust.check(root).files == ['ci/profiles.yml', 'profiles.yml']


def test_the_repositorys_profile_is_untrusted(tmp_path: Path, listener: Listener, secret: str) -> None:
    root = dbt_project(tmp_path / 'repo', postgres_profile(listener.port))
    result = files_only(root)
    assert listener.received == []
    assert any('postgres (dbt profile shop)' in w for w in result.graph.warnings)


# Paths


def test_out_dir_outside_the_project_needs_trust(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('out_dir = "../elsewhere"\n')
    with pytest.raises(UntrustedConfig, match=r"out_dir = '../elsewhere' in konstruct.toml is outside"):
        load_config(root)
    assert load_config(root, tmp_path / 'given').out_dir == tmp_path / 'given'
    (root / 'konstruct.toml').write_text('out_dir = "build/graph"\n')
    assert load_config(root).out_dir == root / 'build' / 'graph'
    (root / 'konstruct.toml').write_text('out_dir = "../elsewhere"\n')
    Project(root).trust()
    assert load_config(root).out_dir == tmp_path / 'elsewhere'


def test_repositories_outside_the_project_need_trust(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    (tmp_path / 'other').mkdir()
    (root / 'inner').mkdir(parents=True)
    (root / 'konstruct.toml').write_text('repositories = ["inner", "../other"]\n')
    with pytest.raises(UntrustedConfig, match=r"repositories: '../other' in konstruct.toml is outside"):
        load_config(root)
    rows = {key: (value, state) for key, value, state in inspect(root).rows()}
    assert rows['trust'][0].startswith("not trusted: repositories: '../other' in konstruct.toml")
    assert rows['trust'][1] == 'attention' and list(rows) == ['config', 'trust']
    assert main(['-C', str(root), 'status']) == 0
    Project(root).trust()
    assert load_config(root).repositories == [root / 'inner', tmp_path / 'other']


def test_a_listed_repositorys_paths_are_reported_with_its_name(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    (root / 'inner').mkdir(parents=True)
    (root / 'inner' / 'konstruct.toml').write_text('[sql]\npaths = ["../../elsewhere/*.sql"]\n')
    (root / 'konstruct.toml').write_text('repositories = ["inner"]\n')
    with pytest.raises(
        UntrustedConfig, match=r"^repositories: inner: \[sql\] paths: '../../elsewhere/\*.sql'"
    ):
        files_only(root)


def test_trust_says_what_it_did(tmp_path: Path) -> None:
    root = postgres_project(tmp_path / 'repo', 5432)
    assert str(Project(root).trust()).splitlines()[1] == 'Trusted: the record is saved'
    assert str(Project(root).trust()).splitlines()[1] == 'Already trusted: the record is unchanged'
    assert str(Project(root).trust(revoke=True)).splitlines()[1] == 'Trust revoked: the record is removed'
    assert (
        str(Project(root).trust(revoke=True)).splitlines()[1] == 'Not trusted: there was no record to remove'
    )
    assert str(trust.check(root)).splitlines()[1].startswith('Not trusted; review it and run')
    Project(root).trust()
    (root / 'konstruct.toml').write_text((root / 'konstruct.toml').read_text() + 'schemas = ["raw"]\n')
    assert '`konstruct trust`' in dict(inspect(root).lines())['trust']
    assert 'run `konstruct trust`' in str(trust.check(root)).splitlines()[1]


def path_setting(table: str | None, key: str, value: str) -> str:
    if table is None:
        return f'{key} = "{value}"\n' if key == 'out_dir' else f'{key} = ["{value}"]\n'
    return f'[{table}]\n{key} = ["{value}"]\n'


@pytest.mark.parametrize(('table', 'key', 'field'), PATH_SETTINGS, ids=lambda v: str(v))
def test_every_path_setting_stays_inside_an_untrusted_project(
    tmp_path: Path, table: str | None, key: str, field: str
) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    victim = tmp_path / 'victim'
    victim.mkdir()
    (victim / 'secret.sql').write_text('create table leaked as select 1 as secret')
    outside = '../victim/*.sql' if table == 'sql' else '../victim'
    (root / 'konstruct.toml').write_text(path_setting(table, key, outside))
    with pytest.raises(
        UntrustedConfig, match=re.escape(f"'{outside}' in konstruct.toml is outside the project")
    ):
        load_config(root)
    assert main(['-C', str(root), 'build', '-q', '--no-dbt']) == 1
    assert not (root / 'konstruct-out' / 'graph.json').exists()
    (root / 'konstruct.toml').write_text(
        path_setting(table, key, 'inside/*.sql' if table == 'sql' else 'inside')
    )
    assert not load_config(root).trusted  # a path inside the project needs no trust
    (root / 'konstruct.toml').write_text(path_setting(table, key, outside))
    Project(root).trust()
    assert load_config(root).trusted


def test_every_path_setting_is_in_the_table() -> None:
    fields = {
        f for f in Config.__dataclass_fields__ if f.endswith('_paths') or f in ('dbt_projects', 'out_dir')
    }
    assert fields | {'repositories'} == {field for _, _, field in PATH_SETTINGS}


@pytest.mark.parametrize(
    'pattern', ['{victim}/*.sql', 'sql/*/../../../victim/*.sql', 'linked/*.sql', 'linked/**/*.sql']
)
def test_a_sql_glob_that_leads_outside_needs_trust(tmp_path: Path, pattern: str) -> None:
    root = tmp_path / 'repo'
    (root / 'sql' / 'a').mkdir(parents=True)
    victim = tmp_path / 'victim'
    victim.mkdir()
    (root / 'linked').symlink_to(victim)
    (root / 'konstruct.toml').write_text(f'[sql]\npaths = ["{pattern.format(victim=victim)}"]\n')
    with pytest.raises(UntrustedConfig, match=r'\[sql\] paths: .* is outside the project'):
        load_config(root)
    (root / 'konstruct.toml').write_text('[sql]\npaths = ["sql/**/*.sql", "*.sql"]\n')
    assert load_config(root).sql_paths == ['sql/**/*.sql', '*.sql']


def test_trust_covers_where_a_path_setting_leads(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    for name in ('victim', 'other'):
        (tmp_path / name).mkdir()
    (root / 'linked').symlink_to(tmp_path / 'victim')
    (root / 'konstruct.toml').write_text('[sql]\npaths = ["linked/*.sql"]\n')
    assert Project(root).trust().files == ['konstruct.toml']
    (root / 'linked').unlink()
    (root / 'linked').symlink_to(tmp_path / 'other')
    assert trust.check(root).state == 'changed'
    with pytest.raises(UntrustedConfig):
        load_config(root)
    # Paths inside the project are not part of the record, so they can change freely.
    (root / 'konstruct.toml').write_text('[sql]\npaths = ["a/*.sql"]\n')
    assert Project(root).trust().files == []
    (root / 'konstruct.toml').write_text('[sql]\npaths = ["b/*.sql"]\n[definitions]\npaths = ["terms"]\n')
    assert trust.check(root).trusted


def test_a_listed_repositorys_connections_are_in_the_fingerprint(
    tmp_path: Path, listener: Listener, secret: str
) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('repositories = ["../other"]\n')
    other = postgres_project(tmp_path / 'other', listener.port)
    shown = Project(root).trust()
    assert shown.files == ['konstruct.toml', str(other / 'konstruct.toml')] and shown.variables == [
        'FAKE_SECRET'
    ]
    build(load_config(root))
    assert listener.leaked
    (other / 'konstruct.toml').write_text((other / 'konstruct.toml').read_text() + 'schemas = ["x"]\n')
    with pytest.raises(UntrustedConfig):
        load_config(root)


def test_duckdb_paths_stay_inside_an_untrusted_project(tmp_path: Path) -> None:
    import duckdb

    outside = tmp_path / 'outside.duckdb'
    duckdb.connect(str(outside)).execute('create table t (a int)').close()
    root = tmp_path / 'repo'
    root.mkdir()
    duckdb.connect(str(root / 'inside.duckdb')).execute('create table u (b int)').close()
    (root / 'konstruct.toml').write_text(
        f'[[warehouse]]\ntype = "duckdb"\npath = "{outside}"\n\n'
        '[[warehouse]]\ntype = "duckdb"\npath = "inside.duckdb"\n\n'
        '[[warehouse]]\ntype = "duckdb"\npath = "${HOME}/x.duckdb"\n'
    )
    graph = build(load_config(root)).graph
    assert any(n.name == 'u' for n in graph.relations())
    assert not any(n.name == 't' for n in graph.relations())
    warning = next(w for w in graph.warnings if trust.WARNING in w)
    assert f'duckdb {outside}' in warning and 'x.duckdb' in warning
    Project(root).trust()
    assert any(n.name == 't' for n in build(load_config(root)).graph.relations())


def test_a_linked_duckdb_file_outside_the_project_is_refused(tmp_path: Path) -> None:
    import duckdb

    outside = tmp_path / 'outside.duckdb'
    duckdb.connect(str(outside)).execute('create table t (a int)').close()
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'data.duckdb').symlink_to(outside)
    graph = build(load_config(root)).graph
    assert not any(n.name == 't' for n in graph.relations())


def test_a_duckdb_file_found_after_trust_opens_only_inside_the_project(tmp_path: Path) -> None:
    import duckdb

    outside = tmp_path / 'outside.duckdb'
    duckdb.connect(str(outside)).execute('create table t (a int)').close()
    root = tmp_path / 'repo'
    root.mkdir()
    duckdb.connect(str(root / 'inside.duckdb')).execute('create table u (b int)').close()
    assert Project(root).trust().trusted
    (root / 'later.duckdb').symlink_to(outside)  # added after `konstruct trust`
    assert trust.check(root).trusted
    graph = build(load_config(root)).graph
    assert any(n.name == 'u' for n in graph.relations())
    assert not any(n.name == 't' for n in graph.relations())
    assert any('later.duckdb leads outside the project' in w for w in graph.warnings)
    assert not any(trust.WARNING in w for w in graph.warnings)
    [skipped] = [s for s in graph.meta['sources'] if s.get('status') == 'skipped']
    assert 'name it under [[warehouse]]' in skipped['note']
    # Named in the trusted configuration, the same link opens, and trust covers where it leads.
    (root / 'konstruct.toml').write_text('[[warehouse]]\ntype = "duckdb"\npath = "later.duckdb"\n')
    Project(root).trust()
    assert any(n.name == 't' for n in build(load_config(root)).graph.relations())
    (root / 'later.duckdb').unlink()
    (root / 'later.duckdb').symlink_to(root / 'inside.duckdb')
    assert trust.check(root).state == 'changed'


def test_untrusted_connector_messages(tmp_path: Path) -> None:
    with pytest.raises(UntrustedConfig, match=r'postgres connection in konstruct\.toml is not trusted'):
        WarehouseConfig('postgres', {'dsn': 'x'}, trusted=False).connector(tmp_path)
    with pytest.raises(UntrustedConfig, match='reads its path from the environment'):
        WarehouseConfig('duckdb', {'path': '{{ env_var("X") }}'}, trusted=False).connector(tmp_path)
    assert WarehouseConfig('duckdb', {'path': 'a.duckdb'}, trusted=False).connector(tmp_path) is not None


# dbt


LOCAL_DUCKDB = "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: 'shop.duckdb'\n"


def fake_dbt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `dbt` on PATH that records the environment it was started with, then fails."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'dbt-ran'
    fake = bin_dir / 'dbt'
    fake.write_text(f'#!/bin/sh\nenv > {marker}\nexit 1\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    return marker


@pytest.mark.parametrize('profile', ['local duckdb', 'postgres', 'users own'])
def test_dbt_never_runs_for_an_untrusted_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    marker = fake_dbt(tmp_path, monkeypatch)
    root = dbt_project(tmp_path / 'repo', postgres_profile(5432) if profile == 'postgres' else LOCAL_DUCKDB)
    if profile == 'users own':
        (root / 'profiles.yml').unlink()
        home = tmp_path / 'home-dbt'
        home.mkdir()
        (home / 'profiles.yml').write_text(LOCAL_DUCKDB)
        monkeypatch.setenv('DBT_PROFILES_DIR', str(home))
    started: list[Any] = []
    run = subprocess.run

    def recording(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recording)
    graph = build(load_config(root)).graph
    assert started == [] and not marker.exists()
    assert any('running dbt in shop' in w for w in graph.warnings)
    assert any(
        'target/manifest.json is missing and dbt is not run until the project is trusted' in w
        for w in graph.warnings
    )
    [source] = [s for s in graph.meta['sources'] if s['input'] == 'dbt']
    assert source['note'] == 'no manifest; dbt is not run until the project is trusted'
    Project(root).trust()
    build(load_config(root))
    assert started and marker.exists()
    environment = marker.read_text()
    assert 'DBT_SEND_ANONYMOUS_USAGE_STATS=false' in environment and 'DO_NOT_TRACK=1' in environment


def test_an_untrusted_build_reads_the_manifest_already_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = fake_dbt(tmp_path, monkeypatch)
    root = dbt_project(tmp_path / 'repo', LOCAL_DUCKDB)
    (root / 'target').mkdir()
    manifest = root / 'target' / 'manifest.json'
    manifest.write_text('{"metadata": {"adapter_type": "duckdb"}, "nodes": {}}')
    os.utime(manifest, (1, 1))  # older than the models, so dbt would otherwise run
    graph = build(load_config(root)).graph
    assert not marker.exists()
    [source] = [s for s in graph.meta['sources'] if s['input'] == 'dbt']
    assert source['note'] == 'manifest is stale; dbt is not run until the project is trusted'
    assert 'status' not in source
    assert dict(inspect(root).lines())['trust'].startswith('not trusted: profiles.yml, dbt in shop;')
    assert trust.check(root).dbt_projects == ['shop'] and 'in shop, once trusted' in str(trust.check(root))


def test_running_dbt_can_still_be_turned_off_once_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = fake_dbt(tmp_path, monkeypatch)
    root = dbt_project(tmp_path / 'repo', LOCAL_DUCKDB)
    Project(root).trust()
    graph = files_only(root).graph
    assert not marker.exists()
    assert not any(trust.WARNING in w for w in graph.warnings)
    [source] = [s for s in graph.meta['sources'] if s['input'] == 'dbt']
    assert source['note'] == 'no manifest; running dbt is disabled'


# Files that lead outside the project


def victim(tmp_path: Path) -> Path:
    outside = tmp_path / 'victim'
    outside.mkdir()
    (outside / 'secret.sql').write_text('create table leaked as select 1 as secret')
    (outside / 'dag.py').write_text('from airflow.sdk import DAG\nwith DAG("leaked_dag"):\n    pass\n')
    return outside


def test_discovered_files_that_link_outside_are_not_read_until_trusted(tmp_path: Path) -> None:
    outside = victim(tmp_path)
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'report.sql').write_text('create table kept as select 1 as id')
    (root / 'leak.sql').symlink_to(outside / 'secret.sql')
    (root / 'leak.py').symlink_to(outside / 'dag.py')  # found by the DAG, Dagster and Spark discovery
    graph = files_only(root).graph
    assert graph.node_for_relation('kept') is not None
    assert graph.node_for_relation('leaked') is None
    assert not any('leaked_dag' in n.id for n in graph.nodes.values())
    [warning] = [w for w in graph.warnings if 'lead outside the project' in w]
    assert warning.startswith('.: did not read leak.py, leak.sql, which lead outside the project')
    Project(root).trust()
    graph = files_only(root).graph
    assert graph.node_for_relation('leaked') is not None
    assert not any('lead outside the project' in w for w in graph.warnings)


def test_configured_directories_skip_links_that_lead_outside(tmp_path: Path) -> None:
    outside = victim(tmp_path)
    root = tmp_path / 'repo'
    (root / 'sql' / 'real').mkdir(parents=True)
    (root / 'sql' / 'real' / 'a.sql').write_text('create table kept as select 1 as id')
    (root / 'sql' / 'linked').symlink_to(outside)
    (root / 'sql' / 'real' / 'b.sql').symlink_to(outside / 'secret.sql')
    (root / 'konstruct.toml').write_text('[sql]\npaths = ["sql/*/*.sql"]\n')
    graph = files_only(root).graph
    assert graph.node_for_relation('kept') is not None and graph.node_for_relation('leaked') is None
    shown = sorted(w.split(':')[0] for w in graph.warnings if 'leads outside the project' in w)
    assert shown == ['sql', 'sql/real']  # one warning for each directory that holds a link


def test_an_airflow_template_outside_the_project_is_not_read(tmp_path: Path) -> None:
    outside = victim(tmp_path)
    root = tmp_path / 'repo'
    (root / 'dags').mkdir(parents=True)
    (root / 'dags' / 'etl.py').write_text(
        'from airflow.sdk import DAG\n'
        'from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator\n'
        'with DAG("etl"):\n'
        f'    SQLExecuteQueryOperator(task_id="q", sql="{outside}/secret.sql")\n'
    )
    graph = files_only(root).graph
    task = graph.nodes['task.etl.q']
    assert 'secret' not in json.dumps(task.attrs)
    assert graph.node_for_relation('leaked') is None
    assert any('did not read secret.sql, which leads outside the project' in w for w in graph.warnings)


def test_a_dbt_manifest_that_links_outside_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / 'victim'
    outside.mkdir()
    (outside / 'manifest.json').write_text('{"nodes": {}}')
    root = dbt_project(tmp_path / 'repo', LOCAL_DUCKDB)
    (root / 'target').mkdir()
    (root / 'target' / 'manifest.json').symlink_to(outside / 'manifest.json')
    graph = files_only(root).graph
    [source] = [s for s in graph.meta['sources'] if s['input'] == 'dbt']
    assert source['status'] == 'failed'
    assert any(w.startswith('target: did not read manifest.json') for w in graph.warnings)


def test_a_macro_that_links_outside_does_not_render_a_model(tmp_path: Path) -> None:
    outside = tmp_path / 'victim'
    outside.mkdir()
    (outside / 'source.sql').write_text('{% macro source_table() %}leaked.secrets{% endmacro %}')
    root = dbt_project(tmp_path / 'repo', LOCAL_DUCKDB)
    (root / 'macros').mkdir()
    (root / 'macros' / 'own.sql').write_text('{% macro own_column() %}id{% endmacro %}')
    (root / 'macros' / 'source.sql').symlink_to(outside / 'source.sql')
    model = {
        'unique_id': 'model.shop.orders',
        'resource_type': 'model',
        'name': 'orders',
        'alias': 'orders',
        'package_name': 'shop',
        'database': 'db',
        'schema': 'main',
        'relation_name': '"db"."main"."orders"',
        'original_file_path': 'models/orders.sql',
        'fqn': ['shop', 'orders'],
        'raw_code': 'select {{ own_column() }} from {{ source_table() }}',  # never compiled
        'language': 'sql',
        'depends_on': {'nodes': [], 'macros': []},
        'config': {'materialized': 'view'},
        'columns': {},
    }
    manifest = {'metadata': {'adapter_type': 'duckdb'}, 'nodes': {model['unique_id']: model}}
    (root / 'target').mkdir()
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))

    def built(graph: Any) -> str:
        return json.dumps(graph.to_dict())

    graph = files_only(root).graph
    assert graph.node_for_relation('db.main.orders') is not None
    assert 'leaked' not in built(graph)
    assert any(w.startswith('macros: did not read source.sql, which leads outside') for w in graph.warnings)
    Project(root).trust()
    assert 'leaked' in built(files_only(root).graph)  # the same link is read once trusted


def test_a_default_out_dir_that_links_outside_needs_trust(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    (tmp_path / 'elsewhere').mkdir()
    (root / 'konstruct-out').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(UntrustedConfig, match=r'konstruct-out is a symbolic link to .*, outside the project'):
        load_config(root)
    with pytest.raises(UntrustedConfig):
        Project(root).build(run_dbt=False)
    assert main(['-C', str(root), 'build', '-q']) == 1
    assert list((tmp_path / 'elsewhere').iterdir()) == []
    assert load_config(root, tmp_path / 'given').out_dir == tmp_path / 'given'
    Project(root).trust()
    assert load_config(root).out_dir == tmp_path / 'elsewhere'


def test_outputs_replace_links_in_the_output_directory(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    (root / 'konstruct-out' / 'repositories').mkdir(parents=True)
    (root / 'report.sql').write_text('create table kept as select 1 as id')
    (root / 'inner').mkdir()
    (root / 'inner' / 'other.sql').write_text('create table other as select 1 as id')
    (root / 'konstruct.toml').write_text('repositories = ["inner"]\n')
    outside = tmp_path / 'victim'
    outside.mkdir()
    names = ('graph.json', 'REPORT.md', 'graph.html', 'ontology.yml', 'cache.json')
    for name in names:
        (outside / name).write_text('keep me')
        (root / 'konstruct-out' / name).symlink_to(outside / name)
    (outside / 'repos').mkdir()
    (root / 'konstruct-out' / 'repositories' / 'inner').symlink_to(outside / 'repos')
    Project(root).build(run_dbt=False, update=True)
    assert all((outside / name).read_text() == 'keep me' for name in names)
    assert list((outside / 'repos').iterdir()) == []
    for name in ('graph.json', 'REPORT.md'):
        path = root / 'konstruct-out' / name
        assert not path.is_symlink() and path.stat().st_size > len('keep me')
    assert not any(p.name.endswith('.tmp') for p in (root / 'konstruct-out').iterdir())


def test_verify_uses_only_a_postgres_warehouse_the_build_read(tmp_path: Path) -> None:
    from konstruct import QueryError

    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "postgres"\ndsn = "postgresql://x@127.0.0.1:1/db"\n'
    )
    (root / 'report.sql').write_text('create table marts.big as select 1 as id')
    Project(root).trust()
    project = Project(root)
    project.build(run_dbt=False, warehouses=False)  # the build read no warehouse
    with pytest.raises(QueryError, match='none was found'):
        project._verification_warehouse()
    graph_json = root / 'konstruct-out' / 'graph.json'
    data = json.loads(graph_json.read_text())
    data['meta']['sources'].append({'input': 'postgres', 'name': 'postgres (konstruct.toml)'})
    graph_json.write_text(json.dumps(data))
    assert Project(root)._verification_warehouse() == 'postgresql://x@127.0.0.1:1/db'
    data['meta']['sources'][-1]['status'] = 'failed'
    graph_json.write_text(json.dumps(data))
    with pytest.raises(QueryError, match='none was found'):
        Project(root)._verification_warehouse()


def test_a_relative_profiles_dir_is_read_from_the_dbt_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener, secret: str
) -> None:
    root = dbt_project(tmp_path / 'repo', 'other:\n  target: dev\n')
    (root / 'profiles.yml').unlink()
    (root / 'ci').mkdir()
    (root / 'ci' / 'profiles.yml').write_text(postgres_profile(listener.port))
    elsewhere = tmp_path / 'cwd'
    (elsewhere / 'ci').mkdir(parents=True)
    (elsewhere / 'ci' / 'profiles.yml').write_text(LOCAL_DUCKDB)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv('DBT_PROFILES_DIR', 'ci')
    project = read_project(root)
    assert project is not None
    paths = [path for path, _ in config_module._profile_files(project, root)]
    assert paths[0] == root / 'ci' / 'profiles.yml'
    assert trust.check(root).files == ['ci/profiles.yml']  # the repository's, as dbt reads it
    files_only(root)
    assert listener.received == []


def test_usage_waits_for_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.inputs import usage

    asked: list[Any] = []

    def fetch(*args: Any) -> list[Any]:
        asked.append(args)
        return []

    monkeypatch.setattr(usage, 'fetch', fetch)
    monkeypatch.setattr(usage, 'SUPPORTED', frozenset({'duckdb'}))
    monkeypatch.setitem(usage.SOURCES, 'duckdb', 'DuckDB')
    root = tmp_path / 'repo'
    root.mkdir()
    import duckdb

    duckdb.connect(str(root / 'w.duckdb')).execute('create table t (a int)').close()
    (root / 'konstruct.toml').write_text('[usage]\nenabled = true\n')
    graph = files_only(root).graph
    assert asked == []
    warning = next(w for w in graph.warnings if trust.WARNING in w)
    assert 'query history ([usage])' in warning
    Project(root).trust()
    files_only(root)
    assert len(asked) == 1
    (root / 'konstruct.toml').write_text('[usage]\nenabled = true\ndays = 7\n')
    assert trust.check(root).state == 'changed'


# verify


def duckdb_warehouse_project(tmp_path: Path) -> tuple[Path, Path]:
    import duckdb

    outside = tmp_path / 'outside.duckdb'
    con = duckdb.connect(str(outside))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int)')
    con.close()
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text(f'[[warehouse]]\ntype = "duckdb"\npath = "{outside}"\n')
    (root / 'report.sql').write_text('create table marts.big as select id from raw.orders')
    return root, outside


def test_verify_never_opens_a_duckdb_file_the_build_skipped(tmp_path: Path) -> None:
    from konstruct import QueryError

    root, outside = duckdb_warehouse_project(tmp_path)
    project = Project(root)
    project.build(run_dbt=False)
    [skipped] = [s for s in project.graph.meta['sources'] if s.get('status') == 'skipped']
    assert str(outside) in skipped['name']
    with pytest.raises(QueryError, match='outside the project, which needs trust'):
        project.verify()
    assert not (root / 'konstruct-out' / 'VERIFY.md').exists()
    project.trust()
    with pytest.raises(QueryError):  # the graph still records the file as skipped
        project.verify()
    project.build(run_dbt=False)
    assert project._verification_warehouse() == str(outside)


def test_verify_takes_no_warehouse_from_a_graph_json_alone(tmp_path: Path) -> None:
    import duckdb

    from konstruct import QueryError

    root, outside = duckdb_warehouse_project(tmp_path)
    (root / 'konstruct.toml').unlink()
    duckdb.connect(str(root / 'inside.duckdb')).execute('create table u (b int)').close()
    project = Project(root)
    project.build(run_dbt=False)
    assert project._verification_warehouse() == str(root / 'inside.duckdb')
    graph_json = root / 'konstruct-out' / 'graph.json'
    data = json.loads(graph_json.read_text())
    # A committed graph.json names a file outside the project as one the build read.
    data['meta']['sources'] = [{'input': 'duckdb', 'name': f'duckdb {outside}', 'note': 'discovered'}]
    graph_json.write_text(json.dumps(data))
    with pytest.raises(QueryError, match='none was found'):
        Project(root).verify()
    Project(root).trust()
    with pytest.raises(QueryError, match='none was found'):
        Project(root).verify()


def test_verify_refuses_an_untrusted_remote_warehouse(tmp_path: Path) -> None:
    from konstruct import QueryError

    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[[warehouse]]\ntype = "snowflake"\naccount = "${FAKE_SECRET}"\n')
    (root / 'report.sql').write_text('create table marts.big as select 1 as id')
    project = Project(root)
    project.build(run_dbt=False)
    with pytest.raises(QueryError, match=r'cannot verify against snowflake: .* is not trusted'):
        project.verify(warehouse='snowflake')


def test_verify_refuses_an_untrusted_postgres_warehouse(
    tmp_path: Path, listener: Listener, secret: str
) -> None:
    from konstruct import QueryError

    root = postgres_project(tmp_path / 'repo', listener.port)
    project = Project(root)
    project.build(run_dbt=False)
    with pytest.raises(QueryError, match=r'postgres connection in konstruct\.toml is not trusted'):
        project.verify()
    assert listener.received == []


# The record and status


def test_the_record_lives_in_the_users_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('exclude = ["a/**"]\n[[warehouse]]\ntype = "duckdb"\npath = "x"\n')
    result = Project(root).trust()
    assert result.record == tmp_path / 'xdg' / 'konstruct' / 'trusted.json'
    data = json.loads(result.record.read_text())
    assert data['projects'][str(root)]['sha256'] == result.sha256
    # Settings that carry no connection or path can change freely.
    (root / 'konstruct.toml').write_text('exclude = ["b/**"]\n[[warehouse]]\ntype = "duckdb"\npath = "x"\n')
    assert trust.check(root).trusted
    assert main(['trust', '--revoke', '--json', str(root)]) == 0
    assert trust.check(root).state == 'untrusted'
    assert str(root) not in json.loads(result.record.read_text())['projects']
    monkeypatch.delenv('XDG_CONFIG_HOME')
    assert trust.trust_file().name == 'trusted.json'


def test_a_corrupt_record_trusts_nothing(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    trust.trust_file().parent.mkdir(parents=True, exist_ok=True)
    trust.trust_file().write_text('not json')
    assert trust.check(root).state == 'untrusted'
    assert Project(root).trust().trusted


def test_status_shows_trust(tmp_path: Path, listener: Listener) -> None:
    root = postgres_project(tmp_path / 'repo', listener.port)
    rows = dict(inspect(root).lines())
    assert rows['trust'].startswith('not trusted: konstruct.toml')
    Project(root).trust()
    assert dict(inspect(root).lines())['trust'] == 'trusted: konstruct.toml'
    assert inspect(root).to_dict()['trust']['trusted'] is True
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert 'trust' not in dict(inspect(empty).lines())
    assert 'nothing' in str(trust.check(empty))


def test_a_configuration_error_is_reported(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[broken')
    with pytest.raises(ConfigError):
        trust.check(root)


def test_trust_config_option_restores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', 'yes')
    with trust.trusting(True):
        assert os.environ['KONSTRUCT_TRUST_CONFIG'] == '1'
    assert os.environ['KONSTRUCT_TRUST_CONFIG'] == 'yes'
    with trust.trusting(False):
        assert trust.trusted_by_environment()
