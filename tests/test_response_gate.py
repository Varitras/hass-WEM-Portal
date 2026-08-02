"""Every portal request must pass its response through the shared gate.

This is an architecture test, not a behaviour test, and it exists because
behaviour tests kept arriving one request too late. Each request site used to
decide for itself what to validate, so ten sites checked for a 403 while three
checked the status code - and three separate audit rounds each found the NEXT
unguarded request, one at a time:

  * the expert-page POST parsed a 500 as a real page,
  * then the main-page GET did, ten lines above it,
  * then the save postback and the verification read did.

Fixing them individually never ended, because the gap was structural: a
per-site decision is a per-site chance to forget. `_check_response` removes
the decision; this test removes the chance to bypass it.

If this fails, do not add a bespoke check at the new call site - route it
through the gate.
"""

import re
from pathlib import Path

import pytest

MODULES = ["scraper.py", "expert_writer.py"]
PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"

# A request and its validation belong together; the distance only has to
# tolerate the argument list and any comment explaining the call.
LOOKAHEAD = 30

REQUEST = re.compile(r"self\.session\.(get|post)\(")


def request_sites(module):
    lines = (PACKAGE / module).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if REQUEST.search(line):
            window = lines[index : index + LOOKAHEAD]
            yield index + 1, any("_check_response" in entry for entry in window)


@pytest.mark.parametrize("module", MODULES)
def test_every_request_passes_through_the_gate(module):
    unguarded = [line for line, guarded in request_sites(module) if not guarded]

    assert not unguarded, (
        f"{module}: the response of the request(s) at line(s) "
        f"{', '.join(map(str, unguarded))} is never checked. Route it through "
        "self._check_response(response, '<what>') instead of validating it "
        "here - see this module's docstring for why."
    )


@pytest.mark.parametrize("module", MODULES)
def test_the_scan_actually_finds_the_requests(module):
    """Guards the guard: a regex that matches nothing would pass silently and
    report perfect coverage forever."""
    assert len(list(request_sites(module))) >= 4


# The gate and the helper it delegates the 403 case to. Status handling lives
# in these two and nowhere else.
GATE_METHODS = ("_check_response", "_raise_if_forbidden")

METHOD_START = re.compile(r"^    def (\w+)\(")


def code_outside_the_gate(module):
    """Every line of the module except the gate's own implementation."""
    lines = (PACKAGE / module).read_text(encoding="utf-8").splitlines()
    kept, inside_gate = [], False
    for line in lines:
        match = METHOD_START.match(line)
        if match:
            inside_gate = match.group(1) in GATE_METHODS
        if not inside_gate:
            kept.append(line)
    return "\n".join(kept)


@pytest.mark.parametrize("module", MODULES)
def test_no_request_site_checks_the_status_itself(module):
    """The gate owns status handling. A hand-rolled check beside a request is
    exactly how the inconsistency looked, so it must not creep back in."""
    strays = re.findall(
        r"status_code\s*(?:!=|>=|==)\s*\d", code_outside_the_gate(module)
    )

    assert not strays, (
        f"{module}: HTTP status handled outside the gate: {strays}. Pass the "
        "response to self._check_response() instead."
    )


@pytest.mark.parametrize("module", MODULES)
def test_the_gate_is_where_the_status_is_actually_handled(module):
    """The other direction: proving that nothing OUTSIDE the gate checks the
    status is worthless if nothing INSIDE it does either.

    Deleting the check from the gate would satisfy every assertion above -
    and leave the module with no status handling at all.
    """
    source = (PACKAGE / module).read_text(encoding="utf-8")
    inside = source[source.index("def _check_response"):]
    inside = inside.split("\n    def ", 1)[0]

    assert re.search(r"status\s*>=\s*400", inside), (
        f"{module}: the gate no longer rejects error responses"
    )
