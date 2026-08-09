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


def test_transport_imports_no_domain_module():
    tree = ast.parse(TRANSPORT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.rsplit(".", 1)[-1])
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    offenders = imported & DOMAIN_MODULES
    assert not offenders, (
        f"transport.py imports domain modules {offenders} - the wire side "
        "must not know what a device or a reading is"
    )
