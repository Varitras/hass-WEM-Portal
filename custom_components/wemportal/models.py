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

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import WemPortalDataUpdateCoordinator
    from .wemportalapi import WemPortalApi


@dataclass
class WemPortalData:
    """Runtime state of one loaded config entry."""

    api: WemPortalApi
    coordinator: WemPortalDataUpdateCoordinator

    # Only one expert portal operation per account at a time - an entity
    # write, the service, or the auto-poll read - so they cannot collide on
    # the same parameter or open parallel portal sessions. Acquired
    # non-blocking by each expert path.
    expert_lock: threading.Lock = field(default_factory=threading.Lock)

    # Expert number entities, registered by the platform so the auto-poll can
    # push read values into them.
    expert_entities: list[Any] = field(default_factory=list)

    # Set by the entry setup, called by the entity platform once it has
    # registered its entities: the poll has nothing to poll before that.
    start_expert_auto_poll: Callable[[], None] | None = None
    expert_poll_started: bool = False
    expert_poll_stopped: bool = False
    expert_poll_initial_task: Any = None
    expert_poll_unsub: Callable[[], None] | None = None
    # Consecutive auto-poll failures per parameter, and which of them the user
    # has already been told about - so a portal that keeps refusing one
    # parameter does not produce a notification every interval.
    expert_poll_fail_counts: dict[str, int] = field(default_factory=dict)
    expert_poll_fail_notified: set[str] = field(default_factory=set)

    # Set before the platforms are unloaded, which is the slow part - anything
    # already talking to the portal in a worker thread has to learn about the
    # teardown at its next gate rather than at the end of it. Home Assistant
    # only drops runtime_data once the unload has finished, so its presence
    # alone cannot answer this.
    unloading: bool = False

    def begin_unload(self) -> None:
        """Announce the teardown, before the platforms come down.

        A named operation rather than a bare flag assignment: it is set at
        one moment and read at four gates, and the reason it has to be set
        THIS early is what gets lost when it is one line in the middle of
        async_unload_entry.
        """
        self.unloading = True

    def why_not_current(self, config_entry) -> str | None:
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

    def is_current_for(self, config_entry) -> bool:
        """Whether an operation holding this state may still act."""
        return self.why_not_current(config_entry) is None


# The entry type carrying the above, so `entry.runtime_data` is typed at every
# use instead of being an untyped dict lookup.
WemPortalConfigEntry = ConfigEntry[WemPortalData]


def raise_if_not_writable(config_entry, what: str) -> "WemPortalData":
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

    data = getattr(config_entry, "runtime_data", None)
    if data is None:
        raise HomeAssistantError(
            f"{what}: this WEM Portal account is not loaded."
        )
    reason = data.why_not_current(config_entry)
    if reason is not None:
        raise HomeAssistantError(
            f"{what}: {reason}; the value was not changed."
        )
    return data
