"""The quality scale file says something, and says it about every rule.

Home Assistant checks this file only for integrations that live in core, so
nothing outside this test would ever notice that it had rotted: a typo in a
status, a rule that quietly lost its entry, or - the one that matters - an
exemption with no reason attached. An exemption without a reason is not an
exemption, it is a rule somebody skipped, and a year later nobody can tell
the two apart.

The rule NAMES are deliberately not checked against a copy of Home
Assistant's list. That list moves on their schedule, and a frozen copy here
would be wrong without anything going red - the same blindness a guard
pinned to one file has. What is checkable without guessing is the shape.
"""

import pathlib

import yaml

QUALITY_SCALE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wemportal"
    / "quality_scale.yaml"
)

ALLOWED = {"done", "exempt", "todo"}
# The two that make a claim about the future rather than the present. Both
# are read by somebody deciding whether to pick the rule up, and neither
# means anything without the sentence that goes with it.
NEEDS_A_REASON = {"exempt", "todo"}


def _rules() -> dict:
    return yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))["rules"]


def _status(entry) -> str:
    return entry if isinstance(entry, str) else entry.get("status", "")


def test_every_rule_carries_a_status_home_assistant_knows():
    wrong = {name: _status(entry) for name, entry in _rules().items()}
    wrong = {name: status for name, status in wrong.items() if status not in ALLOWED}

    assert not wrong, (
        f"{wrong} - a status Home Assistant does not define reads as no "
        f"status at all. One of {sorted(ALLOWED)}."
    )


def test_no_rule_is_excused_without_saying_why():
    """The whole point of the file. `exempt` with no comment is a rule that
    was skipped and dressed up as a decision."""
    silent = [
        name
        for name, entry in _rules().items()
        if _status(entry) in NEEDS_A_REASON
        and not (isinstance(entry, dict) and str(entry.get("comment", "")).strip())
    ]

    assert not silent, (
        f"{silent} claim(s) exempt or todo without a comment. Say what makes "
        "the rule inapplicable, or what is missing and why it is not done."
    )


def test_the_reason_check_would_notice_a_bare_exemption():
    """Proof that the check above can fail, in the exact shape it exists for:
    a status-only mapping, which is what a hurried edit produces."""
    bare = {"status": "exempt"}
    assert _status(bare) in NEEDS_A_REASON
    assert not str(bare.get("comment", "")).strip()
    assert str({"status": "exempt", "comment": "no device"}.get("comment")).strip()
