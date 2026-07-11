"""CI test suite for the wemportal integration.

Runs against real Home Assistant via pytest-homeassistant-custom-component
on GitHub Actions (see .github/workflows/test.yaml). Unlike the local-only
suite, these tests import the integration directly and rely on Home
Assistant actually being installed.
"""
