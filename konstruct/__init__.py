"""konstruct: an ontology of a data stack, with column-level lineage checked against the data.

The main entry point is `Project`::

    import konstruct

    project = konstruct.Project(".")
    project.build(update=True)
    print(project.impact("orders.amount"))
"""

__version__ = '0.13.9'

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
    from konstruct.core.formats import FormatError
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
    'FormatError': 'konstruct.core.formats',
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
    'FormatError',
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
