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

import ast
import re
from pathlib import Path

import pytest

MODULES = ["scraper.py", "expert_writer.py"]
PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"


def _is_request(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "post")
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "session"
    )


def _gate_calls(function):
    """(name, line) for every self._check_response(name, ...) in `function`."""
    calls = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_check_response"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            calls.append((node.args[0].id, node.lineno))
    return calls


def _branch_paths(function):
    """For every node, the chain of (if-node, which branch) it sits inside.

    Two statements are ALTERNATIVES when their chains diverge into different
    branches of the same `if`: only one of them runs. That distinction is
    what separates a legitimate "assign in either branch, check once
    afterwards" from a genuine sequential reuse of the same name.
    """
    paths = {}

    def walk(node, path):
        paths[node] = path
        for field, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    branch = (
                        (node, field)
                        if isinstance(node, (ast.If, ast.Try))
                        and field in ("body", "orelse", "handlers", "finalbody")
                        else None
                    )
                    walk(child, path + [branch] if branch else path)

    walk(function, [])
    return paths


def _alternatives(paths, a, b) -> bool:
    """Whether a and b sit in branches of the same `if` that exclude each other."""
    pa, pb = paths.get(a, []), paths.get(b, [])
    for step_a, step_b in zip(pa, pb):
        if step_a != step_b:
            return step_a[0] is step_b[0]
    return False


def request_sites(module):
    """(line, guarded) for every portal request, matched BY THE RESPONSE.

    This used to be a 30-line text window, which asks the wrong question: a
    gate call anywhere nearby satisfied it, including one in a different
    branch that guards a different response. Tying the check to the name the
    request was assigned to removes that - a request whose own response is
    never passed to the gate is unguarded, however close a gate call sits.
    """
    yield from sites_in((PACKAGE / module).read_text(encoding="utf-8"))


def sites_in(source):
    """The same scan over a source string, so it can be tested on examples."""
    tree = ast.parse(source)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        gates = _gate_calls(function)
        paths = _branch_paths(function)

        # Every request assigned to a name, in source order.
        assignments = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and _is_request(node.value)
            and isinstance(node.targets[0], ast.Name)
        ]
        assignments.sort(key=lambda n: n.lineno)

        # Collected first so the scan below can be over ALL request calls,
        # wherever they sit: an earlier version only looked at assignments
        # and at bare expression statements, so `return self.session.get(...)`
        # was invisible to it - which a mutation promptly demonstrated.
        guarded = set()
        for assign in assignments:
            name = assign.targets[0].id
            # A gate call AFTER this request, with no OTHER request
            # overwriting the name in between. Asking only "is this name
            # handed to the gate anywhere in the function" was the blind
            # spot: reuse the name and one check covered both requests.
            #
            # A reassignment in a sibling branch does not count - only one
            # of the two runs, and a check after the `if` guards whichever
            # it was. That shape is in expert_writer._postback today.
            for gate_name, gate_line in gates:
                if gate_name != name or gate_line <= assign.lineno:
                    continue
                overwritten = any(
                    other is not assign
                    and other.targets[0].id == name
                    and assign.lineno < other.lineno < gate_line
                    and not _alternatives(paths, assign, other)
                    for other in assignments
                )
                if not overwritten:
                    guarded.add(assign.value)
                    break

        for node in ast.walk(function):
            if _is_request(node):
                yield node.lineno, node in guarded


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


class _Answer:
    """A portal answer with nothing in it but a status."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "<html><body>fine</body></html>"
        self.url = "https://www.wemportal.com/Web/Default.aspx"


def _gate_for(module):
    """The module's own _check_response, bound to a real instance."""
    if module == "scraper.py":
        from custom_components.wemportal.scraper import WemPortalScraper

        return WemPortalScraper("user@example.org", "secret")._check_response

    from custom_components.wemportal.expert_writer import WemPortalExpertClient

    return WemPortalExpertClient("user@example.org", "secret")._check_response


