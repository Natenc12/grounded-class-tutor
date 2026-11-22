"""Throwaway (#254): proves merge-check goes red when the test job fails. Reverted next commit."""


def test_merge_check_goes_red_when_a_test_fails():
    assert 1 + 1 == 3, "deliberate failure for the #254 red proof"
