"""Transport must not know the domain (Umbau P9, K8).

transport.py owns getting a request to the portal and surviving its
answers - session, retry ladder, the two 403 backoffs. The moment it
imports a domain module (devices, readings, scraping, entities), the god
module has merely moved house. Import direction is the seam this phase
cut along, so it is pinned here.
"""

import ast
import pathlib

TRANSPORT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wemportal"
    / "transport.py"
)

DOMAIN_MODULES = {
    "wemportalapi",
    "statistics",
    "mapper",
    "scraper",
    "coordinator",
    "sensor",
    "number",
    "select",
    "switch",
    "date",
    "holiday",
    "entity",
    "expert_writer",
    "expert_controller",
    "translations",
}


def _modules_imported_by(source: str) -> set:
    """Every module name this source pulls in, in all three spellings.

    `from . import mapper` used to be invisible here: ast puts nothing in
    `node.module` for it - the name is an alias - so the guard walked past
    the one spelling a sibling import inside a package naturally takes. The
    package already uses it elsewhere, so this was one refactor away from a
    boundary that watched nothing.
    """
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.rsplit(".", 1)[-1])
            else:
                # `from . import x, y` - the modules are the aliases.
                imported.update(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_the_scan_sees_every_spelling_of_an_import():
    """A guard that passes proves nothing. These are the three ways to name
    a sibling module inside this package, and the third one is the one it
    was blind to."""
    assert _modules_imported_by("from .mapper import x") == {"mapper"}
    assert _modules_imported_by("import mapper") == {"mapper"}
    assert _modules_imported_by("from . import mapper") == {"mapper"}, (
        "the sibling-import spelling is invisible to this scan"
    )


def test_transport_imports_no_domain_module():
    offenders = (
        _modules_imported_by(TRANSPORT.read_text(encoding="utf-8")) & DOMAIN_MODULES
    )
    assert not offenders, (
        f"transport.py imports domain modules {offenders} - the wire side "
        "must not know what a device or a reading is"
    )
