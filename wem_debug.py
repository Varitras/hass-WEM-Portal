#!/usr/bin/env python3
"""Standalone debug tool for the WEM Portal expert (Fachmann) navigation.

Runs WemPortalExpertClient directly, outside Home Assistant, so the
navigation chain (Fachmann unlock, module select, timer polls, dialog
read/write) can be tested in seconds instead of a full build-install-
restart-trigger-download-log cycle.

Requirements (install once):
    pip install curl_cffi lxml --break-system-packages

Usage:
    # Read-only: show the current value and allowed range.
    python3 wem_debug.py --entityvalue 64001807000000001E40000F4C0300110104

    # Write: set a new value (validated against the live option list,
    # then verified by re-reading the form - same as the real integration).
    python3 wem_debug.py --entityvalue 64001807000000001E40000F4C0300110104 --value 30

    # Verbose navigation diagnostics (recommended while debugging):
    python3 wem_debug.py --entityvalue ... --value 30 --debug

    # Point at a different checkout of the integration:
    python3 wem_debug.py --entityvalue ... --path /path/to/custom_components/wemportal

Credentials: never passed as CLI arguments (would leak into shell history
and process listings). Set WEM_USERNAME / WEM_PASSWORD as environment
variables, or just run the script and it will prompt for them.

Safety: this hits the real WEM Portal exactly like the HA integration
does. Don't loop-run it - the portal rate-limits (403) hard and IP-wide.
One deliberate run per test is the right cadence.
"""
import argparse
import getpass
import logging
import os
import sys
import types
import importlib.util


def _install_ha_stub():
    """Provide the minimal homeassistant.exceptions stub the wemportal
    modules need at import time, without requiring homeassistant to be
    installed. WemPortalError etc. just need to be real Exception
    subclasses for this standalone use - HA's richer behavior isn't
    needed outside of Home Assistant itself.
    """
    if "homeassistant" in sys.modules:
        return  # real HA is installed (e.g. running on the HA host) - use it
    ha = types.ModuleType("homeassistant")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    ha_exceptions.HomeAssistantError = HomeAssistantError
    ha.exceptions = ha_exceptions
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.exceptions"] = ha_exceptions


def _load_client(module_path):
    """Load const/exceptions/expert_writer from the given
    custom_components/wemportal directory, without needing the package
    to be pip-installed or Home Assistant to be present.
    """
    _install_ha_stub()

    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [os.path.dirname(module_path)]
    sys.modules["custom_components"] = pkg
    pkg2 = types.ModuleType("custom_components.wemportal")
    pkg2.__path__ = [module_path]
    sys.modules["custom_components.wemportal"] = pkg2

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.wemportal.{name}", os.path.join(module_path, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"custom_components.wemportal.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    load("const")
    load("exceptions")
    expert_writer = load("expert_writer")
    return expert_writer


def main():
    parser = argparse.ArgumentParser(
        description="Standalone WEM Portal expert-parameter debug tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--entityvalue", required=True, help="Hex entityvalue ID of the parameter")
    parser.add_argument("--value", type=float, default=None,
                        help="New value to write. Omit to only read the current value.")
    parser.add_argument("--module-arg", default=None,
                        help="Icon-menu module argument (default: '6', heat pump)")
    parser.add_argument("--path", default="custom_components/wemportal",
                        help="Path to the wemportal integration directory "
                             "(default: ./custom_components/wemportal)")
    parser.add_argument("--debug", action="store_true",
                        help="Show detailed navigation step logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Quiet down noisy third-party loggers even in --debug mode.
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)

    module_path = os.path.abspath(args.path)
    if not os.path.isdir(module_path):
        print(f"Integration directory not found: {module_path}", file=sys.stderr)
        print("Point --path at your custom_components/wemportal checkout.", file=sys.stderr)
        return 1

    print("Reminder: this hits the real WEM Portal. Don't loop-run this - "
          "the portal rate-limits (403) hard and IP-wide.\n")

    expert_writer = _load_client(module_path)

    username = os.environ.get("WEM_USERNAME") or input("WEM Portal username (email): ")
    password = os.environ.get("WEM_PASSWORD") or getpass.getpass("WEM Portal password: ")

    client = expert_writer.WemPortalExpertClient(
        username, password, module_arg=args.module_arg,
    )

    try:
        if args.value is not None:
            print(f"\nWriting {args.value} to {args.entityvalue} ...\n")
            state = client.write_parameter(args.entityvalue, args.value)
            print(f"\nOK - verified value: {state.current}")
        else:
            print(f"\nReading {args.entityvalue} ...\n")
            state = client.read_parameter(args.entityvalue)
            print(f"\nCurrent value: {state.current}")
        print(f"Allowed range: {state.min_value} .. {state.max_value} "
              f"({len(state.options)} discrete options)")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
