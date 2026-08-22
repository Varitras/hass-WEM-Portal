"""Pure expert-path helpers, kept clear of the expert client.

These used to live in expert_writer.py - which imports curl_cffi and lxml at
module level. config_flow needs the option helpers on every options dialog and
the entity needs the id digest at construction, so leaving them beside the
client meant every installation loaded the HTTP stack whether or not expert
access was ever enabled. Splitting them out is what makes the lazy import of
the client real rather than claimed.
"""

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    CONF_EXPERT_ENABLE_MODULE_NAV,
    CONF_EXPERT_ENABLE_SECURITY_CODE,
    CONF_EXPERT_MODULE_ARG,
)


def discovery_option_list(
    discovered: Iterable[Mapping[str, Any]] | None,
    current_ids: Iterable[str | None] | None,
) -> list[dict[str, str]]:
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


def canonical_entityvalue(raw: str | None) -> str:
    """One spelling to compare entityvalues by.

    They are hex, so case carries no meaning: `3A7F` and `3a7f` are the same
    parameter of the same installation. Comparing them verbatim let one id
    occupy two slots, and made the service's allowlist refuse whichever
    spelling the caller did not happen to use.

    Only for COMPARING. What goes to the portal is the spelling CONFIGURED
    in the slot - it came out of discovery, so the portal has accepted it,
    while the caller's has proved nothing. This used to pass the typed one
    on, which turned the same uncertainty the other way round: not knowing
    whether the portal is as relaxed is the reason to send the id that is
    known to work.
    """
    return (raw or "").strip().casefold()


def duplicate_entityvalues(id_values: Iterable[str | None] | None) -> set[str]:
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


def expert_client_options(options: Mapping[str, Any]) -> dict[str, Any]:
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


def entityvalue_digest(entityvalue: str) -> str:
    """Short, stable digest of an entityvalue for use in internal IDs.

    Used wherever an id derived from the entityvalue must be unique and
    stable but ends up in persisted/inspectable places (entity-registry
    unique_ids, persistent-notification ids, task names). The raw
    entityvalue is installation-specific and shouldn't appear there
    verbatim - someone sharing their .storage files or diagnostic dumps
    would otherwise leak it. SHA-256 (truncated) keeps the mapping
    deterministic without being reversible.
    """
    cleaned = (entityvalue or "").strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
