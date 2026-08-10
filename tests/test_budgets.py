"""Size and complexity may only ever go down (the anti-erosion ratchet).

Every other disease the rebuild closed has a guard that keeps it closed -
mypy for the types, the module-global scan for the globals, the boundary
register for unchecked answers. The god module had none: nothing stopped
wemportalapi.py from growing back to three thousand lines, one reasonable
method at a time. That is what this file is.

Two different rules, because the two numbers behave differently:

  Lines are a CEILING. They move on almost every change, so demanding an
  exact match would mean a budget commit for every added comment. A file
  simply may not grow past what is written here.

  Complexity is a RATCHET - an exact match. It changes rarely, and when a
  function does get simpler, that progress should be locked in rather than
  left as headroom for the next person to spend.

Adding an entry is allowed. It is meant to be a visible, deliberate act in
a diff, which is exactly what was missing while the god module grew.
"""

import ast
import pathlib

from .complexity import functions, score

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)

# No module may pass this without an entry below. The number is not sacred:
# it sits in today's gap between the ordinary modules (the largest is under
# 800 lines) and the three that carry the history.
LINE_LIMIT = 900

# The three that are over, frozen at what they weigh today. wemportalapi
# and expert_writer are the remaining god modules - wemportalapi lost the
# transport and the statistics path in the rebuild and should keep
# shrinking; expert_writer is a Telerik form protocol written out longhand,
# where the length is mostly literal field ids.
LINE_BUDGETS = {
    "wemportalapi.py": 2674,
    "expert_writer.py": 2345,
    "config_flow.py": 917,
}

# SonarSource's own default. Above it, a function is one somebody has to
# re-read from the top to change safely.
COMPLEXITY_LIMIT = 15

# What is over the limit today, exactly. Each of these was looked at during
# the complexity cleanup and left deliberately: they are long because the
# thing they describe has that many cases, and splitting them would have
# produced pass-through helpers rather than smaller thoughts.
COMPLEXITY_BUDGETS = {
    "expert_writer.py::WemPortalExpertClient.parse_parameter_form": 29,
    "mapper.py::_clear_unanswered": 25,
    "__init__.py::_async_register_expert_service": 22,
    "scraper.py::WemPortalScraper.scrape": 22,
    "sensor.py::_parse_schedule": 21,
    "wemportalapi.py::WemPortalApi._fetch_parameter_values": 19,
    "__init__.py::async_setup_entry": 18,
    "config_flow.py::WemportalOptionsFlow._validate_configure_input": 18,
    "mapper.py::_writeable_entity": 17,
}


def _line_counts():
    return {
        source_file.name: len(source_file.read_text(encoding="utf-8").splitlines())
        for source_file in sorted(PACKAGE.glob("*.py"))
    }


def _complexities():
    found = {}
    for source_file in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for name, node in functions(tree):
            found[f"{source_file.name}::{name}"] = score(node)
    return found


def test_no_module_grows_past_its_budget():
    """The guard the god module never had."""
    too_big = {
        name: count
        for name, count in _line_counts().items()
        if count > LINE_BUDGETS.get(name, LINE_LIMIT)
    }

    assert not too_big, (
        f"module(s) over budget: {too_big}. Split the module, or - if the "
        "growth is genuinely warranted - raise its entry in LINE_BUDGETS in "
        "the same commit, so the decision is visible in the diff."
    )


def test_a_shrunk_module_is_not_left_with_its_old_budget():
    """Otherwise the ceiling drifts away from reality and stops meaning
    anything: a file that halved would still be allowed to double back."""
    counts = _line_counts()
    stale = {
        name: (budget, counts.get(name))
        for name, budget in LINE_BUDGETS.items()
        if counts.get(name) is None or budget - counts[name] > 200
    }

    assert not stale, (
        f"LINE_BUDGETS entries far above the real size (budget, actual): "
        f"{stale}. Lower them to today's count - locking in the shrink is "
        "the point of the ratchet."
    )


def test_no_function_is_more_complex_than_its_budget():
    """New complexity has to be declared, and declaring it is the moment to
    ask whether the function is describing one thing or three."""
    over = {
        name: value
        for name, value in _complexities().items()
        if value > COMPLEXITY_BUDGETS.get(name, COMPLEXITY_LIMIT)
    }

    assert not over, (
        f"function(s) over budget: {over}. Cognitive complexity counts "
        "NESTING, so pulling a nested branch into a named helper usually "
        "costs more than it saves unless the helper is a real thought. If "
        "the complexity is warranted, add the entry to COMPLEXITY_BUDGETS."
    )


def test_a_simplified_function_lowers_its_budget():
    """The ratchet itself: progress is written down, not left as headroom.

    Also catches the entry for a function that was renamed or deleted -
    an exemption nobody can find is an exemption nobody removes.
    """
    complexities = _complexities()
    drifted = {
        name: (budget, complexities.get(name))
        for name, budget in COMPLEXITY_BUDGETS.items()
        if complexities.get(name) != budget
    }

    assert not drifted, (
        f"COMPLEXITY_BUDGETS out of step (budget, actual; None = gone): "
        f"{drifted}. Set each to the current value, or drop the entry when "
        "the function is gone or back under the limit."
    )


def test_the_measuring_stick_still_measures():
    """Guards the guard: a scorer that returned 0 for everything would make
    every budget above pass. The shape below is the textbook example -
    three levels of nesting, one boolean sequence."""
    tree = ast.parse(
        "def f(items):\n"
        "    for item in items:\n"  # +1
        "        if item and item.ok:\n"  # +2 (nesting 1) +1 (bool seq)
        "            while item.next:\n"  # +3 (nesting 2)
        "                item = item.next\n"
    )

    assert score(tree.body[0]) == 7
