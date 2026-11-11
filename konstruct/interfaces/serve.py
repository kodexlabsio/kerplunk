"""``konstruct serve``: the graph viewer and a JSON API over the graph, from a local server.

The server is in the native core (:class:`konstruct._core.Server`). It serves ``graph.json``,
``explain.json`` and the viewer itself, asks :mod:`konstruct.interfaces.http_api` for every API
answer and keeps those that depend only on the graph, and reloads the graph when a build replaces
it, telling open viewers to reload theirs. This module builds the graph when asked, starts the
server and, with ``--watch``, rebuilds when the project's inputs change.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import threading
import webbrowser
from pathlib import Path

from konstruct.interfaces.console import Console
from konstruct.interfaces.project import Project

WATCH_SECONDS = 1.0
"""How often ``--watch`` checks the inputs the last build read."""


def url(host: str, port: int) -> str:
    """The address to open for a server bound to ``host``: this machine for an unspecified
    address (0.0.0.0 or ::), and IPv6 addresses in brackets."""
    try:
        address = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return f'http://{host}:{port}/'
    if address.is_unspecified:
        address = ipaddress.ip_address('::1' if address.version == 6 else '127.0.0.1')
    shown = f'[{address}]' if address.version == 6 else str(address)
    return f'http://{shown}:{port}/'


def loopback(host: str) -> bool:
    """Whether ``host`` names this machine only; a name other than ``localhost`` counts as not."""
    if host.strip().lower() in ('localhost', 'localhost.'):
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


class Rebuilder(threading.Thread):
    """Rebuild the graph, reusing the build cache, whenever an input the last build read changes.
    The server notices the new ``graph.json`` and the viewer reloads."""

    def __init__(self, project: Project, console: Console, interval: float = WATCH_SECONDS) -> None:
        super().__init__(name='konstruct-watch', daemon=True)
        self.project = project
        self.console = console
        self.interval = interval
        self.stopped = threading.Event()
        # The changes a failed build was attempted for, so it is not retried until they change.
        self._failed: list[tuple[str, float]] | None = None

    def _changes(self) -> list[tuple[str, float]]:
        changed = []
        for name in self.project.changed_inputs():
            try:
                changed.append((name, (self.project.root / name).stat().st_mtime))
            except OSError:
                continue
        return changed

    def check(self) -> bool:
        """Rebuild if inputs changed; whether a build ran."""
        changes = self._changes()
        if not changes or changes == self._failed:
            return False
        names = [name for name, _ in changes]
        shown = ', '.join(names[:3]) + (f' and {len(names) - 3} more' if len(names) > 3 else '')
        self.console.info(f'{len(names)} input(s) changed ({shown}); rebuilding')
        try:
            result = self.project.build(update=True)
        except Exception as error:  # a broken input must not stop the server
            self._failed = changes
            self.console.warn(f'the rebuild failed, so the previous graph is still served: {error}')
            return True
        self._failed = None
        self.console.info(f'rebuilt in {result.seconds:.1f}s')
        return True

    def run(self) -> None:
        while not self.stopped.wait(self.interval):
            self.check()


def run(args: argparse.Namespace, console: Console) -> int:
    """Build unless asked not to, then serve until Ctrl-C."""
    from konstruct import _core
    from konstruct.outputs.html import served_page

    root = Path(args.path or args.project)
    if not root.is_dir():
        console.error(f'{root} is not a directory')
        return 1
    if not 0 <= args.port <= 65535:
        console.error(f'--port must be from 0 to 65535, not {args.port}')
        return 2
    plugins = not getattr(args, 'no_plugins', False)
    project = Project(root, plugins=plugins)
    if not args.no_build or not project.has_graph():
        console.info(f'building {project.path}')
        result = project.build(update=True)
        console.info(f'built in {result.seconds:.1f}s')
    from konstruct.interfaces.http_api import Api

    token = (
        None if loopback(args.host) else os.environ.get('KONSTRUCT_SERVE_TOKEN') or secrets.token_urlsafe(32)
    )
    api = Api(project.path, project.out_dir, plugins=plugins)
    try:
        server = _core.Server(str(project.out_dir), args.host, args.port, served_page(), token, api)
    except OSError as error:
        console.error(str(error))
        return 1
    address = url(args.host, server.port)
    if token is not None:
        console.warn(
            f'listening on {server.host}, which other machines can reach; every request needs the '
            'token, as Authorization: Bearer <token> or from the address below. This is plain HTTP: '
            'beyond a network you trust, put a TLS proxy in front'
        )
        address += f'?token={token}'
    console.print(f'Serving {project.out_dir / "graph.json"} at {address}')
    # The address (and with --host the token) is what a script or a log reads first; stdout to a
    # pipe or a file is block-buffered, and the server runs until Ctrl-C.
    console.out.flush()
    console.info(
        f'the API is at {url(args.host, server.port)}api/v1/ (openapi.json describes it); press Ctrl-C to stop'
    )
    watcher = Rebuilder(project, console) if args.watch else None
    if watcher is not None:
        watcher.start()
    if args.open:
        webbrowser.open(address)
    try:
        server.run()
    except KeyboardInterrupt:
        console.info('stopped')
    finally:
        if watcher is not None:
            watcher.stopped.set()
    return 0
