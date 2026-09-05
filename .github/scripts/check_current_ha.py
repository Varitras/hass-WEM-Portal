"""Fail unless this environment runs the Home Assistant CI resolves as current.

The "current" leg of check.sh runs whatever the local interpreter holds, and
a venv pinned by hand ages quietly. It held 2026.8 while CI - which resolves
the newest plugin release pinning a final Home Assistant - ran 2026.9, and
three deprecation failures CI would have shown stayed invisible behind a
green local run. The minimum leg had check_min_ha.py for exactly this; the
current leg had nothing.

Only PyPI knows which Home Assistant the resolved plugin release ships, so
this asks the question the way CI does, with the same code, and compares the
answer with what is installed. Run with the current interpreter, before its
suite. It needs the network, and a comparison it cannot make is a failure,
not a pass: "could not check" is what left the gap open.
"""

import pathlib
import re
import sys

from homeassistant.const import __version__

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from check_min_ha import feature_release  # noqa: E402
from resolve_phcc import PACKAGE, newest_stable, recent_pins  # noqa: E402


def pinned_home_assistant(pin: str) -> str:
    """'homeassistant==2026.9.0' -> '2026.9.0'.

    Only an exact pin will do: the plugin pins one release, and anything
    looser would leave the comparison with nothing definite to compare.
    """
    match = re.search(r"==\s*([\d.]+)", pin)
    if match is None:
        raise SystemExit(f"cannot read a Home Assistant version out of {pin!r}")
    return match.group(1)


def main() -> int:
    try:
        pins = recent_pins()
    except OSError as exc:
        print(
            f"could not ask PyPI which Home Assistant CI resolves ({exc}). A "
            "current leg that cannot be compared with CI proves nothing - "
            "retry with the network available.",
            file=sys.stderr,
        )
        return 1
    plugin = newest_stable(pins)
    expected = pinned_home_assistant(pins[plugin] or "")
    if feature_release(__version__) != feature_release(expected):
        print(
            f"CI resolves Home Assistant {expected} as current, but this "
            f"environment holds {__version__}. A green run here says nothing "
            f'about CI - upgrade it: pip install "{PACKAGE}=={plugin}" '
            "pytest-timeout -r requirements_runtime.txt",
            file=sys.stderr,
        )
        return 1
    print(f"current leg runs Home Assistant {__version__}, as CI resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