@pytest.mark.parametrize("module", MODULES)
def test_the_gate_is_where_the_status_is_actually_handled(module):
    """The other direction: proving that nothing OUTSIDE the gate checks the
    status is worthless if nothing INSIDE it does either.

    Deleting the check from the gate would satisfy every assertion above -
    and leave the module with no status handling at all.

    Driven through the gate rather than matched as text. The previous version
    asserted the literal `status >= 400`, so tightening the rule to "only 200
    carries a page" broke a test whose point that change served: a net on the
    current spelling is a net on the code, not on the intent.
    """
    gate = _gate_for(module)

    for status in (500, 502, 403, 204, 302):
        with pytest.raises(Exception) as excinfo:
            gate(_Answer(status), "probe")
        assert type(excinfo.value).__name__ in ("ServerError", "ForbiddenError"), (
            f"{module}: status {status} was accepted as a usable answer "
            f"({type(excinfo.value).__name__})"
        )

    # The one status that IS a page must still get through, or the gate could
    # satisfy the loop above by rejecting everything.
    gate(_Answer(200), "probe")


# --- the scan itself, on examples --------------------------------------
#
# The previous scan asked "is there a gate call within 30 lines". These two
# cases are what that question got wrong: it said yes to a gate guarding a
# DIFFERENT response, and it would have said yes to one in a branch that never
# runs for this request.

GUARDED = '''
class C:
    def f(self):
        r = self.session.get("u")
        self._check_response(r, "page")
'''

OTHER_RESPONSE = '''
class C:
    def f(self):
        first = self.session.get("u")
        self._check_response(first, "page")
        second = self.session.post("u")
        return second
'''

GATE_IN_ANOTHER_BRANCH = '''
class C:
    def f(self, flag):
        if flag:
            cached = self.session.get("u")
            self._check_response(cached, "page")
        else:
            fresh = self.session.get("u")
        return fresh
'''

DISCARDED = '''
class C:
    def f(self):
        self.session.post("u")
        self._check_response(None, "page")
'''

RETURNED = '''
class C:
    def f(self):
        return self.session.get("u").text
'''

# The two shapes that look identical to a scan which only asks "is this name
# ever handed to the gate": one is a real gap, the other is correct code that
# exists in expert_writer._postback today.
REUSED_NAME = '''
class C:
    def f(self):
        resp = self.session.get("u")
        self._check_response(resp, "page")
        resp = self.session.post("u")
        return resp.text
'''

EITHER_BRANCH = '''
class C:
    def f(self, flag):
        if flag:
            resp = self.session.post("u", headers={})
        else:
            resp = self.session.post("u", allow_redirects=True)
        self._check_response(resp, "navigation postback")
        return resp.text
'''


def test_the_scan_accepts_a_request_whose_own_response_is_checked():
    assert [guarded for _line, guarded in sites_in(GUARDED)] == [True]


def test_the_scan_notices_a_second_request_that_is_not_checked():
    """The hole in the old window: one gate call satisfied both requests."""
    assert [guarded for _line, guarded in sites_in(OTHER_RESPONSE)] == [True, False]


def test_the_scan_notices_a_gate_that_guards_the_other_branch():
    """Named by the audit: a gate in a different branch masked an unguarded
    request that happens to sit near it."""
    assert [guarded for _line, guarded in sites_in(GATE_IN_ANOTHER_BRANCH)] == [
        True,
        False,
    ]


def test_the_scan_notices_a_response_that_is_thrown_away():
    """A request whose response is not even assigned cannot be checked."""
    assert [guarded for _line, guarded in sites_in(DISCARDED)] == [False]


def test_the_scan_notices_a_request_that_is_returned_directly():
    """Found by a mutation, not by reading: a request in a `return` is not an
    assignment and not a bare expression, so a scan that enumerates statement
    types misses it entirely."""
    assert [guarded for _line, guarded in sites_in(RETURNED)] == [False]


def test_the_scan_notices_a_second_request_under_the_same_name():
    """The blind spot this scan had: reuse the name and one gate call counted
    for both requests. Nothing in the source moves - only the second request
    is unchecked - so a scan that asks "is this name ever checked" reports
    perfect coverage."""
    assert [guarded for _line, guarded in sites_in(REUSED_NAME)] == [True, False]


def test_the_scan_accepts_one_gate_after_two_branches():
    """The other half: assigning in both branches of an `if` and checking once
    afterwards is correct - only one of them ran. Treating a reassignment as
    an overwrite regardless of branch would flag expert_writer._postback,
    which is the shape this describes."""
    assert [guarded for _line, guarded in sites_in(EITHER_BRANCH)] == [True, True]
