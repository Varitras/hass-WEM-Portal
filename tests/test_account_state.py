"""One place for what an account remembers across reloads (Umbau P7, K7).

Six module-level globals across five files used to hold this - each
invisible from the others, each cleared by nobody on removal, each a
separate trap in tests. They live in models.AccountState now, keyed by the
normalised account id, and the guard test at the bottom keeps the package
from growing new module-level mutable state.
"""

import ast
import pathlib

from custom_components.wemportal.models import (
    AccountState,
    account_state,
    reset_account_states_for_tests,
)

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)


def test_the_same_account_gets_the_same_memory():
    """Reload survival is identity: a rebuilt api asking again must find the
    state the previous instance left - including when the username arrives
    in different casing, which is one account at the portal."""
    first = account_state("User@Example.org ")
    first.expert_blocked_until = 99.0

    assert account_state("user@example.org") is first
    assert account_state("user@example.org").expert_blocked_until == 99.0


def test_two_accounts_do_not_share_memory():
    """The expert backoff is per account by design - a 403 there is usually
    one rejected request, not an IP-wide limit, and must not spread."""
    account_state("a@example.org").expert_blocked_until = 99.0

    assert account_state("b@example.org").expert_blocked_until == 0.0
    assert account_state("a@example.org") is not account_state("b@example.org")


def test_the_test_reset_really_forgets():
    state = account_state("a@example.org")
    state.auth_failures = 3

    reset_account_states_for_tests()

    assert account_state("a@example.org") is not state
    assert account_state("a@example.org").auth_failures == 0


# --- the guard: no new module-level mutable state -----------------------

# The two sanctioned module globals, each with a reason:
#   models._ACCOUNT_STATES     - the registry the account state survives
#                                reloads in; everything else belongs INSIDE it.
#   wemportalapi._BLOCKED_UNTIL - the IP-wide 403 backoff. The portal limits
#                                per IP, so this is installation state, not
#                                account state.
SANCTIONED_MODULE_STATE = {
    ("models.py", "_ACCOUNT_STATES"),
    ("wemportalapi.py", "_BLOCKED_UNTIL"),
}

_MUTATING_METHODS = {
    "add",
    "append",
    "clear",
    "discard",
    "pop",
    "popitem",
    "remove",
    "setdefault",
    "update",
}


def _module_level_mutable_state(source):
    """Names that hold module-level mutable state in `source`.

    Two ways to qualify: a `global NAME` statement anywhere (reassignment
    from a function), or a module-level container that some code in the
    module mutates. A container nobody mutates is a constant and passes.
    """
    tree = ast.parse(source)

    containers = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        is_container = isinstance(value, (ast.Dict, ast.Set, ast.List)) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in ("dict", "set", "list")
        )
        if not is_container:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        containers.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )

    flagged = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            flagged.update(node.names)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATING_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in containers
        ):
            flagged.add(node.func.value.id)
        if isinstance(node, (ast.Assign, ast.Delete)) and not isinstance(
            node, ast.AnnAssign
        ):
            targets = node.targets
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in containers
                ):
                    flagged.add(target.value.id)
    return flagged


def test_no_module_level_mutable_state_outside_the_registry():
    """K7's guard. State that must outlive an object belongs in
    models.AccountState - a new module global is how the six-globals
    situation started, one plausible exception at a time."""
    found = set()
    for source_file in sorted(PACKAGE.glob("*.py")):
        for name in _module_level_mutable_state(
            source_file.read_text(encoding="utf-8")
        ):
            found.add((source_file.name, name))

    assert found == SANCTIONED_MODULE_STATE, (
        f"module-level mutable state changed: {found ^ SANCTIONED_MODULE_STATE}. "
        "If it must survive a reload it belongs in models.AccountState; if it "
        "is installation-wide, say why next to SANCTIONED_MODULE_STATE."
    )


def test_account_state_starts_empty():
    """The dataclass defaults are the contract a fresh account starts from."""
    state = AccountState()

    assert state.auth_failures == 0
    assert state.expert_blocked_until == 0.0
    assert state.duplicate_rows_reported == set()
    assert state.missing_job_ids_reported == set()
