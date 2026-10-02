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

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"

# Requests whose answers are read somewhere other than a page gate, and why.
NOT_THROUGH_THE_PAGE_GATE = {
    # The mobile API client: make_api_call and the web login read its
    # answers, and test_transport_errors and test_security hold those.
    "transport.py": "the mobile API's own answer handling",
}


def _is_request(node):
    """A `.get` or `.post` on a session - an attribute (`self.session`) or a
    plain local (`session`), since the login keeps its session in one."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "post")
    ):
        return False
    session = node.func.value
    return (isinstance(session, ast.Attribute) and session.attr == "session") or (
        isinstance(session, ast.Name) and session.id == "session"
    )


def _modules_that_request():
    """Every module of the package that sends a portal request, by its code.

    A list of names was here, and a request moved to a third module would
    have been read by nothing.
    """
    return sorted(
        path.name
        for path in PACKAGE.glob("*.py")
        if any(
            _is_request(node)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )


MODULES = [
    module
    for module in _modules_that_request()
    if module not in NOT_THROUGH_THE_PAGE_GATE
]


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
    # strict=False is the point, not a concession: the loop looks for the
    # first step where the two paths diverge, so stopping at the shorter one
    # is correct - a shorter path is a prefix of the longer, and a prefix
    # contains no branch that could separate them.
    for step_a, step_b in zip(pa, pb, strict=False):
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


def test_the_page_clients_are_found_by_what_they_send():
    """Guards the derivation: an empty list would pass every test below."""
    assert {"scraper.py", "expert_writer.py"} <= set(MODULES)


@pytest.mark.parametrize(
    "call", ["self.session.get(url)", "session.post(url, data=form)"]
)
def test_a_request_is_seen_however_its_session_is_held(call):
    assert any(_is_request(node) for node in ast.walk(ast.parse(call)))


def test_every_excused_module_still_sends_requests():
    """An excuse outliving its module would hide the next one of that name."""
    stale = set(NOT_THROUGH_THE_PAGE_GATE) - set(_modules_that_request())

    assert not stale, f"{sorted(stale)} no longer send requests; drop the entries."


@pytest.mark.parametrize("module", MODULES)
def test_the_scan_actually_finds_the_requests(module):
    """Guards the guard: a regex that matches nothing would pass silently and
    report perfect coverage forever. Any site at all: the modules are found by
    their requests now, and a small new client is not a scan that broke."""
    assert list(request_sites(module))


def _requests_classifying_their_transport_failure(tree):
    """Every request whose own try block turns a failure into the right type."""
    classified = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        classifies = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_transport_failure"
            for handler in node.handlers
            for call in ast.walk(handler)
        )
        if not classifies:
            continue
        for statement in node.body:
            classified.update(
                inner for inner in ast.walk(statement) if _is_request(inner)
            )
    return classified


def test_every_scraper_request_classifies_its_transport_failure():
    """A raw timeout here is not untidiness - it is the wrong exception.

    The request timeout may be what is LEFT of the poll cycle, so running out
    of it is the cycle stopping itself, which the coordinator handles by
    keeping the warm session. Unclassified it is an ordinary failure instead:
    the second one discards that session, and in the session-reuse path it
    also triggers a full login against a portal that just failed to answer.

    Only the login GET did this; the other three request sites did not.
    Scanned rather than listed so a fifth site cannot be added without it.
    """
    tree = ast.parse((PACKAGE / "scraper.py").read_text(encoding="utf-8"))
    classified = _requests_classifying_their_transport_failure(tree)

    assert len(classified) >= 4, (
        "the scan found fewer request sites than the module has, so it would "
        "report perfect coverage forever"
    )
    unclassified = [
        node.lineno
        for node in ast.walk(tree)
        if _is_request(node) and node not in classified
    ]
    assert not unclassified, (
        f"scraper.py: the request(s) at line(s) {', '.join(map(str, unclassified))} "
        "let a transport failure through as-is. Wrap them and raise "
        "self._transport_failure(exc, '<what>'), so a spent cycle budget "
        "arrives as the deadline it is."
    )


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


def _timeout_argument(call):
    """The `timeout=` keyword of one request call, or None if it has none."""
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    return None


def test_every_scrape_request_is_capped_by_the_remaining_budget():
    """A scrape request must take its timeout from the poll's budget.

    Lives here because this module already knows how to find a request; the
    invariant is a sibling of the one above. Both are the same shape of
    mistake: something every request site has to get right, decided per site.

    A constant timeout is how a scrape begun one second before the deadline
    ran thirty seconds past it - six times over, for a session reuse followed
    by a full login - while the coordinator had already given up and the
    worker still held the shared lock.

    Only the scraper. expert_writer runs outside a poll cycle, has a user
    waiting on it and no deadline to respect, so a fixed timeout is right
    there.
    """
    source = (PACKAGE / "scraper.py").read_text(encoding="utf-8")
    uncapped = []
    for node in ast.walk(ast.parse(source)):
        if not _is_request(node):
            continue
        timeout = _timeout_argument(node)
        capped = (
            isinstance(timeout, ast.Call)
            and isinstance(timeout.func, ast.Attribute)
            and timeout.func.attr == "_request_timeout"
        )
        if not capped:
            uncapped.append(node.lineno)

    assert not uncapped, (
        f"scraper.py: the request(s) at line(s) {', '.join(map(str, uncapped))} "
        "set their own timeout instead of self._request_timeout(), so they can "
        "outlive the poll cycle they belong to."
    )


def test_the_scan_would_notice_an_uncapped_request():
    """Guards the guard: a scan that finds no requests reports perfect
    coverage forever. The module-level scan above has the same tripwire."""
    assert len(list(request_sites("scraper.py"))) >= 4


class _Answer:
    """A portal answer with nothing in it but a status."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "<html><body>fine</body></html>"
        self.url = "https://www.wemportal.com/Web/Default.aspx"


