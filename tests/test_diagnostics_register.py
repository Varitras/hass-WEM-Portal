"""The diagnostics config-field register: every config key carries a decision.

The diagnostics download is written to be attached to a public issue, so it
redacts installation-specific values (diagnostics.TO_REDACT). TO_REDACT is a
hand-kept blocklist, and the recurring audit class is a blocklist that leaks
each NEW field exactly once: a config option added later, stored in the entry,
carried into the dump, and forgotten in TO_REDACT.

This register forces the decision when the surface grows. Every config key the
integration can store carries an explicit "redacted" or "readable: <reason>",
and a "redacted" claim is verified against the real TO_REDACT so the register
cannot become a second, drifting blocklist. A new CONF_ is a RED test the
moment it is added, not four audits later.

The API row keys (DeviceID, ConnectionStatus, ...) are deliberately NOT covered
here: they come from the portal, not our code, so there is no source of truth
to introspect. They stay covered by the row-key aliasing and the DeviceID
redaction, checked in the e2e diagnostics test.
"""

from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME

from custom_components.wemportal import const
from custom_components.wemportal.diagnostics import TO_REDACT

REDACTED = "redacted"
READABLE = "readable"  # a readable entry must carry a reason after the colon

REGISTER = {
    CONF_USERNAME: "redacted",
    CONF_PASSWORD: "redacted",
    CONF_SCAN_INTERVAL: "readable: web poll interval in seconds",
    const.CONF_SCAN_INTERVAL_API: "readable: api poll interval in seconds",
    const.CONF_LANGUAGE: "readable: UI language, en/de",
    const.CONF_MODE: "readable: poll mode, web/api/both",
    const.CONF_EXPERT_WRITE: "readable: feature flag, expert write on/off",
    const.CONF_EXPERT_AUTO_POLL: "readable: feature flag, auto-poll on/off",
    const.CONF_EXPERT_POLL_INTERVAL: "readable: auto-poll interval in minutes",
    const.CONF_EXPERT_NOTIFY_ON_SUCCESS: "readable: feature flag, success popup",
    const.CONF_EXPERT_ENABLE_MODULE_NAV: "readable: navigation toggle",
    const.CONF_EXPERT_ENABLE_SECURITY_CODE: "readable: navigation toggle, not the code",
    const.CONF_EXPERT_MODULE_ARG: "readable: generic icon-menu index, digits only",
    const.CONF_EXPERT_MODULE_LIST: "redacted",
}
# The two per-slot templates are the growing part of the expert surface; each
# expands to one stored key per slot. The id is the installation entityvalue
# (redacted); the name is a label the user typed (readable).
for _slot in range(1, const.EXPERT_SLOT_COUNT + 1):
    REGISTER[const.CONF_EXPERT_SLOT_ID_TEMPLATE % _slot] = "redacted"
    REGISTER[const.CONF_EXPERT_SLOT_NAME_TEMPLATE % _slot] = "readable: user slot label"


def _declared_config_keys() -> set:
    """Every config key the integration can store under entry.data/options.

    Introspected from const.py, because the growing surface is ours - never a
    hand-listed copy, which would be the blocklist this register replaces. Plus
    the three keys taken from homeassistant.const, a fixed set HA owns that this
    integration does not grow.
    """
    keys = {CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL}
    for name in dir(const):
        if not name.startswith("CONF_"):
            continue
        value = getattr(const, name)
        if not isinstance(value, str):
            continue
        if "%" in value:
            keys.update(value % slot for slot in range(1, const.EXPERT_SLOT_COUNT + 1))
        else:
            keys.add(value)
    return keys


def test_every_config_key_has_a_decision():
    undecided = _declared_config_keys() - set(REGISTER)
    assert not undecided, (
        f"config key(s) with no decision: {sorted(undecided)}. Add each to "
        "REGISTER - 'redacted' (and to diagnostics.TO_REDACT) or "
        "'readable: <reason>'. Silence is how a new option leaks into a public "
        "report exactly once."
    )


def test_no_register_entry_outlives_its_key():
    stale = set(REGISTER) - _declared_config_keys()
    assert not stale, (
        f"register entries for config keys that no longer exist: {sorted(stale)}"
    )


def test_every_decision_is_from_the_vocabulary():
    invalid = {
        key: decision
        for key, decision in REGISTER.items()
        if decision != REDACTED and not decision.startswith(READABLE + ":")
    }
    assert not invalid, (
        f"unknown decision(s): {invalid}. Use '{REDACTED}' or "
        f"'{READABLE}: <reason>' - a readable field with no reason is a shrug."
    )


def test_a_redacted_decision_matches_the_real_redaction():
    """The tie to reality: the register may not claim a redaction the dump does
    not perform, nor stay silent about one it does.

    Both directions against diagnostics.TO_REDACT (what async_redact_data
    actually uses), so the register cannot drift into a second blocklist.
    """
    claims_redacted = {
        key for key, decision in REGISTER.items() if decision == REDACTED
    }

    unbacked = claims_redacted - set(TO_REDACT)
    assert not unbacked, (
        f"REGISTER says 'redacted' but diagnostics.TO_REDACT does not redact "
        f"{sorted(unbacked)}. Add them to TO_REDACT or change the decision."
    )

    config_keys_actually_redacted = _declared_config_keys() & set(TO_REDACT)
    undeclared = config_keys_actually_redacted - claims_redacted
    assert not undeclared, (
        f"diagnostics.TO_REDACT redacts {sorted(undeclared)} but the register "
        "does not say 'redacted' for them."
    )


def test_the_completeness_check_flags_an_undecided_key():
    """Proof-of-red: a config key with no decision is caught."""
    grown_surface = _declared_config_keys() | {"expert_some_new_option"}
    assert grown_surface - set(REGISTER) == {"expert_some_new_option"}


def test_the_tie_flags_a_redaction_the_dump_does_not_perform():
    """Proof-of-red: a 'redacted' claim absent from TO_REDACT is caught."""
    claims = {"a_key_never_in_to_redact"}
    assert claims - set(TO_REDACT) == {"a_key_never_in_to_redact"}
