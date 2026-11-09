"""konstruct: a queryable graph of a data stack, with column-level lineage.

The main entry point is `Project`::

    import konstruct

    project = konstruct.Project(".")
    project.build(update=True)
    print(project.impact("orders.amount"))
"""

__version__ = '0.12.5'

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from konstruct.analysis.explain import Explanation
    from konstruct.analysis.query import (
        Definitions,
        Description,
        EdgeItem,
        Item,
        Listing,
        PathResult,
        QueryError,
        QueryResult,
        SearchResult,
        TermDefinition,
        Walk,
    )
    from konstruct.core.graph import Edge, Graph, Node
    from konstruct.interfaces.project import Project

# The public names are imported when first used, so `konstruct --help` and the commands that do
# not analyse SQL start without loading sqlglot.
_EXPORTS = {
    'Explanation': 'konstruct.analysis.explain',
    **dict.fromkeys(
        (
            'Definitions',
            'Description',
            'EdgeItem',
            'Item',
            'Listing',
            'PathResult',
            'QueryError',
            'QueryResult',
            'SearchResult',
            'TermDefinition',
            'Walk',
        ),
        'konstruct.analysis.query',
    ),
    **dict.fromkeys(('Edge', 'Graph', 'Node'), 'konstruct.core.graph'),
    'Project': 'konstruct.interfaces.project',
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    'Definitions',
    'Description',
    'Edge',
    'EdgeItem',
    'Explanation',
    'Graph',
    'Item',
    'Listing',
    'Node',
    'PathResult',
    'Project',
    'QueryError',
    'QueryResult',
    'SearchResult',
    'TermDefinition',
    'Walk',
    '__version__',
]