def _gate_for(module):
    """The module's own _check_response, bound to a real instance.

    Found in the module rather than named: the class defined there that
    defines the gate itself. A module without one raises LookupError.
    """
    import importlib
    import inspect

    imported = importlib.import_module(
        f"custom_components.wemportal.{module.removesuffix('.py')}"
    )
    owners = [
        cls
        for _name, cls in inspect.getmembers(imported, inspect.isclass)
        if cls.__module__ == imported.__name__ and "_check_response" in vars(cls)
    ]
    if len(owners) != 1:
        raise LookupError(f"{module} defines {len(owners)} classes with a gate")
    return owners[0]("user@example.org", "secret")._check_response


def test_a_module_without_a_gate_of_its_own_is_not_lent_another():
    """The modules are found by what they send, and their gate was then
    looked up by name: any module but the scraper got the expert client's.
    A new page client with a gate that checks nothing would have been
    tested against a gate that does."""
    with pytest.raises(LookupError):
        _gate_for("utils.py")


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
    from custom_components.wemportal.exceptions import ForbiddenError, ServerError

    gate = _gate_for(module)

    # Named rather than caught as a bare Exception and identified afterwards:
    # anything else the gate might raise - an AttributeError from a botched
    # refactor, say - used to read as "rejected, good" for one assertion and
    # was only then told apart by class NAME. Naming both here lets the wrong
    # exception travel up as the failure it is.
    for status in (500, 502, 403, 204, 302):
        answer = _Answer(status)
        with pytest.raises((ServerError, ForbiddenError)):
            gate(answer, "probe")

    # The one status that IS a page must still get through, or the gate could
    # satisfy the loop above by rejecting everything.
    gate(_Answer(200), "probe")


# --- the scan itself, on examples --------------------------------------
#
# The previous scan asked "is there a gate call within 30 lines". These two
# cases are what that question got wrong: it said yes to a gate guarding a
# DIFFERENT response, and it would have said yes to one in a branch that never
# runs for this request.

GUARDED = """
class C:
    def f(self):
        response = self.session.get("u")
        self._check_response(response, "page")
"""

OTHER_RESPONSE = """
class C:
    def f(self):
        first = self.session.get("u")
        self._check_response(first, "page")
        second = self.session.post("u")
        return second
"""

GATE_IN_ANOTHER_BRANCH = """
class C:
    def f(self, flag):
        if flag:
            cached = self.session.get("u")
            self._check_response(cached, "page")
        else:
            fresh = self.session.get("u")
        return fresh
"""

DISCARDED = """
class C:
    def f(self):
        self.session.post("u")
        self._check_response(None, "page")
"""

RETURNED = """
class C:
    def f(self):
        return self.session.get("u").text
"""

# The two shapes that look identical to a scan which only asks "is this name
# ever handed to the gate": one is a real gap, the other is correct code that
# exists in expert_writer._postback today.
REUSED_NAME = """
class C:
    def f(self):
        resp = self.session.get("u")
        self._check_response(resp, "page")
        resp = self.session.post("u")
        return resp.text
"""

EITHER_BRANCH = """
class C:
    def f(self, flag):
        if flag:
            resp = self.session.post("u", headers={})
        else:
            resp = self.session.post("u", allow_redirects=True)
        self._check_response(resp, "navigation postback")
        return resp.text
"""


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
