"""kura#62: the reconciler stops dispatching when the training runner's unclaimed
queue is already at the ceiling. The saturation predicate is pure; fix its edges."""
from tagger.reconciler import _saturated


def test_below_ceiling_dispatches():
    assert _saturated(backlog=3, dispatched_this_tick=0, backlog_max=10) is False


def test_at_ceiling_holds():
    assert _saturated(backlog=10, dispatched_this_tick=0, backlog_max=10) is True


def test_this_ticks_dispatches_count_toward_the_ceiling():
    # 8 already queued + 2 sent this tick == 10: the next one is held.
    assert _saturated(backlog=8, dispatched_this_tick=2, backlog_max=10) is True
    assert _saturated(backlog=8, dispatched_this_tick=1, backlog_max=10) is False


def test_unreadable_backlog_fails_open():
    # None means the broker could not be read: do NOT block (pre-#62 behaviour).
    assert _saturated(backlog=None, dispatched_this_tick=99, backlog_max=1) is False
