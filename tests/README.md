# The gates and the guards

What protects this integration, what each guard holds, and what to do when
one of them turns red. Written for whoever changes this code next - which
is usually somebody with no memory of why any of it is here.

## Run everything

```sh
.github/scripts/check.sh
```

Ruff, formatting, mypy, the test suite, and the mutation run, in that
order, stopping at the first failure. The suite is also run against the
**minimum** supported Home Assistant, which needs a second interpreter:

```sh
MIN_HA_PYTHON=/path/to/min-ha-venv/bin/python .github/scripts/check.sh
```

Without that variable the minimum-version run is skipped **and says so**.
CI runs the same set; a guard in `tests/test_guards.py` fails if the two
ever drift apart.

## Why a mutation run

A passing test proves nothing on its own - every worthless test in this
repository's history passed happily while the feature was broken. So
`.github/mutations/response-gate.json` describes ~360 deliberate breakages
("delete this guard clause", "swap these two values"), and
`.github/scripts/mutate.py` checks that a test actually fails for each one.

A mutation that **survives** means the code was broken and the suite stayed
green: either the test is worthless, or - twice now - a guard has gone
blind and no longer looks where the code moved.

When you add a behaviour worth keeping, add a mutation for it. When you
move code, the `path` fields move with it.

## The guards

Structural tests that fail on a shape rather than on a value. Each one
exists because the thing it prevents actually happened here.

| Guard | Holds |
|---|---|
| `test_budgets.py` | No module or function grows past its frozen budget |
| `test_durations.py` | No single test quietly starts taking minutes (budget in `durations.py`, enforced from `conftest.py`), and a run that stops making progress is cut off rather than only measured |
| `test_guards.py` | No guard binds itself to one source file; every guard is listed; `check.sh` matches CI |
| `test_account_state.py` | No mutable module-level state outside the one sanctioned registry |
| `test_platform_entities.py` | Every platform creates entities through the shared helper, so a reading that arrives on a later cycle still gets one - and reads its row through the shared lookup, so a reclassified row is not published by the entity it no longer belongs to |
| `test_portal_boundaries.py` | Every `.json()` read and HTML parse sits in a declared boundary function |
| `test_portal_values.py` | Decimal-comma normalisation lives in exactly one place |
| `test_reading_boundary.py` | Readings are read as attributes, never as dict keys |
| `test_reading_invariants.py` | Every reading the mapper writes carries the fields another path reads |
| `test_repairs.py` | Every repair issue is translated in every language and prefixed with the entry id |
| `test_security.py` | No module reaches past the diagnostics redaction, and the expert client stays behind its own import |
| `test_requirements.py` | `manifest.json` and `requirements_runtime.txt` name the same dependencies |
| `test_transport_boundary.py` | `transport.py` imports no domain module |
| `test_transport_errors.py` | Only the two value-path reads opt into a transport retry |

## When a budget turns red

`tests/test_budgets.py` freezes size and complexity so the god module
cannot grow back. Two different rules, on purpose:

**Lines are a ceiling.** They move on almost every change, so the test only
asks that a module not grow past its entry. Modules under `LINE_LIMIT`
(900) need no entry at all.

**Complexity is a ratchet - an exact match.** It changes rarely, and when a
function does get simpler that progress is written down rather than left as
headroom for the next person to spend.

So there are four ways this test speaks to you:

| Message | What it means | What to do |
|---|---|---|
| module over budget | a file grew past its entry (or past 900 lines without one) | Split it. If the growth is genuinely warranted, raise the entry **in the same commit** - the point is that the decision is visible in the diff. |
| budget far above the real size | a module shrank; the ceiling is now meaningless | Lower the entry to today's count. |
| function over budget | new complexity, undeclared | Read the function. Cognitive complexity counts **nesting**, so an early return or a guard clause usually helps more than extracting a helper. If it is warranted, add the entry. |
| budget out of step | a function got simpler, was renamed, or is gone | Set the entry to the current value, or drop it. |

The measure is Cognitive Complexity (Campbell / SonarSource), implemented
in `tests/complexity.py` and calibrated against SonarQube Cloud's own
numbers. It counts nesting rather than paths: a flat ten-case dispatch is
cheap, three loops inside each other are not. `COMPLEXITY_LIMIT` is 15,
SonarSource's default.

### Structure is not enough

Most of the guards above check a **shape** or a **list**: no dict access,
no module global, every boundary declared. None of them can see a write
path that produces a perfectly valid object with one field left empty -
a field some *other* path needs.

That is how the per-module freshness came to protect nothing: the mapper
wrote ordinary sensors without the module address, and the ageing pass
matches on exactly that address. Controls had it, plain sensors did not,
and the test that should have caught it filled the address in by hand.

`test_reading_invariants.py` is the answer: it states invariants over the
**data** and checks them against what the real mapper produces. When you
add a field that one path writes and another reads, state it there.

## Adding a guard

Two rules, both learned the hard way:

1. **Scan the package, never a single file.** `PACKAGE.glob("*.py")`, not
   `Path(some_module.__file__).read_text()`. A scan pinned to one file goes
   blind the moment code moves to a new module - and a blind guard is worse
   than none, because the suite stays green. This has happened twice; the
   guard against it is in `test_guards.py`.
2. **Prove the guard can fail.** A test that passes proves nothing. Add a
   case that feeds the detector the exact shape it exists to catch - see
   `test_the_scan_catches_the_blindness_it_was_written_for`, which uses the
   verbatim line from the incident.

Then add the file to `GUARD_FILES` in `test_guards.py` with one line saying
what it holds. A guard nobody lists is a guard nobody knows to keep.

## Layout

Tests move with their subject: `test_portal_values.py` covers the value
parser, `test_value_freshness.py` covers ageing, and so on. Two files are
deliberately large and cross-cutting - `test_hardening.py` (regression
tests, each named after the failure it prevents) and `test_e2e.py` (a real
Home Assistant instance; marked `e2e` and deselected in the everyday run,
which is why CI passes `-m ""`).
