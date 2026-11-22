"""Throwaway for #251: proves merge-check goes red on a failing test. Removed before merge."""


def test_throwaway_fails_on_purpose():
    assert 1 == 2, "deliberate failure for #251's proof run"
