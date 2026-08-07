"""Option helpers for the expert path, without the expert client.

These three are pure functions over the entry options, but they used to live
in expert_writer.py - which imports curl_cffi and lxml at module level. Since
config_flow needs them on every options dialog, that single import decided
that every installation loaded the HTTP stack, whether or not expert access
was ever enabled. Splitting them out is what makes the lazy import of the
client real rather than claimed.
"""

from .const import (
    CONF_EXPERT_ENABLE_MODULE_NAV,
    CONF_EXPERT_ENABLE_SECURITY_CODE,
    CONF_EXPERT_MODULE_ARG,
)


def discovery_option_list(discovered, current_ids) -> list:
    """Build the slot-dropdown options from discovery + current selections.

    Discovered parameters come first (labelled "group / name (value)"); any
    already-configured id not among them is appended (labelled by its raw id)
    so a stored selection stays selectable even without a fresh discovery.
    De-duplicated by entityvalue; empty ids skipped.
    """
    options = []
    seen = set()
    for parameter in discovered or []:
        entityvalue = (parameter.get("entityvalue") or "").strip()
        if not entityvalue or entityvalue in seen:
            continue
        seen.add(entityvalue)
        label = (
            f"{parameter.get('group', '')} / {parameter.get('name', '')} "
            f"({parameter.get('value', '')})"
        )
        options.append({"value": entityvalue, "label": label})
    for entityvalue in current_ids or []:
        entityvalue = (entityvalue or "").strip()
        if not entityvalue or entityvalue in seen:
            continue
        seen.add(entityvalue)
        options.append({"value": entityvalue, "label": entityvalue})
    return options


def duplicate_entityvalues(id_values) -> set:
    """Return the set of entityvalues used more than once (non-empty)."""
    counts = {}
    for raw in id_values or []:
        entityvalue = (raw or "").strip()
        if entityvalue:
            counts[entityvalue] = counts.get(entityvalue, 0) + 1
    return {entityvalue for entityvalue, n in counts.items() if n > 1}


def expert_client_options(options):
    """Return the WemPortalExpertClient kwargs derived from entry options.

    Centralises reading the module argument and the two advanced navigation
    toggles (module select / security code) so every client instantiation -
    write service, entity background write, and auto-poll - stays consistent.
    Both toggles default to OFF (i.e. the steps stay skipped) unless the user
    enabled them in the options UI.
    """
    module_arg = (options.get(CONF_EXPERT_MODULE_ARG) or "").strip() or None
    return {
        "module_arg": module_arg,
        "enable_module_nav": bool(options.get(CONF_EXPERT_ENABLE_MODULE_NAV, False)),
        "enable_security_code": bool(
            options.get(CONF_EXPERT_ENABLE_SECURITY_CODE, False)
        ),
    }
