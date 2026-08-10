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


def canonical_entityvalue(raw) -> str:
    """One spelling to compare entityvalues by.

    They are hex, so case carries no meaning: `3A7F` and `3a7f` are the same
    parameter of the same installation. Comparing them verbatim let one id
    occupy two slots, and made the service's allowlist refuse whichever
    spelling the caller did not happen to use.

    Only for COMPARING. The value that goes to the portal keeps the
    spelling the user entered - the portal is the authority on what it
    accepts, and nothing here has established that it is as relaxed.
    """
    return (raw or "").strip().casefold()


def duplicate_entityvalues(id_values) -> set:
    """Return the set of entityvalues used more than once (non-empty).

    Reported in the canonical spelling: two slots differing only in case
    are one duplicate, and naming it in either of their spellings would be
    arbitrary.
    """
    counts: dict[str, int] = {}
    for raw in id_values or []:
        entityvalue = canonical_entityvalue(raw)
        if entityvalue:
            counts[entityvalue] = counts.get(entityvalue, 0) + 1
    return {entityvalue for entityvalue, count in counts.items() if count > 1}


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
