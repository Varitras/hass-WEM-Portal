#!/bin/sh
# Every gate this repository ships, in one command.
#
# The rule was prose in a plan file before, which meant remembering it -
# and the expensive gates are exactly the ones a tired person skips. Run
# this before calling a change done; CI runs the same set.
#
# The second Home Assistant version is optional because its interpreter
# lives wherever you put it:
#
#     MIN_HA_PYTHON=/path/to/min-ha-venv/bin/python .github/scripts/check.sh
#
# Without it the minimum-version run is SKIPPED and says so - a skipped
# gate that announces itself is honest; one that passes silently is not.

set -e

PYTHON="${PYTHON:-python}"

echo "== ruff =="
"$PYTHON" -m ruff check .

echo "== format =="
"$PYTHON" -m ruff format --check .

echo "== mypy =="
"$PYTHON" -m mypy

echo "== pytest (this environment) =="
"$PYTHON" -m pytest tests/ -q -m ""

if [ -n "$MIN_HA_PYTHON" ]; then
    echo "== pytest (minimum Home Assistant) =="
    "$MIN_HA_PYTHON" -m pytest tests/ -q -m ""
else
    echo "== pytest (minimum Home Assistant): SKIPPED, set MIN_HA_PYTHON =="
fi

echo "== mutations =="
"$PYTHON" .github/scripts/mutate.py .github/mutations/response-gate.json

echo
echo "all gates passed"
