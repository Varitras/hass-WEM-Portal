"""Transport must not know the domain (Umbau P9, K8).

transport.py owns getting a request to the portal and surviving its answers -
session, retry ladder, the two 403 backoffs, and the login. The moment it
imports a domain module (devices, readings, scraping, entities), the god
module has merely moved house. Import direction is the seam this phase cut
along, so it is pinned here.

Pinned as a WHITELIST, not a blacklist of domain modules. A blacklist only
catches a DIRECT import of a domain module; it missed a utility module that
itself loads the domain - transport imported utils.maintenance_notice, and
utils pulls in the sensor and reading models, so the whole domain came in
transitively. The wire side may reach for exactly the domain-free siblings
below and nothing else. TYPE_CHECKING imports are exempt: they run never, so
they drag nothing in.
"""

import ast
import pathlib

TRANSPORT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wemportal"
    / "transport.py"
)

# The only sibling modules the wire side may import AT RUNTIME - every one is
# domain-free. utils is deliberately absent: it imports
# homeassistant.components.sensor and .models, so a runtime import of it drags
# the domain in transitively (the hole the old blacklist missed). The
# maintenance protocol the login needs lives in web_protocol for exactly this.
ALLOWED_TRANSPORT_SIBLINGS = {
    "const",
    "exceptions",
    "mobile_protocol",
    "web_protocol",
}


def _guards_type_checking(test) -> bool:
    """Whether an `if` test is `TYPE_CHECKING` (bare or attribute-qualified)."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _relative_names(node) -> set:
    """The sibling module name(s) a relative ImportFrom names.

    `from .x import a` -> {"x"}; `from . import x, y` -> {"x", "y"} (the
    aliases ARE the modules, and ast leaves node.module empty for that
    spelling - the hole the sibling-import scan was once blind to).
    """
    if node.module:
        return {node.module.rsplit(".", 1)[-1]}
    return {alias.name for alias in node.names}


def _runtime_sibling_imports(source: str) -> set:
    """Sibling modules imported at RUNTIME.

    Relative imports (`from .x`, `from . import x`) that are NOT inside an
    `if TYPE_CHECKING:` block. A TYPE_CHECKING import runs never, so it cannot
    drag anything in; a runtime import of a module that itself loads the
    domain does, which is the transitive hole this guard exists to close.
    Absolute imports (`import x`, `from homeassistant...`) are not siblings.
    """
    tree = ast.parse(source)
    type_checking = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _guards_type_checking(node.test):
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.level >= 1:
                    type_checking |= _relative_names(inner)
    runtime = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1:
            runtime |= _relative_names(node)
    return runtime - type_checking


def test_the_scan_reads_runtime_siblings_only():
    """Both relative spellings count; absolute imports and TYPE_CHECKING do not."""
    assert _runtime_sibling_imports("from .mapper import x") == {"mapper"}
    assert _runtime_sibling_imports("from . import mapper") == {"mapper"}, (
        "the sibling-import spelling is invisible to a naive scan"
    )
    assert _runtime_sibling_imports("import mapper") == set(), (
        "an absolute import is not a sibling"
    )
    guarded = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .models import Reading\n"
    )
    assert _runtime_sibling_imports(guarded) == set(), (
        "a TYPE_CHECKING import runs never, so it drags nothing in"
    )


def test_the_guard_catches_a_utility_that_loads_the_domain():
    """The transitive hole, spelled out: importing utils - which loads the
    sensor and reading models - at runtime must be flagged, even though utils
    is not itself a domain module."""
    offending = "from .utils import maintenance_notice\n"
    assert _runtime_sibling_imports(offending) - ALLOWED_TRANSPORT_SIBLINGS == {"utils"}


def test_transport_imports_only_domain_free_siblings():
    offenders = (
        _runtime_sibling_imports(TRANSPORT.read_text(encoding="utf-8"))
        - ALLOWED_TRANSPORT_SIBLINGS
    )
    assert not offenders, (
        f"transport.py imports {sorted(offenders)} at runtime - the wire side "
        "may only reach for the domain-free siblings in "
        "ALLOWED_TRANSPORT_SIBLINGS. A utility module that itself loads the "
        "sensor or reading models drags the whole domain in transitively."
    )
