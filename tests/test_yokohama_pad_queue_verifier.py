"""Repeated judge waits preserve order, entry count, and contiguous receipt sequences."""
import pytest

from scripts.verify_yokohama_pad_queue import response_sequence_checks

WAIT = "wait_at_current_hold"
ENTER = "enter_delivery_approach"


@pytest.mark.parametrize("actions,entries,repeated,passed", [
    ([WAIT, ENTER], 1, False, True),
    ([WAIT, WAIT, ENTER], 1, False, False),
    ([WAIT, WAIT, ENTER], 1, True, True),
    ([WAIT, WAIT, WAIT, ENTER], 1, True, True),
    ([ENTER], 1, True, False),
    ([WAIT, ENTER, WAIT], 1, True, False),
    ([WAIT, ENTER, ENTER], 1, True, False),
    ([WAIT, WAIT, ENTER, ENTER, ENTER], 3, True, True),
    ([WAIT, ENTER, ENTER], 3, True, False),
    ([WAIT, "unknown", ENTER], 1, True, False),
    ([], 1, True, False),
])
def test_wait_and_entry_receipt_order(actions, entries, repeated, passed):
    requests = [dict(sequence=i, action=action) for i, action in enumerate(actions)]
    assert all(response_sequence_checks(requests, entries, repeated).values()) is passed


@pytest.mark.parametrize("sequences", [[0, 2, 3], [0, 1, 1], [1, 2, 3]])
def test_gaps_replays_and_wrong_start_rejected(sequences):
    requests = [dict(sequence=i, action=action) for i, action in zip(sequences, [WAIT, WAIT, ENTER])]
    checks = response_sequence_checks(requests, 1, True)
    assert checks["wait_then_continue"] is True
    assert checks["sequences"] is False
