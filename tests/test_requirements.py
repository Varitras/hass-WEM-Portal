"""The two places that name the runtime dependencies say the same thing.

Home Assistant installs an integration's dependencies from `manifest.json`
and from nothing else. `requirements_runtime.txt` repeats the same lines so
the modules import during test collection - two copies of one list, and
until now nothing held them together.

Dependabot is what makes that drift expensive. It understands requirements
files and not Home Assistant manifests, so a security update raises the
bound in the txt, opens a green pull request, and leaves the version people
actually install exactly where it was. The fix would look done and change
nothing.
"""

import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = REPO / "requirements_runtime.txt"


def _requirements_of(text: str) -> set[str]:
    """The requirement lines of a pip requirements file.

    Comments, blank lines and `-r` includes are not requirements. Versions
    stay verbatim: the bound is the entire subject here, since all a
    security bump changes is a few digits.
    """
    lines = (line.strip() for line in text.splitlines())
    return {line for line in lines if line and not line.startswith(("#", "-"))}


def _manifest() -> dict:
    """The integration's manifest, found rather than named.

    Spelling out the domain would make this go blind if the domain is ever
    renamed - and blind is worse than absent, because the suite stays green.
    """
    manifests = sorted((REPO / "custom_components").glob("*/manifest.json"))
    assert len(manifests) == 1, (
        f"expected exactly one integration manifest, found {manifests} - "
        "this guard compares against the manifest and cannot pick."
    )
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def test_the_manifest_and_the_runtime_file_name_the_same_requirements():
    """A bound raised in only one of them is a fix nobody receives."""
    installed_by_home_assistant = set(_manifest()["requirements"])
    installed_for_the_tests = _requirements_of(
        RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")
    )

    assert installed_by_home_assistant == installed_for_the_tests, (
        f"manifest.json and requirements_runtime.txt disagree: "
        f"{installed_by_home_assistant ^ installed_for_the_tests}. Home "
        "Assistant installs from the manifest and reads nothing else, so a "
        "version changed in only one place never reaches an installation. "
        "Change both."
    )


def test_the_reader_keeps_the_bound_a_security_bump_would_raise():
    """A guard that passes proves nothing; this is the shape it exists for.

    The left-hand side is what a requirements file looks like around the one
    line that matters, the right-hand side the same requirement after the
    three digits Dependabot would raise in one file and not the other.
    """
    listed = _requirements_of(
        "# a note about why\n\ncurl_cffi>=0.15.0\n-r requirements_runtime.txt\n"
    )

    assert listed == {"curl_cffi>=0.15.0"}
    assert _requirements_of("curl_cffi>=0.15.3\n") != listed
