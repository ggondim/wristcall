import pytest

from wristcall.admission import CallSlots, call_kind
from wristcall.config import ConcurrentCallsConfig


def slots(**limits) -> CallSlots:
    return CallSlots(ConcurrentCallsConfig(**limits))


def test_no_limit_always_admits():
    s = slots()
    assert all(s.try_acquire("conversation") is not None for _ in range(100))
    assert all(s.try_acquire("one_way") is not None for _ in range(100))
    assert s.active() == {"conversation": 100, "one_way": 100}


def test_limit_refuses_until_a_slot_is_released():
    s = slots(conversation=1)
    first = s.try_acquire("conversation")
    assert first is not None
    assert s.try_acquire("conversation") is None
    first.release()
    assert s.try_acquire("conversation") is not None


def test_kinds_are_independent():
    s = slots(conversation=1, one_way=1)
    assert s.try_acquire("conversation") is not None
    assert s.try_acquire("conversation") is None
    assert s.try_acquire("one_way") is not None
    assert s.try_acquire("one_way") is None


def test_release_never_goes_below_zero():
    s = slots(conversation=1)
    slot = s.try_acquire("conversation")
    slot.release()
    slot.release()
    assert s.active()["conversation"] == 0
    # The extra release did not make room for a second call.
    assert s.try_acquire("conversation") is not None
    assert s.try_acquire("conversation") is None


def test_double_release_frees_one_slot():
    s = slots(conversation=2)
    first = s.try_acquire("conversation")
    assert s.try_acquire("conversation") is not None
    first.release()
    first.release()
    assert s.active() == {"conversation": 1, "one_way": 0}


def test_active_counts_by_kind():
    s = slots()
    assert s.active() == {"conversation": 0, "one_way": 0}
    a = s.try_acquire("conversation")
    s.try_acquire("one_way")
    s.try_acquire("one_way")
    assert s.active() == {"conversation": 1, "one_way": 2}
    a.release()
    assert s.active() == {"conversation": 0, "one_way": 2}


@pytest.mark.parametrize(
    ("call_type", "kind"),
    [("conversation", "conversation"), ("one-shot", "one_way"), ("monologue", "one_way")],
)
def test_call_kind(call_type, kind):
    assert call_kind(call_type) == kind
