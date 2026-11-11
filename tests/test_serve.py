"""`konstruct serve`: the server in a subprocess, its routes and API against the Python API, the
committed OpenAPI document, live reload, tokens, ports, shutdown, and the served viewer in
Chromium. The Rust side's limits and framing are tested in tests/serve.rs."""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any, ClassVar
from urllib.parse import quote

import pytest
from jsonschema import Draft202012Validator

from konstruct import _core
from konstruct.analysis import query
from konstruct.core.graph import Graph, Node
from konstruct.interfaces import http_api, serve
from konstruct.interfaces.console import Console
from konstruct.interfaces.http_api import Api
from konstruct.interfaces.project import Project
from konstruct.outputs.html import SERVED_SELECTOR, served_page, template
from tests.conftest import dbt_executable

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / 'docs' / 'reference' / 'openapi.json'

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='stops the server with SIGINT')


class Served:
    """A `konstruct serve` process and the address it printed."""

    def __init__(self, process: subprocess.Popen[str], address: str) -> None:
        self.process = process
        self.address = address
        self.errors: IO[str] | None = None
        match = re.match(r'http://([^/]+):(\d+)/', address)
        assert match, address
        self.host, self.port = '127.0.0.1', int(match.group(2))

    def request(
        self, path: str, headers: dict[str, str] | None = None, method: str = 'GET'
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def json(self, path: str, status: int = 200) -> Any:
        code, _, body = self.request(path)
        assert code == status, (path, code, body[:500])
        return json.loads(body)

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        code = self.process.wait(timeout=20)
        if self.errors is not None:
            self.errors.close()
        return code


def environment(**extra: str) -> dict[str, str]:
    env = {**os.environ, 'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false', **extra}
    # As CI runs it: stdout to a pipe is block-buffered unless serve flushes what it prints.
    env.pop('PYTHONUNBUFFERED', None)
    dbt = dbt_executable()
    if dbt:
        env['PATH'] = f'{Path(dbt).parent}{os.pathsep}{env.get("PATH", "")}'
    return env


STARTING = 300.0
"""How long serve may take to print its address: it builds the project first."""


def start(project: Path, *options: str, env: dict[str, str] | None = None) -> Served:
    command = [
        sys.executable,
        '-m',
        'konstruct.interfaces.cli',
        'serve',
        str(project),
        '--port',
        '0',
        *options,
    ]
    # stderr to a file: a pipe nobody reads fills with a build's progress and stops the server.
    errors = tempfile.TemporaryFile(mode='w+', encoding='utf-8')  # noqa: SIM115 (open while it serves)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=errors, text=True, env=env or environment()
    )
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], STARTING)
    line = process.stdout.readline() if ready else ''
    if 'Serving' not in line:
        process.kill()
        process.wait()
        errors.seek(0)
        raise AssertionError(f'serve printed no address: {line!r} {errors.read()[-2000:]}')
    found = re.search(r' at (http://\S+)', line)
    assert found, line
    served = Served(process, found.group(1))
    served.errors = errors
    return served


