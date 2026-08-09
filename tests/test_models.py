"""The typed identities of the data model.

Umbau phase 3 (K1): a module was addressed as a bare `(index, type)` tuple in
memory and an "index:type" string on disk, both assembled by hand wherever
needed. `ModuleRef` gives the pair its names back - and because it is a
NamedTuple, every existing tuple comparison, unpacking and dict lookup keeps
working, which is what makes the migration safe to do in slices.
"""

import pytest

from custom_components.wemportal.models import ModuleRef


def test_a_module_ref_round_trips_through_its_storage_key():
    """The persisted module cache spells the pair "index:type"."""
    reference = ModuleRef(module_index=2, module_type=17)

    assert reference.as_storage_key() == "2:17"
    assert ModuleRef.from_storage_key("2:17") == reference


def test_the_storage_key_format_is_pinned():
    """Existing installations have "index:type" strings on disk. A change in
    either order or separator silently orphans every persisted module cache -
    the deserializer would build keys nothing looks up."""
    assert ModuleRef.from_storage_key("0:1") == ModuleRef(module_index=0, module_type=1)

    with pytest.raises(ValueError):
        ModuleRef.from_storage_key("not-a-key")


def test_a_module_ref_is_a_drop_in_for_the_bare_tuple():
    """The migration happens in slices, so during it the same dict holds
    keys of both spellings. Equality, hashing and unpacking must not tell
    them apart - the day they do, half the cache goes invisible."""
    reference = ModuleRef(module_index=0, module_type=1)

    assert reference == (0, 1)
    assert hash(reference) == hash((0, 1))
    assert {reference: "x"}[(0, 1)] == "x"
    assert {(0, 1): "x"}[reference] == "x"

    module_index, module_type = reference
    assert (module_index, module_type) == (0, 1)
