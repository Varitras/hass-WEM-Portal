"""What one configured account holds while it is loaded.

This used to be a bare dict under `hass.data[DOMAIN][entry_id]`, created with
three keys and grown to ten - the other seven added from wherever needed one,
each spelled out as a string at every use. A dict access that misses returns
None instead of failing, so a typo is silent: the auto-poll simply never
starts, or a lock is silently absent and two portal operations run at once.

Home Assistant's own answer is `entry.runtime_data`, available on the minimum
version this integration supports. It also removes the attribute itself when
the entry unloads, which makes "is this entry still loaded?" a question the
framework answers rather than one this integration tracks by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from homeassistant.config_entries import ConfigEntry

from .expert_controller import ExpertController

if TYPE_CHECKING:
    from .coordinator import WemPortalDataUpdateCoordinator
    from .wemportalapi import WemPortalApi


def account_unique_id(username: str | None) -> str:
    """Normalised account id: the config entry's unique_id AND the key the
    per-account state lives under.

    Portal usernames are email addresses, so casing and stray whitespace are
    not meaningful - but a raw comparison treated "Max@example.org" and
    "max@example.org" as two accounts, which meant two entries polling the
    same installation twice.
    """
    return (username or "").strip().lower()


@dataclass
class AccountState:
    """What one portal account remembers ACROSS reloads.

    Every field here used to be its own module-level global in the module
    that used it - six of them across five files, each invisible from the
    others. They exist because a reload rebuilds every object while the
    portal's memory does not reset: a 403 backoff must not be forgotten by
    the very reinstantiation it caused, and a warning already given must not
    repeat after every options change.

    NOT here on purpose: the API-wide 403 backoff
    (wemportalapi._BLOCKED_UNTIL). The portal rate-limits per IP, not per
    account, so that one is shared by every account of the installation.
    """

    # Consecutive auth failures, seeding the coordinator's reauth counter -
    # a failed setup triggers the very reload that would otherwise reset it.
    auth_failures: int = 0
    # Monotonic deadline of the expert (web) 403 backoff. Per account, with
    # a test pinning that one account's backoff does not spread to another.
    expert_blocked_until: float = 0.0
    # One warning per subject, surviving the reload that rebuilds the
    # objects doing the warning.
    duplicate_rows_reported: set[str] = field(default_factory=set)
    unreadable_values_reported: set[tuple[str, str]] = field(default_factory=set)
    maintenance_markers_reported: set[str] = field(default_factory=set)
    missing_job_ids_reported: set[str] = field(default_factory=set)


# The ONE sanctioned module-level mutable in this package: the registry the
# per-account state survives reloads in. Anything else that wants to outlive
# its object belongs in here - the guard test enforces exactly that.
_ACCOUNT_STATES: dict[str, AccountState] = {}


def account_state(username: str | None) -> AccountState:
    """The reload-surviving state of one portal account."""
    return _ACCOUNT_STATES.setdefault(account_unique_id(username), AccountState())


def reset_account_states_for_tests() -> None:
    """Only the test suite has any business calling this - production has no
    situation in which forgetting an account's memory is correct."""
    _ACCOUNT_STATES.clear()


class ModuleRef(NamedTuple):
    """One module of one device, as the portal addresses it.

    The pair travelled as a bare `(index, type)` tuple in memory and as an
    "index:type" string in the persisted module cache, both assembled by
    hand wherever needed. A NamedTuple gives the two numbers their names
    back while every existing tuple comparison, unpacking and dict lookup
    keeps working - which is what lets the migration happen in slices.
    """

    module_index: int
    module_type: int

    @classmethod
    def from_storage_key(cls, key: str) -> ModuleRef:
        """The reference a persisted "index:type" cache key names.

        Raises ValueError on anything else: an unreadable key means the
        store is not ours to guess about.
        """
        index_text, type_text = key.split(":", 1)
        return cls(module_index=int(index_text), module_type=int(type_text))

    def as_storage_key(self) -> str:
        """The "index:type" spelling the persisted module cache uses.

        Pinned by test: existing installations have these strings on disk,
        so order and separator are a compatibility contract, not a style
        choice.
        """
        return f"{self.module_index}:{self.module_type}"


@dataclass
class WemPortalData:
    """Runtime state of one loaded config entry."""

    api: WemPortalApi
    coordinator: WemPortalDataUpdateCoordinator

    # Everything the expert path owns: the shared lock, the entities, the
    # optional poll timer and the per-id failure tally. Nine fields sat here
    # before, each reachable from anywhere that could reach this store, and
    # "is the poll running" was a question you answered by reading three.
    expert: ExpertController = field(default_factory=ExpertController)

    # Set before the platforms are unloaded, which is the slow part - anything
    # already talking to the portal in a worker thread has to learn about the
    # teardown at its next gate rather than at the end of it. Home Assistant
    # only drops runtime_data once the unload has finished, so its presence
    # alone cannot answer this.
    unloading: bool = False

    def __post_init__(self) -> None:
        # The controller holds the store, not the entry: an in-flight poll
        # must still find the api it was using AFTER the entry was unloaded,
        # because Home Assistant drops runtime_data at the end of the
        # teardown while the read is still running. Reading it back through
        # the entry raised AttributeError there instead of skipping.
        self.expert.bind(self)

    def begin_unload(self) -> None:
        """Announce the teardown, before the platforms come down.

        A named operation rather than a bare flag assignment: it is set at
        one moment and read at four gates, and the reason it has to be set
        THIS early is what gets lost when it is one line in the middle of
        async_unload_entry.
        """
        self.unloading = True

    def abort_unload(self) -> None:
        """Take the announcement back when the teardown did not happen.

        A platform may refuse to unload, and Home Assistant then leaves the
        entry loaded and running. Without this the flag stayed set for the
        life of that entry: polling carried on, but every write - entity and
        service alike - answered "the integration is being unloaded" forever,
        and only a restart cleared it.
        """
        self.unloading = False

    def why_not_current(self, config_entry: ConfigEntry) -> str | None:
        """Why an operation holding THIS state may no longer act, or None.

        The entry id answers neither of the two ways it can happen. Home
        Assistant removes runtime_data only once the platforms are down, so
        for the whole teardown the id is still there; and a reload puts a NEW
        state under the SAME id while the operation still holds the old one.
        Identity plus the flag covers both.
        """
        if getattr(config_entry, "runtime_data", None) is not self:
            return "the integration was reloaded"
        if self.unloading:
            return "the integration is being unloaded"
        return None


# The entry type carrying the above, so `entry.runtime_data` is typed at every
# use instead of being an untyped dict lookup.
WemPortalConfigEntry = ConfigEntry[WemPortalData]


def raise_if_not_writable(config_entry: ConfigEntry, what: str) -> WemPortalData:
    """The gate every write from an entity passes through.

    `unloading` is set at the very top of async_unload_entry, before the
    platforms come down - and that gap is real time: tearing down four
    platforms and their entities takes long enough for a frontend click to
    land in the middle of it. Only the domain service used to check, so
    Number, Select, Switch and the expert entity could each still start a
    write into a session that was about to be closed.

    Returns the runtime data so the caller does not look it up twice.
    """
    from homeassistant.exceptions import HomeAssistantError

    data: WemPortalData | None = getattr(config_entry, "runtime_data", None)
    if data is None:
        raise HomeAssistantError(f"{what}: this WEM Portal account is not loaded.")
    reason = data.why_not_current(config_entry)
    if reason is not None:
        raise HomeAssistantError(f"{what}: {reason}; the value was not changed.")
    return data