class Events:
    """An open /events stream, read event by event."""

    def __init__(self, served: Served) -> None:
        self.connection = http.client.HTTPConnection(served.host, served.port, timeout=120)
        self.connection.request('GET', '/events')
        self.response = self.connection.getresponse()
        assert self.response.status == 200
        assert self.response.getheader('Content-Type') == 'text/event-stream'

    def next(self) -> dict[str, Any]:
        fields: dict[str, str] = {}
        while True:
            line = self.response.fp.readline().decode('utf-8')
            assert line, 'the stream ended'
            line = line.rstrip('\n')
            if not line:
                if 'data' in fields:
                    # Unnamed: the viewer listens with onmessage, which named events never reach.
                    assert 'event' not in fields, fields
                    data: dict[str, Any] = json.loads(fields['data'])
                    assert fields['id'] == data['version']
                    return data
                fields = {}
                continue
            if line.startswith(':'):
                continue
            key, _, value = line.partition(': ')
            fields[key] = value

    def until(self, wanted: Callable[[dict[str, Any]], bool], timeout: float = 60) -> dict[str, Any]:
        """The first event ``wanted`` accepts, within ``timeout`` seconds: a rebuild can announce a
        graph on the way, such as one built from a file caught half-written, before the one the
        test waits for."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f'no event met the condition within {timeout}s'
            assert self.connection.sock is not None
            self.connection.sock.settimeout(remaining)
            try:
                event = self.next()
            except TimeoutError:
                raise AssertionError(f'no event met the condition within {timeout}s') from None
            if wanted(event):
                return event

    def close(self) -> None:
        self.connection.close()


def validate(document: dict[str, Any], schema: dict[str, Any], value: Any) -> None:
    Draft202012Validator({**schema, 'components': document['components']}).validate(value)


@pytest.fixture(scope='module')
def served_jaffle(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """The example in a git repository of its own, built by `konstruct serve` itself."""
    project = tmp_path_factory.mktemp('served') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project, ignore=shutil.ignore_patterns('konstruct-out'))
    git = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com']
    subprocess.run([*git, 'init', '-q', '-b', 'main'], cwd=project, check=True)
    subprocess.run([*git, 'add', '-A'], cwd=project, check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'Start'], cwd=project, check=True)
    served = start(project)
    yield served
    assert served.stop() == 0


@pytest.fixture(scope='module')
def jaffle_served_graph(served_jaffle: Served) -> Graph:
    return Graph.from_dict(served_jaffle.json('/graph.json'))


def node_path(node_id: str) -> str:
    return quote(node_id, safe='')


EXAMPLES = {
    'getNode': '/api/v1/nodes/' + node_path('model.jaffle_shop.orders#amount'),
    'listNodes': '/api/v1/nodes?kind=model&limit=3',
    'resolve': '/api/v1/resolve?name=orders.amount',
    'impact': '/api/v1/impact/' + node_path('model.jaffle_shop.stg_payments#amount') + '?depth=4',
    'trace': '/api/v1/trace/' + node_path('model.jaffle_shop.orders#amount'),
    'path': '/api/v1/path?from=seed.jaffle_shop.raw_payments&to=exposure.jaffle_shop.customer_dashboard',
    'search': '/api/v1/search?q=lifetime+value&kind=column',
    'describe': '/api/v1/describe/orders',
    'explain': '/api/v1/explain/orders.amount',
    'define': '/api/v1/define?ref=lifetime+value',
    'listTerms': '/api/v1/terms?limit=2',
    'ontology': '/api/v1/ontology',
    'conflicts': '/api/v1/conflicts',
    'query': '/api/v1/query?q=what+breaks+if+stg_payments.amount+changes',
    'history': '/api/v1/history/orders.amount?limit=5',
    'diff': '/api/v1/diff?base=HEAD',
    'gate': '/api/v1/gate?base=HEAD',
    'status': '/api/v1/status',
    'report': '/api/v1/report',
    'export': '/api/v1/export/mermaid?columns=true',
}


@pytest.mark.dbt
def test_every_operation_answers_as_its_schema_says(served_jaffle: Served) -> None:
    document = served_jaffle.json('/api/v1/openapi.json')
    operations = {
        entry['get']['operationId']: (path, entry['get']) for path, entry in document['paths'].items()
    }
    assert set(operations) == {*EXAMPLES, 'openapi'}
    from konstruct.interfaces.http_api import OPERATIONS

    # Python answers every operation of the API; the server only routes, checks and keeps them.
    assert set(OPERATIONS) == set(EXAMPLES)
    cached = {op for op, (_, entry) in operations.items() if not entry['x-konstruct-cached']}
    assert cached == {'history', 'diff', 'gate', 'status'}
    for operation, url in EXAMPLES.items():
        status, headers, body = served_jaffle.request(url)
        _, entry = operations[operation]
        assert status == 200, (operation, status, body[:300])
        # Every route says what else it can answer, as problem details.
        assert entry['responses']['default']['content'] == {
            'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}
        }
        content = entry['responses']['200']['content']
        assert headers['content-type'] in content, (operation, headers['content-type'], list(content))
        if headers['content-type'] == 'application/json':
            validate(document, content['application/json']['schema'], json.loads(body))
    for operation in ('path', 'describe', 'explain', 'define', 'impact', 'history'):
        assert '404' in operations[operation][1]['responses'], operation
    # Each format of an export is sent as the type the document lists for it.
    _, export = operations['export']
    formats = next(p for p in export['parameters'] if p['name'] == 'format')['schema']['enum']
    assert set(export['responses']['200']['content']) == {http_api.EXPORT_MEDIA[f] for f in formats}
    for name in formats:
        status, headers, body = served_jaffle.request(f'/api/v1/export/{name}')
        assert status == 200, (name, body[:300])
        assert headers['content-type'] == http_api.EXPORT_MEDIA[name], name
    # A second answer from Python comes from the cache, and is the same.
    assert served_jaffle.json(EXAMPLES['explain']) == served_jaffle.json(EXAMPLES['explain'])


@pytest.mark.dbt
def test_problems_are_rfc_9457(served_jaffle: Served) -> None:
    document = served_jaffle.json('/api/v1/openapi.json')
    problem = document['components']['schemas']['Problem']
    cases = [
        ('/nothing', 'not-found', 404),
        ('/api/node/x', 'not-found', 404),
        ('/api/v1/nodes/nope', 'no-such-node', 404),
        ('/api/v1/explain/no_such_column', 'no-such-node', 404),
        ('/api/v1/explain/orders', 'not-answerable', 422),
        ('/api/v1/search', 'invalid-parameter', 400),
        ('/api/v1/impact/x?depth=deep', 'invalid-parameter', 400),
        ('/api/v1/search?q=x&limit=5000', 'invalid-parameter', 400),
        ('/api/v1/search?q=x&cursor=bad', 'invalid-parameter', 400),
        ('/api/v1/search?q=x&cursor=0000000000000000.3', 'stale-cursor', 409),
        ('/api/v1/export/pdf', 'invalid-parameter', 400),
        ('/api/v1/nodes/%ff', 'invalid-parameter', 400),
        ('/api/v1/path?from=x', 'invalid-parameter', 400),
    ]
    types = {entry['type'] for entry in document['x-konstruct-problems']}
    for path, kind, status in cases:
        code, headers, body = served_jaffle.request(path)
        assert code == status, (path, code, body)
        assert headers['content-type'] == 'application/problem+json'
        answer = json.loads(body)
        validate(document, problem, answer)
        assert answer['type'].endswith('#' + kind) and answer['type'] in types and answer['status'] == status
    code, headers, _ = served_jaffle.request('/graph.json', method='POST')
    assert code == 405 and headers['allow'] == 'GET, HEAD'
    code, _, body = served_jaffle.request('/', headers={'Host': f'rebind.example:{served_jaffle.port}'})
    assert code == 403 and json.loads(body)['type'].endswith('#host-not-allowed')
    code, _, _ = served_jaffle.request('/', headers={'Host': f'localhost:{served_jaffle.port}'})
    assert code == 200


@pytest.mark.dbt
def test_graph_answers_are_the_python_apis(served_jaffle: Served, jaffle_served_graph: Graph) -> None:
    graph = jaffle_served_graph
    for node in graph.nodes.values():
        for operation, walk in (('impact', query.impact), ('trace', query.trace)):
            served = served_jaffle.json(f'/api/v1/{operation}/{node_path(node.id)}')
            assert served == walk(graph, node.id).to_dict(), (operation, node.id)
        entry = served_jaffle.json(f'/api/v1/nodes/{node_path(node.id)}')
        assert entry == {
            'node': node.to_dict(),
            'label': graph.label(node),
            'incoming': [e.to_dict() for e in graph.in_edges(node.id)],
            'outgoing': [e.to_dict() for e in graph.out_edges(node.id)],
        }
    shallow = served_jaffle.json(
        '/api/v1/impact/' + node_path('model.jaffle_shop.stg_payments#amount') + '?depth=1'
    )
    assert shallow == query.impact(graph, 'model.jaffle_shop.stg_payments#amount', 1).to_dict()
    ids = sorted(graph.nodes)
    for start, end in zip(ids[::7], ids[3::5], strict=False):
        served = served_jaffle.json(f'/api/v1/path?from={node_path(start)}&to={node_path(end)}')
        assert served == query.path(graph, start, end).to_dict(), (start, end)
    for text, kinds in (
        ('lifetime value', []),
        ('amount', ['column']),
        ('orders', ['model', 'seed']),
        ('the', []),
    ):
        url = f'/api/v1/search?q={quote(text)}' + ''.join(f'&kind={k}' for k in kinds)
        served = served_jaffle.json(url)
        expected = query.search(graph, text, 20, kinds or None).to_dict()
        assert served['results'] == expected['results'] and served['query'] == text


def test_search_folds_case_as_python_does(tmp_path: Path) -> None:
    # U+A7CB and U+1C89 have lower cases (U+0264, U+1C8A) from Unicode 16 on; Python 3.12's
    # str.lower, on Unicode 15, keeps them. The HTTP API answers as the CLI and the MCP server do.
    graph = Graph()
    words = ['\ua7cbram', '\u1c89tje', '\u0130stanbul', '\u03a3\u0391\u03a3', '\u01c5x', 'stra\u00dfe']
    for number, word in enumerate(words):
        relation = graph.add_node(Node(f'model.p.m{number}', 'model', f'm{number}', description=word))
        graph.add_column(relation, word, description=f'a {word.upper()} b')
    project = tmp_path / 'shop'
    graph.write(project / 'konstruct-out' / 'graph.json')
    served = start(project, '--no-build')
    differ = []
    try:
        for word in words:
            for text in (word, word.lower(), word.upper(), '\u0264ram', '\u1c8atje'):
                answer = served.json(f'/api/v1/search?q={quote(text)}')
                if answer['results'] != query.search(graph, text).to_dict()['results']:
                    differ.append(text)
    finally:
        assert served.stop() == 0
    assert differ == []


@pytest.mark.dbt
def test_lists_page_in_a_stable_order(served_jaffle: Served, jaffle_served_graph: Graph) -> None:
    for base, key in (('/api/v1/nodes?kind=column', 'items'), ('/api/v1/search?q=id', 'results')):
        whole = served_jaffle.json(f'{base}&limit=1000')
        assert whole['next_cursor'] is None
        seen: list[Any] = []
        cursor = ''
        while True:
            page = served_jaffle.json(f'{base}&limit=7' + (f'&cursor={cursor}' if cursor else ''))
            seen += page[key]
            if page['next_cursor'] is None:
                break
            cursor = page['next_cursor']
        assert seen == whole[key] and len(seen) > 7
    listing = served_jaffle.json('/api/v1/nodes?kind=column&limit=1000')
    assert listing['items'] == query.list_nodes(jaffle_served_graph, ['column']).to_dict()['items']


@pytest.mark.dbt
def test_files_carry_etags_encodings_and_strict_headers(served_jaffle: Served) -> None:
    code, headers, page = served_jaffle.request('/')
    assert code == 200 and headers['content-type'] == 'text/html; charset=utf-8'
    policy = headers['content-security-policy']
    assert policy.startswith("default-src 'none'") and "script-src 'sha256-" in policy
    assert "connect-src 'self'" in policy and "frame-ancestors 'none'" in policy
    # The built page unchanged but for the one element source.ts reads.
    assert page.decode('utf-8') == template().replace(
        '<head>', '<head><meta name="konstruct-serve" content="">', 1
    )
    for name, value in (('x-content-type-options', 'nosniff'), ('referrer-policy', 'no-referrer')):
        assert headers[name] == value
    assert 'access-control-allow-origin' not in headers
    code, headers, plain = served_jaffle.request('/graph.json')
    assert code == 200 and headers['cache-control'] == 'no-cache' and headers['vary'] == 'Accept-Encoding'
    etag = headers['etag']
    assert re.fullmatch(r'"[0-9a-f]{16}"', etag)
    code, _, body = served_jaffle.request('/graph.json', {'If-None-Match': etag})
    assert code == 304 and body == b''
    code, headers, zipped = served_jaffle.request('/graph.json', {'Accept-Encoding': 'gzip'})
    assert headers['content-encoding'] == 'gzip' and gzip.decompress(zipped) == plain
    assert headers['etag'] != etag
    _, headers, _ = served_jaffle.request('/graph.json', {'Accept-Encoding': 'gzip, br'})
    assert headers['content-encoding'] == 'br'
    code, headers, body = served_jaffle.request('/graph.json', method='HEAD')
    assert code == 200 and body == b'' and int(headers['content-length']) == len(plain)
    explain = served_jaffle.json('/explain.json')
    assert 'columns' in explain


def test_the_committed_openapi_document_is_the_served_one() -> None:
    text = _core.openapi()
    if os.environ.get('KONSTRUCT_UPDATE_GOLDEN'):
        OPENAPI.write_text(text, encoding='utf-8')
    assert OPENAPI.read_text(encoding='utf-8') == text
    document = json.loads(text)
    assert document['openapi'] == '3.1.0'
    assert json.dumps(document, indent=2, sort_keys=True) + '\n' == text
    for schema in document['components']['schemas'].values():
        Draft202012Validator.check_schema(schema)
    for path, entry in document['paths'].items():
        assert path.startswith('/api/v1/')
        for parameter in entry['get'].get('parameters', []):
            Draft202012Validator.check_schema(parameter['schema'])


class Asking:
    """An :class:`Api` given the graph in a project's graph.json, as the server gives it the graph
    it serves."""

    def __init__(self, project: Path) -> None:
        self.api = Api(project, project / 'konstruct-out')
        self.path = project / 'konstruct-out' / 'graph.json'

    def __call__(self, operation: str, arguments: dict[str, Any]) -> tuple[str, str, str, str]:
        data = self.path.read_bytes()
        return self.api(operation, arguments, _core.graph_version(data), data)


def small_project(root: Path, columns: str = 'id, name') -> Path:
    (root / 'jobs').mkdir(parents=True, exist_ok=True)
    (root / 'jobs' / 'users.sql').write_text(f'create table main.people as select {columns} from raw.users\n')
    return root


def test_a_refused_reload_keeps_the_server_and_the_api_on_one_graph(tmp_path: Path) -> None:
    """A newer format, an unknown one or invalid JSON in place of graph.json leaves the graph the
    server serves, and the API keeps answering from it, until a good file comes."""
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    path = project / 'konstruct-out' / 'graph.json'
    good = json.loads(path.read_text())
    served = start(project, '--no-build')
    try:
        _, headers, kept = served.request('/graph.json')
        assert served.json('/api/v1/nodes/table.main.people')['node']['id'] == 'table.main.people'
        for text in (
            json.dumps({**good, 'format_version': 2}),
            json.dumps({**good, 'format_version': 'x'}),
            '{not json',
        ):
            temporary = path.with_name('.graph.json.tmp')
            temporary.write_text(text)
            temporary.replace(path)
            time.sleep(1.2)
            _, now, body = served.request('/graph.json')
            assert now['etag'] == headers['etag'] and body == kept, text
            # Answers come from the graph served, cached or not.
            assert served.json('/api/v1/nodes/table.main.people')['node']['id'] == 'table.main.people'
            assert served.json('/api/v1/nodes/table.main.people%23name')['node']['name'] == 'name'
        small_project(project, 'id, name, email')
        Project(project).build()
        deadline = time.monotonic() + 10
        while served.request('/graph.json')[1]['etag'] == headers['etag']:
            assert time.monotonic() < deadline, 'the good file was not served'
            time.sleep(0.2)
        assert served.json('/api/v1/nodes/table.main.people%23email')['node']['name'] == 'email'
    finally:
        assert served.stop() == 0


def test_live_reload_after_a_rebuild(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    served = start(project, '--watch')
    try:
        events = Events(served)
        first = events.next()
        _, headers, before = served.request('/graph.json')
        assert headers['etag'] == f'"{first["version"]}"'
        assert b'#email' not in before
        small_project(project, 'id, name, email')
        # The watcher may catch the file between write_text's truncation and its write, and
        # announce the graph built from that first: wait for the one with the new column.
        changed = events.until(lambda event: event['nodes'] > first['nodes'])
        assert changed['version'] != first['version']
        _, headers, after = served.request('/graph.json')
        assert headers['etag'] == f'"{changed["version"]}"' and b'#email' in after
        events.close()
    finally:
        code = served.stop()
    assert code == 0
    # The port is free again.
    with socket.socket() as again:
        again.bind(('127.0.0.1', served.port))


def test_ids_with_escapes_are_served(tmp_path: Path) -> None:
    # Quoted identifiers keep a doubled quote, a backslash and a tab, which graph.json escapes.
    project = small_project(tmp_path / 'shop', 'id, name as "a""b", name as "c\\d", name as "e\tf"')
    Project(project).build()
    graph = Graph.read(project / 'konstruct-out' / 'graph.json')
    columns = sorted(
        n.name for n in graph.nodes.values() if n.kind == 'column' and n.parent == 'table.main.people'
    )
    assert columns == ['a"b', 'c\\d', 'e\tf', 'id']
    served = start(project, '--no-build')
    try:
        for name in columns:
            entry = served.json('/api/v1/nodes/' + node_path(f'table.main.people#{name}'))
            assert entry['node']['name'] == name
    finally:
        assert served.stop() == 0


def test_a_server_others_can_reach_needs_its_token(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    token = 'a-token-for-this-test-only-0123456789'
    served = start(project, '--host', '0.0.0.0', '--no-build', env=environment(KONSTRUCT_SERVE_TOKEN=token))
    try:
        assert served.address.endswith(f'/?token={token}')
        code, headers, body = served.request('/graph.json')
        assert code == 401 and headers['www-authenticate'] == 'Bearer'
        assert json.loads(body)['type'].endswith('#unauthorised')
        code, _, _ = served.request('/graph.json', {'Authorization': f'Bearer {token}'})
        assert code == 200
        code, headers, _ = served.request(f'/?token={token}')
        cookie = headers['set-cookie'].split(';')[0]
        code, _, _ = served.request('/api/v1/search?q=name', {'Cookie': cookie})
        assert code == 200
    finally:
        assert served.stop() == 0
    generated = start(project, '--host', '0.0.0.0', '--no-build')
    try:
        made = generated.address.split('?token=')[1]
        assert len(made) >= 32 and made != token
        assert generated.request('/', {'Authorization': f'Bearer {made}'})[0] == 200
    finally:
        assert generated.stop() == 0


def committed(project: Path) -> Path:
    git = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com']
    subprocess.run([*git, 'init', '-q', '-b', 'main'], cwd=project, check=True)
    subprocess.run([*git, 'add', '-A'], cwd=project, check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'Start'], cwd=project, check=True)
    return project


def test_a_ref_git_would_read_as_an_option_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    project = committed(small_project(tmp_path / 'shop'))
    written = tmp_path / 'written'
    option = quote(f'--output={written}', safe='')
    served = start(project)
    try:
        for url in (
            f'/api/v1/diff?base={option}',
            f'/api/v1/diff?base=HEAD&head={option}',
            f'/api/v1/gate?base={option}',
            f'/api/v1/history/{option}',
        ):
            code, _, body = served.request(url)
            assert code == 400, (url, code, body)
            assert json.loads(body)['detail'].endswith('must not start with -')
        assert served.json('/api/v1/diff?base=HEAD')['base'] == 'HEAD'
    finally:
        assert served.stop() == 0
    # Behind the server, the Python operations refuse it too.
    api = Asking(project)
    for operation, arguments in (
        ('diff', {'base': f'--output={written}'}),
        ('gate', {'base': f'-o{written}'}),
    ):
        kind, _, detail, _ = api(operation, arguments)
        assert kind == 'not-answerable' and 'is not a commit, branch, tag or date' in detail
    assert not list(tmp_path.glob('written*'))


def test_a_port_in_use_fails_clearly(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'konstruct.interfaces.cli',
                'serve',
                str(project),
                '--no-build',
                '--port',
                str(port),
            ],
            capture_output=True,
            text=True,
            env=environment(),
            timeout=60,
        )
    assert result.returncode == 1
    assert f'port {port} on 127.0.0.1 is in use' in result.stderr and '--port 0' in result.stderr


# The Python layer, in process


class FakeServer:
    """Stands in for _core.Server: records how it was made and stops at once."""

    made: ClassVar[list[tuple[Any, ...]]] = []
    failure: ClassVar[OSError | None] = None

    def __init__(self, *arguments: Any) -> None:
        if FakeServer.failure is not None:
            raise FakeServer.failure
        FakeServer.made.append(arguments)
        self.host, self.port = '0.0.0.0', 8123

    def run(self) -> None:
        raise KeyboardInterrupt


def namespace(path: Path, **options: Any) -> Any:
    import argparse

    defaults = {
        'project': '.',
        'port': 0,
        'host': '127.0.0.1',
        'watch': False,
        'open': False,
        'no_build': True,
    }
    return argparse.Namespace(path=str(path), **{**defaults, **options})


def test_run_builds_serves_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = small_project(tmp_path / 'shop')
    monkeypatch.setattr(_core, 'Server', FakeServer)
    opened: list[str] = []
    monkeypatch.setattr('webbrowser.open', opened.append)
    monkeypatch.delenv('KONSTRUCT_SERVE_TOKEN', raising=False)
    FakeServer.made.clear()
    assert serve.run(namespace(project, host='0.0.0.0', open=True, watch=True), Console(colour=False)) == 0
    out, err = capsys.readouterr()
    ((out_dir, host, port, page, token, api),) = FakeServer.made
    assert Path(out_dir) == project / 'konstruct-out' and (host, port) == ('0.0.0.0', 0)
    assert 'konstruct-serve' in page and token and isinstance(api, Api) and api.project.use_plugins
    assert f'at http://127.0.0.1:8123/?token={token}' in out and opened == [
        f'http://127.0.0.1:8123/?token={token}'
    ]
    assert 'which other machines can reach' in err and 'stopped' in err
    FakeServer.failure = OSError('port 8000 on 127.0.0.1 is in use')
    try:
        assert serve.run(namespace(project), Console(colour=False)) == 1
    finally:
        FakeServer.failure = None
    assert 'is in use' in capsys.readouterr().err
    assert serve.run(namespace(project, port=70000), Console(colour=False)) == 2
    # --no-plugins reaches the answers as it reaches the build.
    FakeServer.made.clear()
    assert serve.run(namespace(project, no_plugins=True), Console(colour=False)) == 0
    assert not FakeServer.made[0][5].project.use_plugins
    assert serve.run(namespace(tmp_path / 'missing'), Console(colour=False)) == 1


def test_addresses_and_loopback() -> None:
    assert serve.url('127.0.0.1', 8000) == 'http://127.0.0.1:8000/'
    assert serve.url('0.0.0.0', 1) == 'http://127.0.0.1:1/'
    assert serve.url('::', 2) == 'http://[::1]:2/'
    assert serve.url('[::1]', 3) == 'http://[::1]:3/'
    assert serve.url('box.lan', 4) == 'http://box.lan:4/'
    assert serve.loopback('localhost') and serve.loopback('127.0.0.2') and serve.loopback('[::1]')
    assert not serve.loopback('0.0.0.0') and not serve.loopback('box.lan')


def test_the_rebuilder_rebuilds_once_per_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = Project(small_project(tmp_path / 'shop'))
    project.build()
    rebuilder = serve.Rebuilder(project, Console(colour=False, quiet=True), interval=0.01)
    assert not rebuilder.check()
    time.sleep(1.1)
    small_project(project.path, 'id, name, email')
    assert rebuilder.check()
    assert 'table.main.people#email' in ''.join(Graph.read(project.graph_path).nodes)
    assert not rebuilder.check()
    time.sleep(1.1)
    small_project(project.path, 'id')

    def broken(**_: Any) -> Any:
        raise RuntimeError('broken input')

    monkeypatch.setattr(project, 'build', broken)
    assert rebuilder.check()  # tried, failed, kept the old graph
    assert not rebuilder.check()  # not retried until the inputs change again
    rebuilder.start()
    rebuilder.stopped.set()
    rebuilder.join(timeout=5)
    assert not rebuilder.is_alive()


def test_the_rebuilder_rebuilds_when_an_input_is_deleted(tmp_path: Path) -> None:
    """A deleted or renamed input cannot be looked at, and is a change all the same."""
    project = Project(small_project(tmp_path / 'shop'))
    project.build()
    rebuilder = serve.Rebuilder(project, Console(colour=False, quiet=True), interval=0.01)
    (project.path / 'jobs' / 'users.sql').unlink()
    assert rebuilder.check()
    assert 'table.main.people' not in Graph.read(project.graph_path).nodes
    assert not rebuilder.check()


def test_the_api_answers_with_the_python_api_in_process(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    api = Asking(project)
    graph = Graph.read(project / 'konstruct-out' / 'graph.json')
    people, name = 'table.main.people', 'table.main.people#name'

    def answer(operation: str, **arguments: Any) -> Any:
        kind, media, body, _ = api(operation, arguments)
        assert (kind, media) == ('ok', 'application/json'), body
        return json.loads(body)

    entry = answer('getNode', id=name)
    assert entry['node'] == graph.nodes[name].to_dict() and entry['label'] == 'people.name'
    assert entry['incoming'] == [e.to_dict() for e in graph.in_edges(name)]
    assert (
        answer('impact', id='table.raw.users#name') == query.impact(graph, 'table.raw.users#name').to_dict()
    )
    assert answer('trace', id=name, depth=1) == query.trace(graph, name, 1).to_dict()
    assert answer('path', **{'from': 'table.raw.users', 'to': people}) == (
        query.path(graph, 'table.raw.users', people).to_dict()
    )
    found = answer('search', q='name', kind=['column'])
    assert found == query.search(graph, 'name', limit=len(graph.nodes), kinds=['column']).to_dict()
    assert answer('resolve', name='people')['nodes'][0]['id'] == people
    assert answer('status')['graph'] and answer('report')['lineage']['columns'] >= 2
    # The routes that take an id take nothing else, as the server's route table says.
    for operation, arguments in (
        ('getNode', {'id': 'people'}),
        ('impact', {'id': 'people'}),
        ('path', {'from': people, 'to': 'nowhere'}),
    ):
        kind, _, detail, _ = api(operation, arguments)
        assert kind == 'no-such-node' and 'no node has this id' in detail, (operation, detail)


def test_an_answer_names_the_graph_it_came_from(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    path = project / 'konstruct-out' / 'graph.json'
    api = Asking(project)
    kind, _, body, version = api('getNode', {'id': 'table.main.people'})
    assert kind == 'ok' and version == _core.graph_version(path.read_bytes())
    assert len(version) == 16 and json.loads(body)['node']['id'] == 'table.main.people'
    # A build replaces graph.json: the next answer comes from it, and says so.
    time.sleep(0.01)
    small_project(project, 'id, name, email')
    Project(project).build(update=True)
    kind, _, body, newer = api('getNode', {'id': 'table.main.people#email'})
    assert kind == 'ok' and newer == _core.graph_version(path.read_bytes()) != version
    # A problem names it too, and a version is read once.
    served = api.api.project.served
    assert api('getNode', {'id': 'nowhere'})[::3] == ('no-such-node', newer)
    assert api.api.project.served is served


def test_the_api_answers_from_the_graph_it_is_given_and_reads_nothing(tmp_path: Path) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    path = project / 'konstruct-out' / 'graph.json'
    good = path.read_bytes()
    api = Api(project, project / 'konstruct-out')
    path.unlink()  # the server gives the API its graph; the API does not look for the file
    kind, _, _, version = api('getNode', {'id': 'table.main.people'}, _core.graph_version(good), good)
    assert kind == 'ok' and version == _core.graph_version(good)
    # A graph Graph.read cannot read fails each answer with its error, read once.
    broken = b'{"nodes": [{"id": "a", "kind": "model"}]}'
    for _ in range(2):
        kind, _, detail, version = api('getNode', {'id': 'a'}, _core.graph_version(broken), broken)
        assert (kind, version) == ('internal-error', _core.graph_version(broken)) and 'KeyError' in detail
    kind, _, _, version = api('getNode', {'id': 'table.main.people'}, _core.graph_version(good), good)
    assert kind == 'ok' and version == _core.graph_version(good)


def test_api_errors_become_problem_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = small_project(tmp_path / 'shop')
    Project(project).build()
    api = Asking(project)
    assert api('nothing', {})[0] == 'internal-error'
    assert api('explain', {'ref': 'no_such_thing'})[0] == 'no-such-node'
    kind, media, body, _ = api('resolve', {'name': 'people'})
    assert (kind, media) == ('ok', 'application/json') and json.loads(body)['nodes'][0]['label'] == 'people'
    assert api('diff', {})[0] == 'not-answerable'  # not a git repository
    assert api('export', {'format': 'ossie', 'select': 'people'})[0] == 'not-answerable'
    kind, media, body, _ = api('export', {'format': 'csv'})
    assert kind == 'ok' and media.startswith('text/csv')
    assert json.loads(api('report', {})[2])['lineage']['columns'] >= 0

    def failing(_: Project) -> Any:
        raise ZeroDivisionError('no')

    monkeypatch.setitem(http_api.OPERATIONS, 'status', failing)
    assert api('status', {})[:3] == ('internal-error', '', 'ZeroDivisionError: no')

    # A client sees what failed, not where this machine keeps the project.
    def unreadable(_: Project) -> Any:
        raise OSError(2, 'No such file or directory', str(project / 'konstruct-out' / 'graph.json'))

    monkeypatch.setitem(http_api.OPERATIONS, 'status', unreadable)
    kind, _, detail, _ = api('status', {})
    assert kind == 'internal-error'
    assert detail == "FileNotFoundError: [Errno 2] No such file or directory: 'graph.json'"
    assert (
        http_api.without_paths(r'C:\Users\x\shop\a.sql and /tmp/x/y.json at 1/2') == 'a.sql and y.json at 1/2'
    )


# The served viewer in Chromium


def test_the_built_viewer_reads_the_served_marker() -> None:
    # The contract of viewer/src/lib/source.ts: the page looks for this element to load a served
    # graph, and the server puts it in the page's head.
    assert SERVED_SELECTOR in template()
    assert served_page().count('<meta name="konstruct-serve" content="">') == 1


@pytest.mark.browser
def test_the_served_viewer_loads_follows_changes_and_reloads_under_its_policy(tmp_path: Path) -> None:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        if os.environ.get('KONSTRUCT_REQUIRE_SERVICES'):
            raise
        pytest.skip('playwright is not installed')
    project = small_project(tmp_path / 'shop')
    served = start(project)
    problems: list[str] = []
    documents: list[int] = []
    fetched: list[tuple[str, int]] = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None
                )
            except Error as error:
                if os.environ.get('KONSTRUCT_REQUIRE_SERVICES'):
                    raise
                pytest.skip(f'Chromium is not available: {str(error).splitlines()[0]}')
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            # Before any of the page's own script runs, so a violation while it loads is seen.
            page.add_init_script(
                "document.addEventListener('securitypolicyviolation', e => "
                "console.error('CSP ' + e.violatedDirective + ' ' + e.blockedURI))"
            )
            page.on('pageerror', lambda error: problems.append(str(error)))
            page.on('console', lambda m: problems.append(m.text) if m.type in ('error', 'warning') else None)

            def seen(response: Any) -> None:
                path = response.url.split('#')[0].split('?')[0].removeprefix(served.address.split('?')[0])
                if response.request.resource_type == 'document':
                    documents.append(response.status)
                elif path in ('graph.json', 'explain.json'):
                    fetched.append((path, response.status))

            page.on('response', seen)
            page.goto(served.address + '#/node/table.main.people')
            page.wait_for_selector('#page h1:text-is("people")')
            page.wait_for_selector("#nodes [data-id='table.main.people']")
            assert 'email' not in page.inner_text('#page')
            assert ('graph.json', 200) in fetched and ('explain.json', 200) in fetched
            # A rebuild: the server sends an event, and the page reloads the graph in place.
            time.sleep(1.1)  # past the graph's modification time, as an editor's save would be
            small_project(project, 'id, name, email')
            Project(project).build(update=True)
            page.wait_for_function(
                "() => document.querySelector('#page').innerText.includes('email')", timeout=20000
            )
            assert page.inner_text('#page h1') == 'people'
            assert page.evaluate('location.hash') == '#/node/table.main.people'
            # A refresh revalidates the page, and it renders again under the same policy.
            page.reload()
            page.wait_for_selector('#page h1:text-is("people")')
            page.wait_for_function("() => document.querySelector('#page').innerText.includes('email')")
            assert documents[0] == 200 and documents[-1] in (200, 304) and len(documents) == 2
            browser.close()
    finally:
        assert served.stop() == 0
    assert problems == []
