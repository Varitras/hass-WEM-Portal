"""How long one test may take, and who is over.

The suite has no business containing a test that runs for minutes. When one
does, it is nearly always an accident rather than a decision - a wait that
was supposed to be shortened and no longer is.

That is not hypothetical: `test_a_recovery_leaves_a_busy_connection_alone`
shortened the lock timeout with

    monkeypatch.setattr(wemportalapi, "API_LOCK_TIMEOUT_SECONDS", 0.05)

and kept passing after `reset_transport` moved to transport.py, where the
constant is read from a different module namespace. The patch landed on a
name nothing reads any more, so the test waited out the real 330 seconds -
80% of the entire suite, for months, in green.

Neither the mutation run nor any structural guard could see it: the
assertions still held, and `wemportalapi` still has the constant (and uses
it elsewhere), so even a check for a dead patch target would have passed.
What is visible from outside is the clock.
"""

# Generous on purpose. This is not a performance budget - it exists to catch
# a wait nobody meant to have, and a threshold that argues with ordinary
# slowness would get raised until it means nothing. The slowest deliberate
# test in this suite is an end-to-end run of a few seconds.
SLOW_TEST_SECONDS = 30.0


def over_budget(durations, budget=SLOW_TEST_SECONDS):
    """The tests whose total runtime passed `budget`, slowest first.

    Total, not per phase: a test that spends its time in a fixture is just
    as slow as one that spends it in the body.
    """
    too_slow = [
        (node_id, seconds) for node_id, seconds in durations.items() if seconds > budget
    ]
    return sorted(too_slow, key=lambda entry: -entry[1])
